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
from dataclasses import dataclass, field, replace
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


def _numeric_code(values: list) -> Decimal | None:
    """A TERM value that is also a number, so a choice can be a from/to.

    The catalogue calls `year` a choice — on an imported leaf it is a
    vocabulary of numeric codes, and `floor` and `doors` are inline option
    lists of them — while a buyer calls all three a range. The type says
    nothing about it: the same `2015` is a `ref_select` code here and an
    `int` one category over, and only one of the two answered `r.year=2015..`
    before this.

    A number is written only for a SINGLE scalar value: a multi-value axis
    has no one number to bound, and ``bool`` is `False == 0`, which is a term
    and never a bound. The term is still written — an axis can be both, and
    ``facets=year`` keeps counting buckets while ``r.year`` gets its bounds.
    """
    if len(values) != 1:
        return None
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    return _decimal(value)


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
            # The projection carries no type, so the VALUE decides: a single
            # scalar that parses as a number is a number. Until 0.14.7 this
            # branch wrote no numbers at all, which is why a live stand held
            # 0 rows in `search_number` while every listing carried a year
            # and a mileage — the range half of the panel was empty by
            # construction, not by configuration, on every producer that
            # hands over this projection instead of DAOs.
            number = _numeric_code(listed)
            if number is not None:
                numbers[slug] = number
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
        elif mapping.kind == "term":
            # A vocabulary-backed or inline choice whose CODE is a number
            # (`year`, `floor`, `doors`). Not `path`: a root->leaf address is
            # not a magnitude, and its segments are rolled up by prefix.
            number = _numeric_code(values)
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


def _range_payload(bounds) -> tuple[dict[str, dict], dict[str, int]]:
    """``{slug: (low, high[, documents])}`` -> the payload and the coverages.

    Numbers, not strings. A price crosses the wire as a string on the CARD
    because a written price must not be rounded on its way to a human; a
    slider END is arithmetic the client does immediately, and handing it
    `"2015"` to parse buys nothing. Integral values render integral, so a
    year is `2015` and an engine volume is `1.4`.

    The second half of the answer is ``{slug: documents}`` — how many
    candidates carry a number on that axis — and it is separate from the
    payload because it is not something a client is owed: it is what
    :func:`_withheld_ranges` measures the coverage floor with, and a slug
    withheld by it never reaches the payload at all. A backend still
    answering two values simply contributes no entry here, and an axis whose
    coverage cannot be measured is never withheld for it.
    """
    payload: dict[str, dict] = {}
    coverage: dict[str, int] = {}
    for slug, entry in (bounds or {}).items():
        low, high = entry[0], entry[1]
        if low is None or high is None:
            continue
        payload[str(slug)] = {"min": _number(low), "max": _number(high)}
        if len(entry) > 2 and entry[2] is not None:
            coverage[str(slug)] = int(entry[2])
    return payload, coverage


#: Why an axis the plan admitted did not reach the panel. A closed set: a
#: client that branches on it has these three and nothing else, and an
#: unknown value means it is older than the server.
WITHHELD_COVERAGE = "coverage"
WITHHELD_UNLABELLED = "unlabelled"
WITHHELD_REASONS = (WITHHELD_COVERAGE, WITHHELD_UNLABELLED)

#: Which half of the panel a withheld row is about. One slug can be both: an
#: imported `year` is a bucket list AND a from/to, and the two are measured
#: by different quantities over the same page (the sum of the buckets against
#: the documents carrying a number). So a row names its half rather than
#: leaving a reader to assume that a withheld slug means the axis is gone.
AXIS_GROUP = "group"
AXIS_RANGE = "range"


def _range_label(slug: str, plan) -> tuple[str | None, bool, str | None]:
    """``(label, translatable, unit)`` for one range axis.

    The SAME source a facet group's heading comes from — the plan's
    ``group_labels``, folded out of the category's own feature definitions —
    because a group and a range are two ways of narrowing one authored
    feature and nothing about the axis being numeric changes who named it. A
    core range has no definition to read and takes its caption from
    :data:`~stapel_search.index_schema.CORE_RANGE_LABELS`, which is this
    library's, because the axis is this library's.

    ``None`` for the label means the definition carries no name — which is
    what made a live chip row read `doors`, `kilometrage`, `engine_volume`:
    the answer shipped the bounds and nothing to write above them, and every
    client that had no category schema in hand printed the storage slug at a
    reader. It is now the condition for withholding the axis, and the
    difference between "no name" and "a name this module invented out of the
    slug" is the whole reason the fold refuses to fabricate one.
    """
    from .index_schema import CORE_RANGE_LABELS

    if slug in CORE_RANGE_LABELS:
        label, translatable = CORE_RANGE_LABELS[slug]
        return label, bool(translatable), None
    name, translatable = plan.group_labels.get(slug, (None, False))
    return name, bool(translatable), plan.units.get(slug)


def _withheld_ranges(payload, coverage, plan, q, candidates: int) -> list[dict]:
    """The range axes that were bounded and must not be offered anyway.

    Two rules, and the first one is new in 0.16.0 while the second is the
    facet groups' own rule finally applied to the other half of the panel.

    ``unlabelled`` — the axis has no caption from any source, so a client can
    only print the slug above it. An axis a reader cannot name is not a
    filter; it is a control whose meaning the reader has to infer from the
    numbers in it.

    ``coverage`` — fewer than ``FACET_MIN_COVERAGE`` of the candidates carry
    a number on the axis. This is the measure ``facet_meta.withheld`` has
    used for bucket lists since 0.14.9, applied to the measurement half:
    a phones leaf shipped «Вес (Для Доставки), кг», «Количество в фасовке»
    and four more wholesale axes over a handful of the fifty-two listings on
    the page, and a from/to picker over three documents narrows nothing while
    taking exactly as much of the rail as a real one.

    Two exemptions carry over from the group rule verbatim — an axis the
    reader has already FILTERED on is never withheld (that would leave the
    filter applied with no control to undo it), and nothing is withheld when
    the floor is 0 — and one does not. A group the QUERIED CATEGORY authored
    is exempt there, because a closed option set answering with its zeros is
    a shipped decision; a range has no zeros to answer with and no option set
    to have decided about, so authorship says nothing about whether the axis
    describes this page. What decides that is the count, and the count is the
    same number either way.

    An axis whose engine did not report a count is not withheld for coverage:
    the floor cannot establish "describes too little" from a number it does
    not have, which is the same exemption a bucket list capped at its bucket
    limit already gets.
    """
    from .conf import search_settings

    floor = float(search_settings.FACET_MIN_COVERAGE)
    filtered = {spec.slug for spec in (q.ranges or ())}
    core = set(plan.core_ranges)
    withheld: list[dict] = []
    for slug in payload:
        if slug in filtered:
            continue
        label, _translatable, _unit = _range_label(slug, plan)
        if not label:
            withheld.append(
                {"slug": slug, "axis": AXIS_RANGE, "reason": WITHHELD_UNLABELLED}
            )
            continue
        # A core range addresses a column every document in every corpus has
        # and is announced unconditionally (`core_ranges`); measuring its
        # share of the page and then removing it would contradict the
        # announcement in the same answer.
        if slug in core or floor <= 0 or candidates <= 0 or slug not in coverage:
            continue
        documents = coverage[slug]
        if documents < floor * candidates:
            withheld.append(
                {
                    "slug": slug,
                    "axis": AXIS_RANGE,
                    "reason": WITHHELD_COVERAGE,
                    "coverage": documents,
                    "candidates": candidates,
                }
            )
    return withheld


def _range_meta(payload, plan) -> dict[str, dict]:
    """The payload with every entry's caption, unit and panel position on it.

    Called after the withholding pass, so every entry left here has a label
    by construction — the answer never carries a range a client would have to
    print a slug above.
    """
    out: dict[str, dict] = {}
    for slug, bounds in payload.items():
        label, translatable, unit = _range_label(slug, plan)
        entry = {
            **bounds,
            "label": label,
            "label_translatable": bool(translatable),
            "order": plan.order.get(slug),
        }
        if unit:
            entry["unit"] = unit
        out[slug] = entry
    return out


def _number(value):
    """A Decimal as the narrowest JSON number that loses nothing."""
    number = Decimal(str(value))
    integral = number == number.to_integral_value()
    return int(number) if integral else float(number)


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


def _apply_extraction(q, extraction):
    """Fold an :class:`Extraction` into the query it came from.

    Three rules, and each closes a way for extraction to make an answer
    silently wrong:

    * an APPLIED filter narrows, a soft one does not — but both become
      ``signals``, so a word the server declined to filter on can still
      rank the rows that satisfy it;
    * an explicit ``f.<slug>`` from the caller always wins. The person
      chose ``siniy`` in the panel; a word in the box that reads as
      ``krasnyy`` must not overrule the click;
    * the residual replaces the text ONLY when it still says something.
      «красные штаны» leaves «штаны» and the engine matches it. «красный»
      leaves nothing, and searching for the empty string would silently
      widen the answer to the whole catalogue — so the text is dropped only
      when the residual is empty AND some slug the extraction CLAIMED is
      actually being filtered (by this extraction or by the caller's own
      ``f.<slug>``). The query then becomes the filters it turned out to
      be, and ``count`` counts the chips' set, which is what the chips
      describe. If nothing narrowed, dropping the text would widen the
      answer to everything, so the original text stays and the answer is
      the narrow one.

    Returns ``(query, extraction)``: the extraction comes back rewritten so
    ``applied`` states what actually happened. A filter the caller's own
    ``f.<slug>`` overruled did not apply, and reporting it as applied would
    put a chip on the page that is not narrowing anything.
    """
    from dataclasses import replace

    from .text import normalize_query

    facets = dict(q.facets)
    overruled = set(q.facets)
    filters = tuple(
        replace(f, applied=f.applied and f.slug not in overruled)
        for f in extraction.filters
    )
    applied = [f for f in filters if f.applied]
    for extracted in applied:
        facets[extracted.slug] = [extracted.value]
    residual = (extraction.residual or "").strip()
    narrowed = any(f.slug in facets for f in filters)
    if residual:
        text = normalize_query(residual, q.language)
    elif narrowed:
        text = None
    else:
        text = q.text
    return (
        replace(
            q,
            facets=facets,
            text=text,
            signals=tuple((f.slug, f.value) for f in filters),
        ),
        replace(extraction, filters=filters),
    )


#: Marker for an extraction that was undone because it emptied the page.
UNDERSTANDING_WITHDRAWN = "understanding_withdrawn"


def _unextract_if_empty(q, unextracted, extraction, result, backend, plan):
    """Undo the extraction when its filters left nothing to show.

    An extracted filter is a GUESS about what a reader meant, and the one
    outcome that proves the guess wrong is an empty page over a catalogue
    that is not empty. Falling back to the plain text search is the single
    measured win of the 2026-09-03 labelled eval: recall@10 +0.057, paired
    bootstrap CI [+0.011, +0.125], P(gain) = 1.00 — larger than anything
    embedding listing titles bought, and free.

    Only on the FIRST page: a cursor mid-walk names an anchor inside a
    population, and quietly changing the population underneath it would
    repeat or skip rows rather than rescue the reader.

    The chips come back stamped ``applied=False`` and the answer carries
    ``understanding_withdrawn``, because a chip that is not filtering must
    not render as one that is — the whole point of showing them.
    """
    if extraction is None or unextracted is None or result.hits:
        return q, extraction, result
    if q.cursor is not None:
        return q, extraction, result
    if not any(f.applied for f in extraction.filters):
        return q, extraction, result

    retry, _ = _drop_hidden_filters(unextracted, plan)
    try:
        widened = backend.query(retry)
    except Exception:  # noqa: BLE001 - the empty answer we already have is valid
        logger.exception("retry without extraction failed; keeping the empty answer")
        return q, extraction, result
    if not widened.hits:
        # The catalogue really has nothing. Leave the chips applied — they
        # are a true description of what was searched for.
        return q, extraction, result

    withdrawn = replace(
        extraction,
        filters=tuple(replace(f, applied=False) for f in extraction.filters),
        degraded=tuple(dict.fromkeys(extraction.degraded + (UNDERSTANDING_WITHDRAWN,))),
    )
    return retry, withdrawn, widened


def _understanding_payload(extraction) -> dict:
    """The ``query_understanding`` block: what the words turned into.

    ``param`` is the whole contract with the frontend — the literal query
    parameter this filter IS, so a client keeps a chip by re-sending it and
    removes one by omitting it. Nothing here is remembered server-side,
    which is why the parameter has to be complete.
    """
    return {
        "filters": [
            {
                "slug": f.slug,
                "value": f.value,
                "label": f.label,
                "value_label": f.value_label,
                "method": f.method,
                "confidence": f.confidence,
                "span": list(f.span),
                "param": f.param,
                "applied": f.applied,
            }
            for f in extraction.filters
        ],
        "category_path": list(extraction.category_path),
        "category_confidence": extraction.category_confidence,
        "residual": extraction.residual,
        "degraded": list(extraction.degraded),
    }


#: Card keys that carry one half of a coordinate pair. A public card keeps
#: them, on the grid — a host map reading ``card.lat`` still works, it just
#: draws an area. Matched case-insensitively, and also as a ``*_lat`` suffix,
#: because the key is the host's to name.
_CARD_LAT_KEYS = ("lat", "latitude")
_CARD_LON_KEYS = ("lon", "lng", "long", "longitude")

#: Card keys that carry a position this module cannot rewrite into an area:
#: an encoding (``geohash``), a nested object, a pair in one string. They are
#: REMOVED rather than coarsened, for the reason ``AudienceRedactionMixin``
#: blanks the public geohash instead of truncating it — a second,
#: differently-aligned area around the same point intersects the first one
#: down to a sliver, and one area with nothing to intersect is the only
#: shape that keeps its promise.
_CARD_POSITION_KEYS = (
    "geohash",
    "geo",
    "_geo",
    "geopoint",
    "geo_point",
    "coord",
    "coords",
    "coordinate",
    "coordinates",
    "location",
    "point",
    "position",
    "pin",
)


def _coordinate_role(key: str) -> str | None:
    """``"lat"`` | ``"lon"`` | ``"drop"`` | ``None`` for one card key."""
    folded = str(key).strip().lower().replace("-", "_")
    if folded in _CARD_LAT_KEYS or folded.endswith("_lat") or folded.endswith("_latitude"):
        return "lat"
    if folded in _CARD_LON_KEYS or folded.endswith("_lon") or folded.endswith("_lng") or folded.endswith("_longitude"):
        return "lon"
    if folded in _CARD_POSITION_KEYS:
        return "drop"
    return None


def _public_card(row, audience: str) -> dict:
    """The stored card as this reader may have it. The promise, enforced.

    "The card never carries full-precision coordinates" was a docstring, and
    the code under it ran only inside ``geo_mode=rank`` — which needs
    ``GEO_BANDS``, which is off — and where it did run it OVERWROTE ``lat``
    and ``lon``, so a card that spelled the same fact ``latitude`` or carried
    a ``geohash`` walked through untouched. On this fleet that was safe by
    accident: the host's card happens to carry no coordinates at all.

    So: every path, flag or no flag. A recognised half of a pair is rewritten
    onto the public grid from the row's OWN columns (never from what the card
    happened to say); anything else that names a position is removed; and
    ``geo_precision_km`` is added beside whatever survived, so a client draws
    a circle rather than inferring precision from how many digits it can see.
    """
    from .backends import _shared as shared

    card = dict(row.card or {}) if row is not None else {}
    if row is None or not card or shared.is_precise(audience):
        return card
    lat, lon, precision_km = shared.coarse_coordinates(
        row.lat, row.lon, shared.public_precision()
    )
    touched = False
    for key in list(card):
        role = _coordinate_role(key)
        if role is None:
            continue
        coarse = lat if role == "lat" else None if role == "drop" else lon
        if coarse is None:
            card.pop(key)
        else:
            card[key] = coarse
        touched = True
    if touched and lat is not None:
        card.setdefault("geo_precision_km", precision_km)
    return card


def _card_area(row) -> dict:
    """The card's coordinates as an AREA — the ``rank`` mode's own addition.

    Under ``geo_mode=rank`` a card gains the neighbourhood
    (``CARD_COORD_PRECISION`` decimals, ~1.1km at the default 2) plus
    ``geo_precision_km``, which is what tells a client to draw a circle
    instead of a marker, even when the host's card carried no position at
    all. Cards that DID carry one are already on the grid by then
    (:func:`_public_card`); this only adds.
    """
    from .backends import _shared as shared

    if row is None:
        return {}
    lat, lon, precision_km = shared.coarse_coordinates(
        row.lat, row.lon, shared.public_precision()
    )
    if lat is None or lon is None:
        return {}
    return {"lat": lat, "lon": lon, "geo_precision_km": precision_km}


def _honest_bands(result, items) -> list[dict]:
    """The ``bands[]`` block: ``{id, count, radius_km?}`` in display order.

    :func:`_honest_count`'s rule, applied one level down. A heading reading
    «Объявления поблизости» over three visible cards may not claim zero of
    them, whatever the engine computed — so what this page shows is a floor
    for its own band, and a count below it becomes that floor and says it is
    one.

    ``radius_km`` rides on ``nearby`` alone: it is the edge that band was
    cut at, and ``all`` has no edge — it is everything else, which is the
    whole promise. Emitting it there as ``null`` would invite a reader to
    look for a number that does not exist.
    """
    shown: dict[str, int] = {}
    for item in items:
        band = item.get("band") or ""
        if band:
            shown[band] = shown.get(band, 0) + 1
    out = []
    for band in result.bands:
        count, lower_bound = band.count, band.count_is_lower_bound
        floor = shown.get(band.id, 0)
        if count is None:
            count, lower_bound = (floor, True) if floor else (None, False)
        elif count < floor:
            count, lower_bound = floor, True
        entry = {
            "id": band.id,
            "count": count,
            "count_is_lower_bound": bool(lower_bound),
        }
        if band.radius_km is not None:
            entry["radius_km"] = band.radius_km
        out.append(entry)
    return out


def _resolve_one_segment(segment):
    """One ``category=`` segment, looked up in BOTH namespaces.

    Returns ``(id path, slug or None, outcome, detail)``. A segment is read
    first as what it looks like — stapel-categories types an id as an
    integer, so a numeric segment is an id and anything else is a slug — and
    the other namespace is tried only when the first has no node. That order
    is what keeps a numeric id's unknown-id **400** a 400 while
    ``categories.by_slug`` still has no provider: the namespace the segment
    belongs to owns the outcome, and the other one can only upgrade it to
    ``ok``.
    """
    from .facets import lookup_path, lookup_slug

    text = str(segment)
    numeric = text.lstrip("-").isdigit()
    primary, secondary = (
        (lookup_path, lookup_slug) if numeric else (lookup_slug, lookup_path)
    )
    path, outcome, detail = primary(segment)
    if outcome == "ok" and path:
        return path, (None if numeric else text), "ok", ""
    other, other_outcome, _ = secondary(segment)
    if other_outcome == "ok" and other:
        return other, (text if numeric else None), "ok", ""
    return (), None, outcome, detail


def _category_echo(path, known):
    """``category_resolved`` — the same node in both addressable forms.

    The filter runs on ids; a readable address is made of slugs; a client
    holding either form has to be able to write the other, and this is the
    only place that knows both. ``slugs`` is ``null`` rather than partial:
    half a slug path builds a WRONG address, and a client cannot tell the
    two apart by looking. Returns ``(payload, degraded)``.
    """
    from .facets import slugs_for_ids

    ids = list(path)
    unnamed = [ids[i] for i, slug in enumerate(known) if not slug]
    named, unavailable = slugs_for_ids(unnamed) if unnamed else ({}, False)
    slugs = [slug or named.get(ids[i]) for i, slug in enumerate(known)]
    payload = {"path": "/".join(ids), "slugs": slugs if all(slugs) else None}
    return payload, ("category_names",) if unavailable else ()


def _resolve_category(q):
    """``category=`` takes ids, slugs and any mix of them; the filter takes ids.

    ``category`` is a PATH and it filters by PREFIX, so a single segment
    used to be read as a root: ``category=166`` matched documents whose
    ancestry STARTS at 166, of which a leaf three levels down has none, and
    the answer was ``count: 0`` with an empty panel at HTTP 200 beside its
    own working ``141/151/166``. 0.14.2 closed that by resolving a bare id
    through the same ``categories.path`` the INDEXER writes ancestry with —
    one place knows the tree — and this is the same resolution over the
    other unique key.

    A slug is a whole address on its own, because ``Category.slug`` is
    globally unique: ``category=avtomobili``, ``category=transport/
    avtomobili`` and ``category=141/avtomobili`` all resolve to the id path
    ``141/151`` the prefix filter runs on, and the answer echoes both forms
    back in ``category_resolved``.

    Returns ``(query, echo, degraded)``. Three outcomes per segment, three
    different answers, because collapsing them is how a 400 gets printed
    over an unreachable provider:

    - resolved -> filter on the id path;
    - no catalogue has the segment -> **400**, naming it. A bare
      ``count: 0`` is indistinguishable from an empty category, and a reader
      cannot tell a typo in a link from a branch the catalogue lost;
    - nobody answered -> the segment stands as it was and ``degraded:
      ["category_rollup"]`` says the rollup could not be built. An outage
      upstream does not make the caller's request invalid.
    """
    from .errors import ERR_400_UNKNOWN_CATEGORY, SearchValidationError
    from .facets import note_path_degradation

    segments = q.category_path
    if not segments:
        return q, None, ()

    if len(segments) == 1:
        path, slug, outcome, detail = _resolve_one_segment(segments[0])
        if outcome == "ok":
            q = replace(q, category_path=path)
            known = [None] * (len(path) - 1) + [slug]
        elif outcome == "unknown":
            raise SearchValidationError(ERR_400_UNKNOWN_CATEGORY, category=segments[0])
        else:
            note_path_degradation(detail)
            known = [None]
    else:
        resolved: list[str] = []
        known = []
        for segment in segments:
            if str(segment).lstrip("-").isdigit():
                # A multi-segment ID path is already what the index holds:
                # 0.14.2 left it untouched, and so does this. No lookup, so
                # no new refusals on links that work today.
                resolved.append(str(segment))
                known.append(None)
                continue
            path, slug, outcome, detail = _resolve_one_segment(segment)
            if outcome == "ok":
                resolved.append(path[-1])
                known.append(slug)
            elif outcome == "unknown":
                raise SearchValidationError(ERR_400_UNKNOWN_CATEGORY, category=segment)
            else:
                note_path_degradation(detail)
                resolved.append(str(segment))
                known.append(None)
        q = replace(q, category_path=tuple(resolved))

    echo, degraded = _category_echo(q.category_path, known)
    return q, echo, degraded


def search(params, *, accept_language: str = "", audience: str = "anonymous") -> dict:
    """Run one query and shape the answer. The module's whole read path.

    *audience* is ``stapel_attributes.visibility``'s axis — the one that
    already decides who may read a VIN — resolved once by
    :func:`stapel_search.authz.resolve_audience` and carried on the query. It
    decides whether this reader's geo answers are measured against the stored
    point or against the ~1.1km public grid, and whether the card is filtered
    on the way out. It defaults to the weakest audience: a caller that does
    not say who it is gets the grid.
    """
    from .backends import get_backend
    from .conf import search_settings
    from .errors import SearchBackendUnavailable
    from .facets import (
        evidence_plan,
        facet_plan,
        fill_zero_options,
        path_degradation,
        reset_path_degradation,
        url_keys,
        vocabulary_labels,
    )
    from .models import SearchDocument
    from .query import (
        encode_cursor,
        parse_facet_selection,
        parse_geo_mode,
        parse_query,
        parse_understanding,
        resolve_feature_keys,
    )

    started = timezone.now()
    reset_path_degradation()
    q = parse_query(params, accept_language=accept_language, audience=audience)
    # Before anything reads `q.category_path`: the plan is drawn from its last
    # segment and the engine filters on the whole of it, so a bare id or a
    # slug has to BE the id path by the time either looks.
    q, category_resolved, category_degraded = _resolve_category(q)
    # The address the reader sees carries `f.make`, the index holds
    # `make_ref_select`, and the scope that makes the short form unambiguous
    # is the category just resolved. Derived here from the same provider the
    # plan reads, so the two can never disagree about which slug a key names;
    # empty outside a scope, where only a real slug filters anything.
    scope_keys = url_keys(q.category_path[-1] if q.category_path else None)
    q = resolve_feature_keys(q, scope_keys)
    backend = get_backend()

    # The plan is built BEFORE the engine sees the query, because it is what
    # says which slugs are not filterable: a hidden slug's filter has to be
    # gone by the time an engine could answer with it.
    requested = parse_facet_selection(params)
    plan = facet_plan(
        q.category_path[-1] if q.category_path else None, requested=requested
    )
    plan_source = "category"
    evidence_categories: list[tuple[tuple[str, ...], int]] = []
    plan_degraded: tuple[str, ...] = ()
    if requested is None and int(search_settings.FACET_EVIDENCE_CATEGORIES) > 0:
        # The trigger is "the queried category's own schema did not fill the
        # budget", not "the category is a branch" — the second needs a tree
        # walk this module has no business doing, and the first is the thing
        # that actually matters. A wide leaf therefore pays NOTHING: its 19
        # authored slugs are already over MAX_FACET_FIELDS and the aggregate
        # is never run. A branch, a root and a text query all have an empty
        # plan and land here.
        #
        # A thin leaf pays one aggregate and gets its own plan back, because
        # the only category its candidate set contains is itself.
        if len(plan.slugs) < int(search_settings.MAX_FACET_FIELDS):
            aggregate = getattr(backend, "category_counts", None)
            if aggregate is None:
                # Never silent. An empty panel that cannot say why is
                # indistinguishable from a corpus with no axes, which is the
                # lie D175 was: «Для этого поиска фильтров нет» over 46
                # phones that all carry a manufacturer.
                plan_degraded = ("facet_plan_evidence",)
            else:
                try:
                    evidence_categories = list(
                        aggregate(q, limit=int(search_settings.FACET_EVIDENCE_CATEGORIES))
                    )
                except Exception as exc:  # noqa: BLE001 — a plan is never fatal
                    logger.warning("category_counts failed on %s: %s", backend.name, exc)
                    plan_degraded = ("facet_plan_evidence",)
                if evidence_categories:
                    widened = evidence_plan(
                        [(path[-1], count) for path, count in evidence_categories],
                        requested=requested,
                        # The queried category's own plan is already in the
                        # order the client draws it (schema order, mandatory
                        # first). Widening adds axes BELOW it; it does not get
                        # to reshuffle a page that has a schema — a thin leaf
                        # widened from itself must come back identical.
                        authored=plan.slugs + plan.skipped,
                    )
                    if widened.slugs:
                        # The queried category AUTHORED these, so they are not
                        # borrowed and the coverage floor does not govern them —
                        # the exemption 0.14.3 states. Widening used to erase
                        # it: `evidence_plan` marks everything it ranked, and a
                        # thin leaf (fewer axes than the budget) is widened from
                        # ITSELF, so its own mandatory axes then faced a 0.6
                        # floor. A make filled by a third of a small leaf's
                        # listings vanished from the panel with real buckets
                        # behind it, and a vocabulary-backed group the client
                        # cannot enumerate on its own is gone for good.
                        authored = set(plan.slugs)
                        plan = replace(
                            widened,
                            evidence=tuple(
                                slug for slug in widened.evidence if slug not in authored
                            ),
                        )
                        plan_source = "evidence"
    # Extraction runs AFTER the plan (it needs an option space to resolve
    # against) and BEFORE the hidden-filter sweep, so a word that resolves
    # to a non-public slug is dropped by the same guard an explicit
    # `f.vin=` is — an extracted oracle is still an oracle.
    extraction = None
    unextracted = None
    if parse_understanding(params):
        from .understanding import extract

        raw_text = str(params.get("q") or "").strip()
        if raw_text:
            extraction = extract(
                raw_text,
                language=q.language,
                plan=plan,
                category_path=q.category_path,
            )
            # Kept so an extraction that empties the page can be undone. The
            # query BEFORE extraction is the one the reader actually typed.
            unextracted = q
            q, extraction = _apply_extraction(q, extraction)

    q, dropped_filters = _drop_hidden_filters(q, plan)

    try:
        capabilities = backend.capabilities()
        result = backend.query(q)
    except Exception as exc:  # noqa: BLE001 - any engine failure is a 503, not a 500
        logger.exception("search backend %s failed", getattr(backend, "name", "?"))
        raise SearchBackendUnavailable(str(exc)) from exc

    q, extraction, result = _unextract_if_empty(
        q, unextracted, extraction, result, backend, plan
    )

    facet_result = None
    if plan.slugs and capabilities.facet_counts:
        facet_result = backend.facets(q, plan)

    # The BOUNDS of the numeric axes, which the counting verb cannot give:
    # a from/to picker has nothing to enumerate. Asked for whether or not
    # facets were counted — a range axis is not on the facet budget — and
    # never fatal: a panel without bounds is worse than one with them, and
    # both are better than a 503 over a slider.
    range_bounds: dict[str, dict] = {}
    range_coverage: dict[str, int] = {}
    range_degraded: tuple[str, ...] = ()
    bounder = getattr(backend, "ranges", None)
    if bounder is None:
        range_degraded = ("facet_ranges",)
    elif plan.range_candidates or plan.core_ranges:
        try:
            range_bounds, range_coverage = _range_payload(bounder(q, plan))
        except Exception as exc:  # noqa: BLE001 — a bound is never fatal
            logger.warning("ranges failed on %s: %s", backend.name, exc)
            range_degraded = ("facet_ranges",)

    keys = [hit.key for hit in result.hits]
    rows = {
        row.doc_key: row
        for row in SearchDocument.objects.filter(doc_type=q.doc_type, doc_key__in=keys)
    }

    # Two different questions. `geo_mode == "rank"` decides the SHAPE of
    # the answer (a caller ranking by proximity is told what it got, even
    # when that is "no centre, so no partition"); `q.near` decides whether
    # there is anything to label. Under `filter` — which is every request
    # while GEO_BANDS is off — neither is true and the answer is what it
    # was before bands existed, key for key.
    ranked = parse_geo_mode(params) == "rank"
    items = []
    for hit in result.hits:
        row = rows.get(hit.key)
        item = {
            "key": hit.key,
            "score": hit.score,
            # DSA Art. 26: present on EVERY item, under every sort,
            # including when it is false. The serializer cannot omit it.
            "promoted": bool(row.promoted) if row is not None else False,
            # The seller this row belongs to, as the source named them. Read
            # off the row already in hand, so a seller panel per card costs
            # no second query here and one batched profile read at the
            # client. Empty when the source indexed no owner — an absent
            # value is "" and never a missing key, so a client branches on
            # the value rather than on the shape.
            "owner_key": str(row.owner_key or "") if row is not None else "",
            "distance_km": hit.distance_km,
            "card": _public_card(row, q.audience),
        }
        if ranked:
            item["band"] = hit.band
            item["card"].update(_card_area(row))
        if q.signals:
            item["match_count"] = hit.match_count
        items.append(item)

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

    # A facet the evidence admitted has to earn its slot in the panel.
    #
    # The trap the coverage floor closes is the one that produced «Вес/Длина/
    # Высота (Для Доставки)» above «Производитель»: a plan drawn from several
    # categories offers axes that describe a handful of rows, and a group of
    # three options over one document out of fifty-two is noise wearing the
    # costume of a filter. Coverage is the sum of a group's bucket counts —
    # the SAME quantity `facetCoverage` in @stapel/search-react sorts the
    # chip row and the rail by — so the server withholds by exactly the
    # measure the two client surfaces already rank by.
    #
    # Three things are deliberately exempt:
    #  - a slug the QUERIED CATEGORY authored (`plan.evidence` holds only the
    #    borrowed ones). A closed option set answering with its zeros is a
    #    shipped decision, not an accident;
    #  - a slug the reader has already filtered on — withholding that group
    #    leaves the filter applied with no control to undo it;
    #  - everything, when FACET_MIN_COVERAGE is 0.
    withheld: list[dict] = []
    # The plan BEFORE the group prune below, kept because the range half of
    # the panel is labelled from it. A group withheld for describing too few
    # documents and a range over the same slug are two measurements of two
    # different things (a bucket sum against documents carrying a number),
    # and letting the first one delete the second's caption would report the
    # range as `unlabelled`, which would be a reason that is not true.
    label_plan = plan
    if facet_result and plan.evidence:
        from .backends._shared import bucket_limit

        floor = float(search_settings.FACET_MIN_COVERAGE)
        denominator = facet_result.candidates
        if floor > 0 and denominator > 0:
            for slug in plan.evidence:
                if slug in q.facets:
                    continue
                values = facet_result.counts.get(slug) or {}
                if len(values) >= bucket_limit(plan, slug):
                    # The bucket list hit its cap, so this sum is a FLOOR on
                    # the group's coverage and not a measurement of it — and
                    # a group is withheld for describing too little, which a
                    # floor cannot establish. A long tail of makes is the
                    # case: capped at 200 of 418, it reads as half a page.
                    continue
                coverage = sum(values.values())
                if coverage < floor * denominator:
                    withheld.append(
                        {
                            "slug": slug,
                            # `axis` and `reason` are named since 0.16.0.
                            # `withheld` used to hold one kind of thing, so
                            # both were the list's identity; it now holds
                            # ranges too, and a panel saying «3 filters apply
                            # to too few of these» about an axis withheld for
                            # having no name is a sentence that is not true.
                            "axis": AXIS_GROUP,
                            "reason": WITHHELD_COVERAGE,
                            "coverage": coverage,
                            "candidates": denominator,
                        }
                    )
    withheld_slugs = {row["slug"] for row in withheld}
    if withheld_slugs:
        facet_result = replace(
            facet_result,
            counts={
                slug: values
                for slug, values in facet_result.counts.items()
                if slug not in withheld_slugs
            },
        )
        plan = replace(
            plan,
            slugs=tuple(slug for slug in plan.slugs if slug not in withheld_slugs),
            closed_options={
                slug: values
                for slug, values in plan.closed_options.items()
                if slug not in withheld_slugs
            },
            option_labels={
                slug: values
                for slug, values in plan.option_labels.items()
                if slug not in withheld_slugs
            },
            vocabulary_refs={
                slug: address
                for slug, address in plan.vocabulary_refs.items()
                if slug not in withheld_slugs
            },
            group_labels={
                slug: label
                for slug, label in plan.group_labels.items()
                if slug not in withheld_slugs
            },
        )

    # The measurement half of the panel, held to the same two rules: an axis
    # nobody can name is not offered, and an axis that describes too little
    # of this page is not offered. Both say so in `withheld` rather than
    # vanishing, so a panel can tell "no filters here" from "these filters
    # apply to too few of these", which are different pages.
    #
    # Run over the FULL bound set and after the group prune, because the two
    # halves are independent: withholding «Вес (Для Доставки)» as a slider
    # says nothing about the same slug's bucket list, and vice versa.
    # No facet pass means no candidate total to take a share OF, so the
    # coverage rule stands down and the naming rule still applies.
    range_candidates_total = facet_result.candidates if facet_result else 0
    withheld_range_rows = _withheld_ranges(
        range_bounds, range_coverage, label_plan, q, range_candidates_total
    )
    if withheld_range_rows:
        gone = {row["slug"] for row in withheld_range_rows}
        range_bounds = {
            slug: bounds for slug, bounds in range_bounds.items() if slug not in gone
        }
        withheld = withheld + withheld_range_rows
    range_bounds = _range_meta(range_bounds, label_plan)

    counts = fill_zero_options(facet_result.counts, plan) if facet_result else {}
    count, count_is_lower_bound = _honest_count(result, offset=offset, shown=len(items))
    exact_total = bool(result.exact_total and count is not None and not count_is_lower_bound)
    # A vocabulary-backed slug's caption is resolved from the codes the query
    # actually counted, so it can only be added here. `translatable` is False
    # for these without asking: a vocabulary term's label is literal text an
    # owner curated, never a translation key — that is the difference between
    # a vocabulary and an inline option list.
    #
    # An entry exists for EVERY group in `facets`, captions or not, because
    # `label` is owed for every group. A panel with no heading of its own
    # falls back to the slug and prints `make_ref_select` above the makes,
    # which is a group nobody recognizes as the make filter — the axis was
    # there and unreadable. `label: null` says the definition carries no
    # name, which is a different fact from a name this module invented out
    # of the slug, and the client can tell the two apart.
    groups = (
        list(counts)
        + [slug for slug in plan.option_labels if slug not in counts]
        + [slug for slug in plan.vocabulary_refs if slug not in counts]
    )
    facet_labels: dict[str, dict] = {}
    for slug in groups:
        name, name_translatable = plan.group_labels.get(slug, (None, False))
        facet_labels[slug] = {
            "label": name,
            "label_translatable": bool(name_translatable),
            # What this group is called in the address bar: the slug without
            # its importer type suffix where that stays unambiguous in this
            # category, and the slug itself otherwise (and always, with no
            # category in scope). The client writes this and reads both.
            "url_key": scope_keys.get(slug, slug),
            "translatable": bool(plan.translatable_labels.get(slug, True)),
            "values": dict(plan.option_labels.get(slug) or {}),
            # Where this group sits in ONE panel, numbered with the RANGES —
            # see FacetPlan.order. The two halves of a panel arrive in two
            # keys (`facets` and `facet_meta.ranges`), and a client that draws
            # every choice and then every measurement is not drawing the page
            # the category authored: a cars schema puts «Год» among the makes
            # and models, not below them.
            "order": label_plan.order.get(slug),
        }
        # The vocabulary a client can't otherwise learn: a branch page has no
        # leaf schema of its own, and the plan's feature definition is the
        # only place that still knows this axis is `ref_select` rather than
        # an inline `select` — same source `vocabulary_labels` reads below.
        # `None` for an inline option set, never simply absent, so a reader
        # can tell "no vocabulary" from "field not built yet".
        ref = plan.vocabulary_refs.get(slug)
        facet_labels[slug]["vocabulary"] = ref[0] if ref else None
        if ref:
            facet_labels[slug]["level"] = ref[1]
    for slug, values in vocabulary_labels(plan, counts).items():
        ref = plan.vocabulary_refs.get(slug)
        default = {
            "label": None,
            "label_translatable": False,
            "url_key": scope_keys.get(slug, slug),
            "vocabulary": ref[0] if ref else None,
            "order": label_plan.order.get(slug),
        }
        if ref:
            default["level"] = ref[1]
        facet_labels.setdefault(slug, default)
        facet_labels[slug].update({"translatable": False, "values": values})
    answer = {
        "items": items,
        # The queried node in both addressable forms, or null when no
        # category was asked for. `category=` accepts ids, slugs and any mix
        # of them, so the caller's own string is not the address it landed
        # on — this is, and a client rewrites its URL from it in either
        # direction (`141/151` for the filter, `transport/avtomobili` for a
        # readable path). `slugs` is null, never partial.
        "category_resolved": category_resolved,
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
            # `{slug: {min, max}}` for every axis that HAS a number in this
            # candidate set — core columns and attributes alike, measured
            # with the range filters removed. This is what a from/to picker
            # is drawn from; an axis absent here has no numbers behind it on
            # this page, which is a different fact from a bound of zero.
            "ranges": range_bounds,
            # Where the plan came from. `category` is the authored schema of
            # the queried category; `evidence` means it was drawn from the
            # categories the candidate set actually contains, because that
            # schema did not fill the budget (a branch owns no axes, a text
            # query names no category).
            "plan": plan_source,
            # Counted, then dropped for describing too little of the result
            # set — with the number, so a panel can say "3 filters apply to
            # too few of these" instead of "no filters".
            "withheld": withheld,
            # The categories the candidate set is made of, busiest first —
            # the evidence the plan was drawn from, and the material a panel
            # needs to offer the CATEGORY as the first filter on a text
            # search. Empty when the plan is the queried category's own.
            "categories": [
                {"category": "/".join(path), "count": count}
                for path, count in evidence_categories
            ],
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
                + tuple(extraction.degraded if extraction is not None else ())
                + plan_degraded
                + category_degraded
                + range_degraded
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
    if extraction is not None:
        # Absent entirely when the flag is off: an answer that never
        # extracted must look exactly like the answers that came before
        # extraction existed.
        answer["query_understanding"] = _understanding_payload(extraction)
    if ranked:
        # Present only under `geo_mode=rank`, so an answer with GEO_BANDS
        # off is byte-for-byte the one this module gave before bands
        # existed. Empty means "ranked, but there was no centre to partition
        # around" — which is an answer, not a failure.
        answer["bands"] = _honest_bands(result, items)
    return answer


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
