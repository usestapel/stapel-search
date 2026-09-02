"""The service layer: indexing, the query pipeline, rebuild and drift.

Everything that decides *meaning* lives here or in ``backends/_shared.py``;
the backends only execute. Two consequences worth stating because they are
the whole architecture:

- the index table is written by :func:`index_documents` in one transaction
  together with ``backend.upsert`` — a row that exists without its engine
  entry is the outbox defect one layer down, and it has the same cure;
- ``card`` and ``promoted`` are served from this module's own table under
  every backend. A backend never shapes a result row.

The four Projection guarantees are implemented here by name: idempotency on
redelivery (``source_event_id``), ordering (``source_seq``), ``rebuild``
from the owner's snapshot Function, and ``drift_check``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .dto import Cursor, IndexDocument, IndexSettings, SearchDocumentInput
from .registry import SourceSpec, get_facet_mapping, get_source

logger = logging.getLogger(__name__)

#: Facet terms longer than this lose their tail (``search.W005``): a term is
#: a discriminator, and one that no longer discriminates is worse than a
#: missing one because it silently merges two options into a single count.
MAX_TERM_CHARS = 160


@dataclass
class IndexReport:
    """What one indexing pass did — logged and returned, never invisible."""

    indexed: int = 0
    removed: int = 0
    skipped_stale: int = 0
    skipped_duplicate: int = 0
    missing: tuple[str, ...] = ()
    truncated_terms: int = 0

    def merge(self, other: "IndexReport") -> "IndexReport":
        return IndexReport(
            indexed=self.indexed + other.indexed,
            removed=self.removed + other.removed,
            skipped_stale=self.skipped_stale + other.skipped_stale,
            skipped_duplicate=self.skipped_duplicate + other.skipped_duplicate,
            missing=tuple(self.missing) + tuple(other.missing),
            truncated_terms=self.truncated_terms + other.truncated_terms,
        )


@dataclass
class DriftReport:
    """Count comparison between our index and the owner's snapshot.

    Shape and property names are ``stapel_core.comm.projections.DriftReport``
    verbatim — a drift report that reads differently from every other
    module's is a report nobody's tooling can consume.
    """

    name: str
    local: int
    source: int
    stale: int = 0
    missing_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def in_sync(self) -> bool:
        return self.local == self.source and self.stale == 0


# --------------------------------------------------------------------------
# document construction
# --------------------------------------------------------------------------


def _decimal(value) -> Decimal | None:
    """Decimals cross the wire as strings so a price is never rounded."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return parse_datetime(value)
    return value


def _truncate(term: str) -> tuple[str, bool]:
    if len(term) <= MAX_TERM_CHARS:
        return term, False
    return term[:MAX_TERM_CHARS], True


def _indexable(dao: Any) -> bool:
    """Whether a stored DAO may be written into a public index.

    ``stapel_attributes.visibility.is_public`` with the one thing an indexer
    owes on top of it: a stamp this library has never heard of is NOT public.
    ``normalize_visibility`` raises ``UnknownVisibility`` on a typo — the right
    answer for a writer, which can refuse — and the right answer here is to
    treat it as hidden, because the alternative to "index nothing" is
    "index a VIN because somebody wrote ``private``".
    """
    from stapel_attributes.visibility import UnknownVisibility, is_public

    try:
        return is_public(dao)
    except UnknownVisibility:
        return False


def build_facets(doc: SearchDocumentInput) -> tuple[dict, list[str], dict, int]:
    """``(facets, facet_terms, numbers, truncated)`` from the source DAOs.

    We read the mapper's raw ``features`` (full stapel-attributes DAOs), not
    the source's ``features_search`` projection, because that projection
    loses ``hex_color``'s ``simple`` axis, loses unit context, flattens lists
    and cannot tell a range from a term (spec §5.3). ``features_search``
    remains a declared fallback for a host that will not hand over DAOs —
    lossy, and the loss is reported in ``degraded[]`` rather than absorbed.

    **A value the document says is hidden is not indexed at all** — no
    ``facets`` entry, no ``facet_terms`` term, no ``SearchNumber`` row. Every
    one of the three is an exact-value oracle on its own: ``f.vin=<value>``
    matched a synthesized ``"vin=<value>"`` term, ``r.mileage=X..X`` answered
    off the numbers table, and ``facets=vin`` re-enumerated the values with
    counts. A value the catalogue marked ``owner``/``staff`` must produce
    none of them, and the cheapest place to guarantee that is here, once,
    rather than in three read paths that each have to remember.

    Two channels say a slug is hidden, and both are obeyed:

    - the DAO's own ``visibility`` stamp (stapel-attributes 0.8) — the
      authority, because it travels WITH the value and needs no schema lookup
      at index time;
    - ``doc.hidden_features`` — the producer's explicit denylist, which is the
      only channel ``features_search`` has, since that projection carries
      values and nothing else.
    """
    from .conf import search_settings

    facets: dict[str, list] = {}
    terms: list[str] = []
    numbers: dict[str, Decimal] = {}
    truncated = 0

    hidden = {str(slug) for slug in (doc.hidden_features or ())}
    features = dict(doc.features or {})
    if not features and doc.features_search and search_settings.ACCEPT_FEATURES_SEARCH:
        # The lossy path: values only, no type, so everything is a term — and
        # no stamp, so `is_public` has nothing to read. `hidden_features` is
        # the whole defence here, which is why it exists on the input rather
        # than being derived: a producer that hands over this projection is
        # already telling us it will not hand over DAOs, and asking the
        # category schema per document would put a comm call (that fails OPEN
        # when the provider is down) in the write path. A producer that
        # declares nothing is caught at read time instead — `facet_plan`
        # refuses to plan or count the slug and `search()` drops its filter.
        for slug, values in doc.features_search.items():
            if slug in hidden:
                continue
            listed = [v for v in (values or []) if v not in (None, "")]
            if not listed:
                continue
            facets[slug] = listed
            for value in listed:
                term, cut = _truncate(f"{slug}={value}")
                truncated += int(cut)
                terms.append(term)
        return facets, terms, numbers, truncated

    for slug, dao in features.items():
        if not isinstance(dao, dict):
            continue
        if slug in hidden or not _indexable(dao):
            continue
        mapping = get_facet_mapping(dao.get("type") or "")
        if mapping.kind == "skip":
            continue
        values = [v for v in mapping.extract(dao) if v not in (None, "")]
        if not values:
            continue
        facets[slug] = list(values)

        if mapping.kind == "path":
            # Every prefix of the path, which IS the rollup — and it stays
            # correct when the same child code appears under two parents,
            # because hierarchical_select only enforces sibling uniqueness.
            for depth in range(1, len(values) + 1):
                joined = "/".join(str(v) for v in values[:depth])
                term, cut = _truncate(f"{slug}={joined}")
                truncated += int(cut)
                terms.append(term)
        elif mapping.kind == "term":
            for value in values:
                term, cut = _truncate(f"{slug}={value}")
                truncated += int(cut)
                terms.append(term)

        if mapping.numeric:
            number = _decimal(values[0])
            if number is not None:
                numbers[slug] = number

    return facets, terms, numbers, truncated


def build_index_document(
    spec: SourceSpec, doc: SearchDocumentInput
) -> tuple[IndexDocument, dict, int]:
    """``SearchDocumentInput`` -> the flattened, backend-facing document."""
    from .text import fold, index_text

    facets, terms, numbers, truncated = build_facets(doc)
    text_extra = " ".join(str(v) for v in (doc.text_extra or ()) if v)
    visible = doc.status in spec.visible_statuses

    index_doc = IndexDocument(
        doc_type=doc.doc_type,
        doc_key=str(doc.doc_key),
        visible=visible,
        language=doc.language or "",
        owner_key=str(doc.owner_key or ""),
        category_path=tuple(str(p) for p in (doc.category_path or ())),
        title=doc.title or "",
        body=doc.body or "",
        text_extra=text_extra,
        text_plain=index_text(doc.title or "", text_extra, doc.body or ""),
        facets=facets,
        facet_terms=tuple(dict.fromkeys(terms)),
        numbers=numbers,
        price_base=_decimal(doc.price_base),
        published_at=_datetime(doc.published_at),
        popularity=0,
        lat=None if doc.lat is None else float(doc.lat),
        lon=None if doc.lon is None else float(doc.lon),
        geohash=doc.geohash or "",
        boost=0.0,
        promoted=False,
        card=dict(doc.card or {}),
    )
    _ = fold  # imported for the docstring's claim about a single folding path
    return index_doc, numbers, truncated


# --------------------------------------------------------------------------
# write side
# --------------------------------------------------------------------------


@transaction.atomic
def index_documents(doc_type: str, inputs: Iterable[SearchDocumentInput]) -> IndexReport:
    """Upsert documents into the table AND the engine, in one transaction.

    Idempotency and ordering are the Projection guarantees, borrowed by
    name: a redelivered ``source_event_id`` is a no-op, and a document
    carrying a ``source_seq`` below the stored one does not overwrite a
    fresher row. Both are needed because delivery is at-least-once and
    unordered, and because a rebuild can race a live event.

    Signal-owned fields (``popularity``, ``boost``, ``promoted``,
    ``promotion_expires_at``) are NOT touched here: a re-index must not undo
    a promotion, which is why the source document and the signal write
    disjoint sets of columns.
    """
    from .models import SearchDocument, SearchNumber

    spec = get_source(doc_type)
    report = IndexReport()
    pending: list[IndexDocument] = []
    removals: list[str] = []

    for doc in inputs:
        index_doc, numbers, truncated = build_index_document(spec, doc)
        report.truncated_terms += truncated
        key = index_doc.doc_key

        row = SearchDocument.objects.select_for_update().filter(
            doc_type=doc_type, doc_key=key
        ).first()

        if row is not None:
            if doc.source_event_id and row.source_event_id == doc.source_event_id:
                report.skipped_duplicate += 1
                continue
            if doc.seq and row.source_seq and doc.seq < row.source_seq:
                report.skipped_stale += 1
                continue
        if row is None:
            row = SearchDocument(doc_type=doc_type, doc_key=key)

        row.visible = index_doc.visible
        row.language = index_doc.language
        row.owner_key = index_doc.owner_key
        row.category_path = list(index_doc.category_path)
        row.title = index_doc.title
        row.body = index_doc.body
        row.text_extra = index_doc.text_extra
        row.text_plain = index_doc.text_plain
        row.facets = index_doc.facets
        row.facet_terms = list(index_doc.facet_terms)
        row.price_base = index_doc.price_base
        row.published_at = index_doc.published_at
        row.lat = None if doc.lat is None else _decimal(doc.lat)
        row.lon = None if doc.lon is None else _decimal(doc.lon)
        row.geohash = index_doc.geohash
        row.card = index_doc.card
        row.source_seq = doc.seq or row.source_seq
        row.source_event_id = doc.source_event_id or ""
        row.source_updated_at = _datetime(doc.source_updated_at)
        row.save()

        SearchNumber.objects.filter(document=row).exclude(slug__in=list(numbers)).delete()
        for slug, value in numbers.items():
            SearchNumber.objects.update_or_create(
                document=row, slug=slug, defaults={"value": value}
            )

        # Signal-owned values ride along to the engine so a Meilisearch
        # document is never a downgrade of the row it mirrors.
        pending.append(
            IndexDocument(
                **{
                    **index_doc.__dict__,
                    "popularity": row.popularity,
                    "boost": row.boost,
                    "promoted": row.promoted,
                }
            )
        )
        if not index_doc.visible:
            removals.append(key)
        report.indexed += 1

    if pending or removals:
        from .backends import get_backend

        backend = get_backend()
        if pending:
            backend.upsert(pending)
        if removals:
            # A document that left the indexed statuses is tombstoned, not
            # deleted: the row keeps the ordering token so a late event for
            # the same key cannot resurrect a stale version.
            backend.delete(doc_type, removals)
    if report.indexed or removals:
        # The type-ahead's per-category counts are an aggregate over this
        # table, cached. A TTL alone would be correct but slow to notice: a
        # freshly seeded stand must not offer a dropdown of zeros, and a
        # category that has just emptied must not keep advertising stock.
        from .suggest import invalidate_counts

        invalidate_counts(doc_type)
    return report


@transaction.atomic
def remove_documents(doc_type: str, keys: Iterable[str]) -> IndexReport:
    """Tombstone documents: invisible immediately, purged by the beat later."""
    from .models import SearchDocument

    listed = [str(k) for k in keys]
    if not listed:
        return IndexReport()
    updated = SearchDocument.objects.filter(doc_type=doc_type, doc_key__in=listed).update(
        visible=False, indexed_at=timezone.now()
    )
    from .backends import get_backend

    get_backend().delete(doc_type, listed)
    if updated:
        from .suggest import invalidate_counts

        invalidate_counts(doc_type)
    return IndexReport(removed=updated)


@transaction.atomic
def reassign_owner(from_key, into_key) -> int:
    """Re-point every indexed document from one owner onto another.

    The merge counterpart of :func:`remove_documents`, and deliberately NOT a
    re-pull. The index is derived data, but a merge changes exactly one
    indexed thing — who owns the document — and the row already holds every
    other field. Two reasons the cheap answer is also the correct one:

    * ``ingest`` would re-read the source, which may not have processed its
      own merge yet, and would then re-stamp the guest's id back onto a row
      this handler had just corrected. No source emits a per-document signal
      after a bulk reassignment, so nothing would ever come along to fix it;
    * ``ingest`` treats "absent from the source's answer" as *delete*. A
      transport hiccup during a merge must not tombstone a person's
      documents.

    It IS a re-index rather than an UPDATE, though: the index has two halves,
    and rewriting only the table would leave the engine's copy of every
    document still filed under an account that can no longer sign in. Each
    touched row is pushed to the engine through the same
    :func:`_row_to_index_document` path a signal write uses.

    Idempotent: a redelivery matches no rows and reports ``0``.
    """
    from .models import SearchDocument

    source, target = str(from_key), str(into_key)
    if not source or source == target:
        return 0
    rows = list(SearchDocument.objects.filter(owner_key=source))
    if not rows:
        return 0
    SearchDocument.objects.filter(owner_key=source).update(
        owner_key=target, indexed_at=timezone.now()
    )
    for row in rows:
        row.owner_key = target
        _reupsert(row)
    return len(rows)


def pull_documents(doc_type: str, keys: Iterable[str]) -> dict[str, dict]:
    """Fetch documents through the source's content Function.

    Event -> pull, not event-carried document: the ``listing.*`` payloads are
    ``additionalProperties: false`` and carry identity only, and fattening
    them would put every listing's text and PII on a durable bus for every
    subscriber. The same ``call()`` runs in-process in a monolith and over
    the bus in a split — that is the whole point of the seam.
    """
    from stapel_core.comm import call

    spec = get_source(doc_type)
    listed = [str(k) for k in keys]
    if not listed:
        return {}
    result = call(spec.content_function, {"keys": listed})
    return result if isinstance(result, dict) else {}


def ingest(doc_type: str, keys: Iterable[str], *, event_id: str = "", seq: int = 0) -> IndexReport:
    """Pull *keys* and index them; keys the source no longer has are removed.

    "Absent from the answer" is the source's way of saying "no document"
    (``projections.read()`` semantics, restated in stapel-listings 0.4.0):
    a soft-deleted listing is simply not in the batch, and for the index
    that means delete.
    """
    spec = get_source(doc_type)
    listed = [str(k) for k in keys]
    documents = pull_documents(doc_type, listed)

    inputs: list[SearchDocumentInput] = []
    for key in listed:
        payload = documents.get(key)
        if payload is None:
            continue
        mapped = spec.mapper({**payload, "key": key})
        if mapped is None:
            continue
        if not isinstance(mapped, SearchDocumentInput):
            raise TypeError(
                f"source {doc_type!r} mapper returned {type(mapped)!r}, expected "
                "SearchDocumentInput"
            )
        enriched = _with_event_metadata(mapped, event_id=event_id, seq=seq)
        inputs.append(_with_category_path(enriched))

    missing = [key for key in listed if key not in documents]
    report = index_documents(doc_type, inputs) if inputs else IndexReport()
    if missing:
        report = report.merge(remove_documents(doc_type, missing))
        report.missing = tuple(missing)
    return report


def _with_event_metadata(doc: SearchDocumentInput, *, event_id: str, seq: int):
    from dataclasses import replace

    return replace(
        doc,
        source_event_id=doc.source_event_id or event_id,
        seq=doc.seq or seq,
    )


def _with_category_path(doc: SearchDocumentInput) -> SearchDocumentInput:
    """Fill ``category_path`` from stapel-categories when the mapper did not."""
    if doc.category_path or not doc.category_id:
        return doc
    from dataclasses import replace

    from .facets import category_path

    return replace(doc, category_path=category_path(doc.category_id))


# --------------------------------------------------------------------------
# signals (the one inbound door for promotion / popularity)
# --------------------------------------------------------------------------


@transaction.atomic
def apply_signal(payload: dict, *, event_id: str = "") -> bool:
    """Apply one ``search.signal``. Last writer wins, by event position."""
    from .models import SearchDocument, SearchSignal

    doc_type = payload.get("doc_type")
    doc_key = payload.get("doc_key")
    if not doc_type or doc_key in (None, ""):
        logger.error("search.signal without doc_type/doc_key: %s", event_id)
        return False
    doc_key = str(doc_key)

    if event_id and SearchSignal.objects.filter(event_id=event_id).exists():
        return False

    row = SearchDocument.objects.select_for_update().filter(
        doc_type=doc_type, doc_key=doc_key
    ).first()
    expires_at = _datetime(payload.get("expires_at"))

    recorded = False
    for kind in (SearchSignal.KIND_BOOST, SearchSignal.KIND_POPULARITY, SearchSignal.KIND_PROMOTED):
        if kind not in payload:
            continue
        value = payload[kind]
        SearchSignal.objects.create(
            doc_type=doc_type,
            doc_key=doc_key,
            kind=kind,
            value=float(bool(value)) if kind == SearchSignal.KIND_PROMOTED else float(value),
            expires_at=expires_at,
            event_id=event_id,
        )
        recorded = True
        if row is None:
            continue
        if kind == SearchSignal.KIND_BOOST:
            row.boost = max(-1.0, min(5.0, float(value)))
        elif kind == SearchSignal.KIND_POPULARITY:
            row.popularity = int(value)
        else:
            row.promoted = bool(value)

    if row is not None and recorded:
        row.promotion_expires_at = expires_at
        row.save(
            update_fields=["boost", "popularity", "promoted", "promotion_expires_at", "indexed_at"]
        )
        _reupsert(row)
    return recorded


def _reupsert(row) -> None:
    """Push a row's current state to the engine without re-pulling the source."""
    from .backends import get_backend

    get_backend().upsert([_row_to_index_document(row)])


def _row_to_index_document(row) -> IndexDocument:
    return IndexDocument(
        doc_type=row.doc_type,
        doc_key=row.doc_key,
        visible=row.visible,
        language=row.language,
        owner_key=row.owner_key,
        category_path=tuple(row.category_path or ()),
        title=row.title,
        body=row.body,
        text_extra=row.text_extra,
        text_plain=row.text_plain,
        facets=dict(row.facets or {}),
        facet_terms=tuple(row.facet_terms or ()),
        numbers={n.slug: n.value for n in row.numbers.all()},
        price_base=row.price_base,
        published_at=row.published_at,
        popularity=row.popularity,
        lat=None if row.lat is None else float(row.lat),
        lon=None if row.lon is None else float(row.lon),
        geohash=row.geohash,
        boost=row.boost,
        promoted=row.promoted,
        card=dict(row.card or {}),
    )


def expire_signals() -> int:
    """Drop promotions past their expiry. Beat: ``search_expire_signals``."""
    from .models import SearchDocument

    now = timezone.now()
    expired = list(
        SearchDocument.objects.filter(
            promotion_expires_at__isnull=False, promotion_expires_at__lte=now
        ).filter(promoted=True)
    )
    extra = list(
        SearchDocument.objects.filter(
            promotion_expires_at__isnull=False, promotion_expires_at__lte=now, boost__gt=0.0
        ).exclude(pk__in=[row.pk for row in expired])
    )
    rows = expired + extra
    for row in rows:
        row.promoted = False
        row.boost = 0.0
        row.promotion_expires_at = None
        row.save(update_fields=["promoted", "boost", "promotion_expires_at", "indexed_at"])
        _reupsert(row)
    return len(rows)


def purge_tombstones(days: int | None = None) -> int:
    """Delete invisible rows older than the retention window."""
    from .conf import search_settings
    from .models import SearchDocument

    horizon = timezone.now() - timedelta(
        days=int(days if days is not None else search_settings.TOMBSTONE_RETENTION_DAYS)
    )
    deleted, _ = SearchDocument.objects.filter(visible=False, indexed_at__lt=horizon).delete()
    return deleted


# --------------------------------------------------------------------------
# rebuild / drift / catch-up
# --------------------------------------------------------------------------


def _snapshot_pages(spec: SourceSpec, batch_size: int):
    """Page the owner's export Function.

    Contract is ``stapel_core.comm.projections._iter_snapshot`` verbatim:
    called with ``{"cursor": <opaque|None>, "limit": n}``, answering
    ``{"rows": [...], "cursor": <next|None>, "total": <int|None>}``.
    """
    from stapel_core.comm import call

    cursor = None
    while True:
        page = call(spec.export_function, {"cursor": cursor, "limit": batch_size})
        rows = page.get("rows") or []
        yield rows, page.get("total")
        cursor = page.get("cursor")
        if not cursor:
            return


def rebuild(doc_type: str, *, batch_size: int = 500) -> IndexReport:
    """Rebuild the whole index for *doc_type* from the source of truth."""
    spec = get_source(doc_type)
    report = IndexReport()
    seen: set[str] = set()

    for rows, _total in _snapshot_pages(spec, batch_size):
        inputs: list[SearchDocumentInput] = []
        for row in rows:
            key = str(row.get("key") or "")
            if not key:
                continue
            seen.add(key)
            mapped = spec.mapper({**row, "key": key})
            if mapped is None:
                continue
            mapped = _with_event_metadata(mapped, event_id="", seq=int(row.get("seq") or 0))
            inputs.append(_with_category_path(mapped))
        if inputs:
            report = report.merge(index_documents(doc_type, inputs))

    from .models import SearchDocument

    stale_keys = list(
        SearchDocument.objects.filter(doc_type=doc_type, visible=True)
        .exclude(doc_key__in=seen)
        .values_list("doc_key", flat=True)
    )
    if stale_keys:
        report = report.merge(remove_documents(doc_type, stale_keys))
    logger.info("search rebuild %s: %s", doc_type, report)
    return report


def drift_check(doc_type: str, *, batch_size: int = 500) -> DriftReport:
    """Compare the index against the owner's snapshot, without rewriting it."""
    from .models import SearchDocument

    spec = get_source(doc_type)
    source_rows: dict[str, Any] = {}
    total = 0
    for rows, reported_total in _snapshot_pages(spec, batch_size):
        for row in rows:
            key = str(row.get("key") or "")
            if key:
                source_rows[key] = row.get("seq") or 0
        if reported_total is not None:
            total = reported_total
    if not total:
        total = len(source_rows)

    local_rows = dict(
        SearchDocument.objects.filter(doc_type=doc_type).values_list("doc_key", "source_seq")
    )
    missing = tuple(sorted(set(source_rows) - set(local_rows)))
    stale = sum(1 for key, seq in source_rows.items() if local_rows.get(key, -1) < seq)
    return DriftReport(
        name=doc_type,
        local=len(local_rows),
        source=total,
        stale=stale,
        missing_keys=missing,
    )


def reindex_stale(doc_type: str, *, limit: int = 500) -> IndexReport:
    """Re-pull the documents the source has moved on from.

    The safety net for a lost event, not a crutch against the source:
    stapel-listings 0.4.0 rebuilds ``features_search`` on every save and
    emits ``listing.updated`` from the real edit paths, so this should find
    nothing on a healthy deployment — and a beat job that always finds
    something is a signal worth reading.
    """
    from .models import SearchDocument

    keys = list(
        SearchDocument.objects.filter(doc_type=doc_type)
        .filter(source_updated_at__isnull=False)
        .filter(source_updated_at__gt=timezone.now() - timedelta(days=3650))
        .order_by("indexed_at")
        .values_list("doc_key", flat=True)[:limit]
    )
    if not keys:
        return IndexReport()
    return ingest(doc_type, keys)


def reconcile(doc_type: str, *, batch_size: int = 500) -> IndexReport:
    """Sweep every VISIBLE row against the source of truth; drop the ghosts.

    The deterministic answer to a write that never became an event — a
    queryset ``.update(status=...)``, a raw save on an emitter too old to
    guard its own boundary, a lost delivery. Each visible row's key is
    re-pulled through the same :func:`ingest` path a live signal uses, so the
    pulled document's status decides (``visible_statuses``), and a key the
    source no longer serves is removed — no second predicate, no special
    cases.

    Differs from :func:`rebuild` (which replays the source's whole snapshot —
    heavier, and also *adds* missing documents) and from
    :func:`reindex_stale` (the rolling beat catch-up over a bounded batch):
    this one asks exactly one question — "is everything the index still
    SHOWS actually there?" — which is the question a ghost card fails.

    Keyset-paged by ``doc_key`` so tombstoning a row mid-sweep cannot shift
    the cursor under the reader.
    """
    from .models import SearchDocument

    report = IndexReport()
    last_key = ""
    while True:
        keys = list(
            SearchDocument.objects.filter(doc_type=doc_type, visible=True)
            .filter(doc_key__gt=last_key)
            .order_by("doc_key")
            .values_list("doc_key", flat=True)[: max(1, int(batch_size))]
        )
        if not keys:
            break
        report = report.merge(ingest(doc_type, keys))
        last_key = keys[-1]
    logger.info("search reconcile %s: %s", doc_type, report)
    return report


def apply_settings(doc_type: str) -> None:
    """Push the engine-side schema and the dictionary halves it owns."""
    from .backends import get_backend
    from .conf import search_settings
    from .text import load_dictionary

    languages = sorted(set(search_settings.FTS_CONFIGS or {}))
    synonyms: list[tuple[str, ...]] = []
    stopwords: set[str] = set()
    for language in languages:
        dictionary = load_dictionary(language)
        synonyms.extend(dictionary.equivalents)
        stopwords.update(dictionary.stopwords)

    get_backend().apply_settings(
        doc_type,
        IndexSettings(
            doc_type=doc_type,
            synonyms=tuple(synonyms),
            stopwords=tuple(sorted(stopwords)),
        ),
    )


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------


def _honest_count(result, *, offset: int, shown: int) -> tuple[int | None, bool]:
    """``(count, count_is_lower_bound)`` — a count the page cannot disprove.

    The invariant, enforced here for EVERY backend rather than trusted to
    each one: the answer may never claim fewer matches than the reader can
    already see. ``count: 0`` beside a non-empty ``items`` is the shape this
    exists to make impossible — the storefront printed «Примерно 0
    объявлений» over four cards, which is not an approximation, it is a
    contradiction.

    So the floor is what this page proves: the rows before it (the cursor's
    offset), the rows on it, and one more when ``has_next`` says another
    exists. A backend number below that floor is replaced by the floor and
    marked a lower bound; ``None`` from a backend that genuinely cannot
    count stays ``None`` only while nothing has been seen — past that, "at
    least what you can see" is knowledge, and zero was never the honest way
    to spell "unknown".
    """
    seen = offset + shown + (1 if result.has_next else 0)
    total = result.total
    if total is None:
        return (seen, True) if seen else (None, False)
    total = int(total)
    if total < seen:
        return seen, True
    return total, bool(getattr(result, "total_is_lower_bound", False))


def _degradations(capabilities, q, facet_result, path_degraded: str, exact_total: bool) -> tuple[str, ...]:
    """What the caller asked for that this engine could not deliver.

    Nothing is written to a log and forgotten. A frontend that cannot see
    the shortfall renders a confident wrong answer — the ``email_mock``
    failure one layer down, where the UI said "code sent" and nothing had
    been sent.
    """
    degraded: list[str] = []
    if q.text is not None and not q.text.is_empty:
        if not capabilities.typo_tolerance:
            degraded.append("typo_tolerance")
        # Reported only when this ANSWER actually lost something, not
        # whenever the engine class lacks the feature. Query-side expansion
        # runs on every backend, so `iphone -> айфон` is substituted even on
        # Postgres; what an engine without phrase synonyms cannot do is
        # match a MULTI-WORD group member («бывший в употреблении») as a
        # phrase. Keying on the capability alone put a yellow «Синонимы не
        # подставлялись» over every SERP, on every query, for every buyer —
        # a sentence that was also false.
        if not capabilities.phrase_synonyms and q.text.multiword_expansions:
            degraded.append("phrase_synonyms")
    # Per ANSWER, not per engine: an engine that cannot always count exactly
    # still counts a small candidate set exactly, and reporting `exact_total`
    # as degraded over an exact number teaches a frontend to distrust a
    # number that is right.
    if not exact_total:
        degraded.append("exact_total")
    if facet_result is not None and facet_result.approximate:
        degraded.append("exact_facet_counts")
    if path_degraded:
        degraded.append("category_rollup")
    for slug in q.scorers:
        if slug not in capabilities.supported_scorers:
            degraded.append(f"scorer:{slug}")
    return tuple(dict.fromkeys(degraded))


def _drop_hidden_filters(q, plan) -> tuple[Any, tuple[str, ...]]:
    """Strip ``f.<slug>``/``r.<slug>`` filters on slugs the category hides.

    The belt to :func:`build_facets`' braces. The writer is the real fix — a
    hidden value never enters the index, so there is nothing to match — but
    every document indexed BEFORE that fix still carries its terms and its
    ``SearchNumber`` rows, and an exact-match filter over them is a working
    oracle: ``?f.vin=<value>`` answering one hit confirms which listing is
    that car. The read path therefore refuses the filter as well, and keeps
    refusing it for documents nobody has reindexed yet.

    Dropped, not 400'd: the slug is a legitimate attribute the caller may well
    have got from an older panel, and a refusal teaches a frontend to retry.
    Dropped loudly, though — the slugs come back in
    ``facet_meta.dropped_filters``, because a filter the server silently
    ignored is the most expensive kind of wrong answer (``query`` module
    docstring), and here it is also the WIDER one: the answer without the
    filter is a superset, never a leak.

    **Cross-category queries.** With no ``category`` there is no plan, so
    ``plan.hidden`` is empty and nothing is dropped. That is accepted rather
    than papered over: visibility is a property of a FeatureDef, which is a
    property of a CATEGORY — the same slug can be public in one branch and
    hidden in another — so a fleet-wide slug→visibility map does not exist
    and could not be right if it did. What closes the case is the writer:
    after ``python manage.py search_rebuild --type <doc_type>`` there is no
    term and no number for a hidden slug in any document, so the filter
    matches nothing regardless of which category it was aimed at. Until that
    rebuild runs, a cross-category query is the one hole, and it is named
    here rather than hidden in a passing test.
    """
    from dataclasses import replace

    hidden = set(plan.hidden)
    if not hidden:
        return q, ()
    dropped = sorted(
        {slug for slug in q.facets if slug in hidden}
        | {r.slug for r in q.ranges if r.slug in hidden}
    )
    if not dropped:
        return q, ()
    return (
        replace(
            q,
            facets={s: v for s, v in q.facets.items() if s not in hidden},
            ranges=tuple(r for r in q.ranges if r.slug not in hidden),
        ),
        tuple(dropped),
    )


def search(params, *, accept_language: str = "") -> dict:
    """Run one query and shape the answer. The module's whole read path."""
    from .backends import get_backend
    from .errors import SearchBackendUnavailable
    from .facets import (
        facet_plan,
        fill_zero_options,
        path_degradation,
        reset_path_degradation,
        vocabulary_labels,
    )
    from .models import SearchDocument
    from .query import encode_cursor, parse_facet_selection, parse_query

    started = timezone.now()
    reset_path_degradation()
    q = parse_query(params, accept_language=accept_language)
    backend = get_backend()

    # The plan is built BEFORE the engine sees the query, because it is what
    # says which slugs are not filterable: a hidden slug's filter has to be
    # gone by the time an engine could answer with it.
    requested = parse_facet_selection(params)
    plan = facet_plan(
        q.category_path[-1] if q.category_path else None, requested=requested
    )
    q, dropped_filters = _drop_hidden_filters(q, plan)

    try:
        capabilities = backend.capabilities()
        result = backend.query(q)
    except Exception as exc:  # noqa: BLE001 - any engine failure is a 503, not a 500
        logger.exception("search backend %s failed", getattr(backend, "name", "?"))
        raise SearchBackendUnavailable(str(exc)) from exc

    facet_result = None
    if plan.slugs and capabilities.facet_counts:
        facet_result = backend.facets(q, plan)

    keys = [hit.key for hit in result.hits]
    rows = {
        row.doc_key: row
        for row in SearchDocument.objects.filter(doc_type=q.doc_type, doc_key__in=keys)
    }

    items = []
    for hit in result.hits:
        row = rows.get(hit.key)
        items.append(
            {
                "key": hit.key,
                "score": hit.score,
                # DSA Art. 26: present on EVERY item, under every sort,
                # including when it is false. The serializer cannot omit it.
                "promoted": bool(row.promoted) if row is not None else False,
                "distance_km": hit.distance_km,
                "card": dict(row.card or {}) if row is not None else {},
            }
        )

    next_anchor = None
    prev_anchor = None
    offset = q.cursor.offset if q.cursor is not None else 0
    if result.hits:
        if result.has_next:
            last = result.hits[-1]
            next_anchor = encode_cursor(
                Cursor(sort_value=last.sort_value, doc_key=last.key, offset=offset + len(items))
            )
        if offset > 0:
            first = result.hits[0]
            prev_anchor = encode_cursor(
                Cursor(
                    sort_value=first.sort_value,
                    doc_key=first.key,
                    offset=max(0, offset - q.limit),
                )
            )

    counts = fill_zero_options(facet_result.counts, plan) if facet_result else {}
    count, count_is_lower_bound = _honest_count(result, offset=offset, shown=len(items))
    exact_total = bool(result.exact_total and count is not None and not count_is_lower_bound)
    # A vocabulary-backed slug's caption is resolved from the codes the query
    # actually counted, so it can only be added here. `translatable` is False
    # for these without asking: a vocabulary term's label is literal text an
    # owner curated, never a translation key — that is the difference between
    # a vocabulary and an inline option list.
    facet_labels: dict[str, dict] = {
        slug: {
            "translatable": bool(plan.translatable_labels.get(slug, True)),
            "values": values,
        }
        for slug, values in plan.option_labels.items()
    }
    for slug, values in vocabulary_labels(plan, counts).items():
        facet_labels[slug] = {"translatable": False, "values": values}
    return {
        "items": items,
        "facets": counts,
        "facet_labels": facet_labels,
        "facet_meta": {
            "approximate": bool(facet_result.approximate) if facet_result else False,
            "candidates": facet_result.candidates if facet_result else 0,
            "counted": list(plan.slugs) if facet_result else [],
            "skipped": list(plan.skipped),
            # Filters the caller sent that this answer did NOT apply, because
            # the category marks the slug non-public. Never silent: the answer
            # is wider than what was asked for, and a panel that cannot see
            # that renders a confident wrong narrowing.
            "dropped_filters": list(dropped_filters),
            "core_ranges": list(plan.core_ranges),
        },
        "next_anchor": next_anchor,
        "prev_anchor": prev_anchor,
        "has_next": result.has_next,
        "has_prev": offset > 0,
        # `count` is nullable and `count_is_lower_bound` says how to read it:
        # a floor renders as "N+", never as "N", and `null` renders as no
        # count line at all. Zero beside items is unreachable by construction.
        "count": count,
        "count_is_lower_bound": count_is_lower_bound,
        "exact_total": exact_total,
        # Deduplicated across the three layers that contribute, not just
        # within each: `_degradations` derives a shortfall from
        # `capabilities()` while a backend may report the same one from the
        # branch it actually took, and the concatenation shipped
        # `["phrase_synonyms", "phrase_synonyms"]` on every query with text.
        # The frontend deduped it on arrival, which is exactly why nobody
        # saw it until a stand was read by hand.
        "degraded": list(
            dict.fromkeys(
                _degradations(
                    capabilities, q, facet_result, path_degradation(), exact_total
                )
                + tuple(result.degraded)
                + tuple(facet_result.degraded if facet_result else ())
            )
        ),
        "backend": getattr(backend, "name", "unknown"),
        # Which dictionary and analyzer configuration answered. Resolved from
        # `lang`, then `Accept-Language`, then DEFAULT_LANGUAGE — and when
        # that fallback is wrong the whole synonym layer silently does not
        # apply: on a live stand `айфон` found 2 and `iphone` found 15,
        # because no header reached the service and the ru dictionary was
        # never loaded. Stating it makes that visible from the answer.
        "language": q.language,
        "sort": q.sort,
        "took_ms": int((timezone.now() - started).total_seconds() * 1000),
    }


def suggest(params, *, accept_language: str = "") -> dict:
    """What to offer under the search box: CATEGORIES first, then terms.

    A classified's type-ahead is a navigation control. «шорты» is not one
    destination but three — men's, women's, children's — and the buyer picks
    between them by the ancestor path and by how many live listings are
    behind each. So ``categories`` is the primary half of the answer and
    carries the full path plus a count that is the SERP's count; ``terms``
    is the 0.1.0 title-prefix half, which remains useful and remains second.

    Neither half comes from a query log: none is kept, which is a privacy
    decision before it is a product one (spec §15).

    ``type`` is optional here, unlike on ``query``. A deployment with one
    registered document type has one answer, and requiring a storefront to
    name it in every keystroke is ceremony that can only be got wrong; with
    several registered types the parameter is required again, because
    guessing which corpus a buyer meant is not something this module can do.
    """
    from .backends import get_backend
    from .conf import search_settings
    from .errors import ERR_400_UNKNOWN_DOC_TYPE, SearchValidationError
    from .query import resolve_language
    from .registry import get_sources
    from .suggest import suggest_categories

    sources = get_sources()
    doc_type = str(params.get("type") or "").strip()
    if not doc_type and len(sources) == 1:
        doc_type = next(iter(sources))
    if not doc_type or doc_type not in sources:
        raise SearchValidationError(ERR_400_UNKNOWN_DOC_TYPE, doc_type=doc_type)

    prefix = str(params.get("q") or "").strip()
    if len(prefix) > int(search_settings.MAX_QUERY_CHARS):
        from .errors import ERR_400_QUERY_TOO_LONG

        raise SearchValidationError(ERR_400_QUERY_TOO_LONG)
    try:
        limit = max(
            1,
            min(
                int(params.get("limit") or search_settings.DEFAULT_SUGGEST_LIMIT),
                int(search_settings.MAX_SUGGEST_LIMIT),
            ),
        )
    except (TypeError, ValueError):
        limit = int(search_settings.DEFAULT_SUGGEST_LIMIT)

    language = resolve_language(params, accept_language=accept_language)

    backend = get_backend()
    # The backend rides along for the goods-driven fallback: when no
    # category NAME matches, the engine that runs the SERP is asked which
    # categories hold matching documents (an optional verb — see
    # suggest.py's module docstring for why that half crosses the seam).
    categories, degraded = (
        suggest_categories(doc_type, prefix, language=language, limit=limit, backend=backend)
        if prefix
        else ([], [])
    )
    if prefix:
        # The vector net, below the deterministic floor (vector/integration
        # .py). Flag off — the default — returns the same objects untouched.
        from .vector import augment_category_suggestions

        categories, degraded = augment_category_suggestions(
            categories,
            degraded,
            doc_type=doc_type,
            q=prefix,
            language=language,
            limit=limit,
        )
    terms = backend.suggest(doc_type, prefix, limit=limit) if prefix else []
    return {
        "categories": categories,
        "terms": terms,
        # The 0.1.0 name for `terms`, kept for one minor so a storefront
        # already reading it is not broken by an answer that grew. Removing
        # a field a live frontend reads is a deletion, and a deletion gets
        # its own release note rather than riding along with a feature.
        "items": terms,
        "language": language,
        "degraded": degraded,
        "backend": getattr(backend, "name", "unknown"),
    }


def health() -> dict:
    """Backend reachability, capabilities and how far behind the index is."""
    from dataclasses import asdict

    from .backends import get_backend
    from .models import SearchDocument
    from .registry import get_sources

    backend = get_backend()
    try:
        status = backend.health()
        capabilities = asdict(backend.capabilities())
        capabilities["supported_scorers"] = sorted(capabilities["supported_scorers"])
    except Exception as exc:  # noqa: BLE001
        return {
            "backend": getattr(backend, "name", "unknown"),
            "reachable": False,
            "detail": str(exc),
            "types": sorted(get_sources()),
        }

    newest = (
        SearchDocument.objects.filter(visible=True)
        .order_by("-indexed_at")
        .values_list("indexed_at", flat=True)
        .first()
    )
    lag_seconds = None if newest is None else max(0, int((timezone.now() - newest).total_seconds()))
    return {
        "backend": status.name,
        "reachable": status.reachable,
        "detail": status.detail,
        "documents": status.documents,
        "capabilities": capabilities,
        "types": sorted(get_sources()),
        "lag_seconds": lag_seconds,
        "stale_reason": _stale_reason(),
    }


def _stale_reason() -> str:
    """Why the index may be behind, in words a reader can act on."""
    from .facets import path_degradation

    reasons = []
    if path_degradation():
        reasons.append("category_path_unavailable")
    from .models import SearchDocument

    if not SearchDocument.objects.exists():
        reasons.append("index_empty")
    return ",".join(reasons)


__all__ = [
    "DriftReport",
    "IndexReport",
    "MAX_TERM_CHARS",
    "apply_settings",
    "apply_signal",
    "build_facets",
    "build_index_document",
    "drift_check",
    "expire_signals",
    "index_documents",
    "ingest",
    "pull_documents",
    "purge_tombstones",
    "reassign_owner",
    "rebuild",
    "reindex_stale",
    "remove_documents",
    "search",
    "suggest",
    "health",
]
