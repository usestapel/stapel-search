"""Frozen data contracts shared by the service layer and every backend.

Nothing here touches Django or a database: these are the shapes that cross
the backend seam, which is exactly why they are testable — and importable
by a third-party backend — without a settings module.

The one rule the seam enforces, borrowed one level up from
``stapel_geo.search.base``: **backends return keys and scores; the service
layer shapes the response.** A backend never builds a card, never resolves
a label, never decides what "promoted" means.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

# --------------------------------------------------------------------------
# write side
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchDocumentInput:
    """What a source mapper returns: one document, engine-agnostic.

    This is the composite's output and the indexer's input. It is deliberately
    *not* the model: the mapper must not know about tsvectors, term arrays or
    which backend is configured.
    """

    doc_type: str
    doc_key: str
    #: Raw source status. The index membership predicate is applied here, by
    #: the indexer, from the document — never inferred from an event name
    #: (spec §19.7: a republished listing emits `listing.updated` with
    #: `status: pending`, and `listing.removed` is not emitted at all).
    status: str = ""
    language: str = ""
    owner_key: str = ""
    category_id: str = ""
    #: root->leaf. Built by this module from stapel-categories, because the
    #: listings source does not serve one (spec §19.1).
    category_path: tuple[str, ...] = ()
    title: str = ""
    body: str = ""
    #: Values shown as title/badge chips — weight B text.
    text_extra: tuple[str, ...] = ()
    #: ``{slug: dao}`` — full stapel-attributes DAOs, the authoritative input.
    features: dict[str, dict] = field(default_factory=dict)
    #: ``{slug: [values]}`` — the source's lossy projection, used only when a
    #: mapper hands over no DAOs and ``ACCEPT_FEATURES_SEARCH`` is on.
    features_search: dict[str, list] = field(default_factory=dict)
    price_base: Decimal | None = None
    # stapel: index-waived price — the display price rides to the card, which is
    # what a result row shows; sorting and range filters use price_base, the one
    # value that is comparable across currencies.
    price: Decimal | None = None
    # stapel: index-waived currency — a label for the card, never a predicate: a
    # currency filter would be a filter on how a price is written down.
    currency: str = ""
    published_at: Any = None
    source_updated_at: Any = None
    lat: Decimal | None = None
    lon: Decimal | None = None
    geohash: str = ""
    # stapel: index-waived location_id — carried to the card so a result row can
    # link to the place; proximity is answered by lat/lon, and an opaque place id
    # is not a search axis this module owns.
    location_id: str = ""
    # stapel: index-waived location_label — the human-readable place name for the
    # card. Indexing it would put an unstemmed, untranslated string into the text
    # arm and make "Paris" match every listing in Paris ahead of the one selling
    # a book about it.
    location_label: str = ""
    #: Stored row fields, so a result page costs one query (verdict §18.4).
    card: dict = field(default_factory=dict)
    #: Monotonic ordering token; unix ms, comparable with ``Event.timestamp``.
    seq: int = 0
    source_event_id: str = ""


@dataclass(frozen=True)
class IndexDocument:
    """The flattened, backend-facing document.

    Derived from :class:`SearchDocumentInput` by the indexer, so every backend
    sees identical, already-normalized values — the divergence a conformance
    suite exists to prevent starts with two engines normalizing differently.
    """

    doc_type: str
    doc_key: str
    visible: bool
    language: str
    owner_key: str
    category_path: tuple[str, ...]
    title: str
    body: str
    text_extra: str
    text_plain: str
    #: ``{slug: [values]}`` — authoritative filter structure.
    facets: dict[str, list]
    #: ``["slug=value", ...]`` — the counting structure (path slugs expanded
    #: to every prefix, which is what makes rollup correct under non-unique
    #: sibling codes).
    facet_terms: tuple[str, ...]
    #: ``{slug: Decimal}`` — the range-filter answer.
    numbers: dict[str, Decimal]
    price_base: Decimal | None
    published_at: Any
    popularity: int
    lat: float | None
    lon: float | None
    geohash: str
    boost: float
    promoted: bool
    card: dict


@dataclass(frozen=True)
class IndexSettings:
    """Engine-level settings for one ``doc_type``.

    Postgres derives almost all of this from its schema and applies only the
    query-side dictionary work; Meilisearch needs it declared up front. Both
    receive the same object, which is what keeps the dictionary halves
    honest.
    """

    doc_type: str
    searchable_fields: tuple[str, ...] = ("title", "text_extra", "body")
    filterable_fields: tuple[str, ...] = ()
    sortable_fields: tuple[str, ...] = ()
    #: Symmetric groups, expanded per engine.
    synonyms: tuple[tuple[str, ...], ...] = ()
    stopwords: tuple[str, ...] = ()
    ranking_rules: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeFilter:
    """``r.<slug>=<from>..<to>``; either end may be open."""

    slug: str
    lower: Decimal | None = None
    upper: Decimal | None = None


@dataclass(frozen=True)
class GeoFilter:
    """A radius around a centre, or a rectangle.

    ``min_lon > max_lon`` means the box crosses the antimeridian — the
    contract is borrowed verbatim from ``GeoSearchBackend.bbox`` and is the
    first item of the conformance suite.
    """

    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    min_lat: float | None = None
    min_lon: float | None = None
    max_lat: float | None = None
    max_lon: float | None = None

    @property
    def has_center(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def is_bbox(self) -> bool:
        return None not in (self.min_lat, self.min_lon, self.max_lat, self.max_lon)

    @property
    def crosses_antimeridian(self) -> bool:
        return self.is_bbox and self.min_lon > self.max_lon


@dataclass(frozen=True)
class NormalizedQuery:
    """The dictionary half of query handling — identical for every backend.

    Morphology is NOT here: stemming belongs to the ``russian`` tsvector
    config or Meili's analyzer (architect verdict §9). This object carries
    only rewrites, stopword removal, synonym groups and transliteration —
    and the conformance suite asserts both backends receive a byte-identical
    one, because a divergence here is precisely the seam defect the suite
    exists for.
    """

    raw: str
    #: One entry per surviving term; each is the term plus its expansions.
    terms: tuple[tuple[str, ...], ...] = ()
    dropped_stopwords: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.terms

    @property
    def flat_terms(self) -> tuple[str, ...]:
        return tuple(group[0] for group in self.terms)


@dataclass(frozen=True)
class Cursor:
    """Decoded keyset anchor: ``(sort_value, doc_key)``.

    Relevance is not a column in any engine, which is why
    ``AnchorPagination`` (one model field) cannot carry it and this module
    ships its own opaque cursor — with the *same* response envelope, so a
    frontend paging hook needs no special case for search (spec §7.5).
    """

    sort_value: Any
    doc_key: str
    #: How many rows precede this anchor, for the MAX_RESULT_WINDOW refusal.
    offset: int = 0


@dataclass(frozen=True)
class SearchQuery:
    """One parsed, validated query. The only thing a backend is handed."""

    doc_type: str
    #: Selects the FTS config and the dictionary. ALWAYS set (from `lang`,
    #: else Accept-Language, else DEFAULT_LANGUAGE) — it describes the
    #: QUERY, not the corpus.
    language: str = ""
    #: Corpus predicate, set ONLY when the caller asked for a language
    #: explicitly. Defaulting this from Accept-Language would mean a browser
    #: sending `Accept-Language: en` never sees a Russian listing — a filter
    #: nobody typed, silently emptying the catalogue.
    language_filter: str = ""
    text: NormalizedQuery | None = None
    category_path: tuple[str, ...] = ()
    owner_key: str = ""
    #: ``{slug: [values]}`` — AND between slugs, OR within a slug.
    facets: dict[str, list[str]] = field(default_factory=dict)
    ranges: tuple[RangeFilter, ...] = ()
    geo: GeoFilter | None = None
    sort: str = "relevance"
    limit: int = 24
    cursor: Cursor | None = None
    direction: str = "next"
    #: Scorer slugs active for this query's sort.
    scorers: tuple[str, ...] = ()

    def without_facet(self, slug: str) -> "SearchQuery":
        """This query with *slug*'s own filter removed — drill-down semantics.

        Counting a facet against a candidate set that still contains its own
        filter would show ``N`` for the chosen value and ``0`` for every
        neighbour, turning the panel into a dead end (spec §7.1).
        """
        if slug not in self.facets:
            return self
        remaining = {k: v for k, v in self.facets.items() if k != slug}
        return replace(self, facets=remaining)


@dataclass(frozen=True)
class Hit:
    """One backend answer: a key, a score, and a distance when geo was asked."""

    key: str
    score: float = 0.0
    distance_km: float | None = None
    #: Value of the active sort key, so the service can build the next cursor
    #: without a second read.
    sort_value: Any = None


@dataclass(frozen=True)
class QueryResult:
    """One page of hits, plus how many documents match — honestly.

    ``total`` answers three different questions and says which one it is:

    * ``exact_total=True`` — the count is the count.
    * ``total_is_lower_bound=True`` — **at least** ``total`` match, possibly
      more. A capped count, a window-truncated engine answer, or an
      estimate: it renders as "N+", never as "N".
    * ``total=None`` — the engine cannot say. Rendered as no count at all.

    A backend must never return ``total=0`` beside a non-empty ``hits``:
    zero is a claim ("nothing matches") and the page in front of the reader
    disproves it. When the count is unknown, ``None`` is the honest answer;
    when it is partial, ``total_is_lower_bound`` is. The service layer
    enforces the invariant on top of whatever a backend returns
    (``services._honest_count``), so a third-party engine cannot reintroduce
    the "Примерно 0 объявлений" over four visible cards.
    """

    hits: tuple[Hit, ...] = ()
    total: int | None = 0
    #: True when the count is exact for THIS answer. Per-answer, not a
    #: property of the engine: ``BackendCapabilities.exact_total`` says
    #: whether an engine counts exactly at ANY corpus size, and an engine
    #: that does not can still count a small candidate set exactly.
    exact_total: bool = False
    #: True when ``total`` is a floor rather than a count.
    total_is_lower_bound: bool = False
    has_next: bool = False
    has_prev: bool = False
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class FacetPlan:
    """What to count, from the category's feature schema.

    ``closed_options`` carries the full option list for slugs whose set is
    closed (``options && !allowCustom``): those answer with **every** option,
    zeros included — that is the entire reason a plan exists rather than
    "count whatever showed up".
    """

    slugs: tuple[str, ...] = ()
    kinds: dict[str, str] = field(default_factory=dict)
    closed_options: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Slugs dropped because MAX_FACET_FIELDS was reached — reported, never
    #: silently vanished.
    skipped: tuple[str, ...] = ()
    revision: Any = None


@dataclass(frozen=True)
class FacetResult:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    approximate: bool = False
    candidates: int = 0
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendCapabilities:
    """What this engine can honestly do.

    The service compares what was asked against this and puts the shortfall
    in ``degraded[]``. Nothing degrades into a log line: a frontend that
    cannot see the shortfall renders a confident wrong answer, which is the
    ``email_mock`` failure mode one layer down.
    """

    typo_tolerance: bool = False
    facet_counts: bool = False
    exact_facet_counts: bool = False
    #: Whether this engine counts exactly at ANY corpus size. A ``False``
    #: here is not a promise that every answer is inexact: Postgres counts a
    #: candidate set below ``FACET_CANDIDATE_CAP`` exactly and says so in
    #: ``QueryResult.exact_total``, which is the per-answer truth the
    #: response and ``degraded[]`` are built from.
    exact_total: bool = False
    geo_native: bool = False
    synonyms_native: bool = False
    suggest: bool = False
    phrase_synonyms: bool = False
    supported_scorers: frozenset[str] = frozenset()
    max_facet_fields: int = 12
    max_result_window: int = 1000


@dataclass(frozen=True)
class BackendHealth:
    name: str
    reachable: bool
    detail: str = ""
    documents: int | None = None


__all__ = [
    "BackendCapabilities",
    "BackendHealth",
    "Cursor",
    "FacetPlan",
    "FacetResult",
    "GeoFilter",
    "Hit",
    "IndexDocument",
    "IndexSettings",
    "NormalizedQuery",
    "QueryResult",
    "RangeFilter",
    "SearchDocumentInput",
    "SearchQuery",
]
