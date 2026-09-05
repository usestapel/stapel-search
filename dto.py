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
    # stapel: index-waived hidden_features — a DENYLIST the indexer obeys, never
    # a value it writes. Indexing it is precisely what this field exists to
    # prevent.
    #: Slugs whose values must not be indexed at all, declared by the producer.
    #:
    #: A DAO carries its own ``visibility`` stamp (stapel-attributes 0.8), so
    #: the ``features`` path needs no declaration: :func:`is_public` reads the
    #: value in hand. ``features_search`` cannot — it is ``{slug: [values]}``,
    #: values only, with no stamp and no type — so a producer that hands over
    #: that projection instead of DAOs has no way to say "this one is a VIN"
    #: unless a channel exists. This is that channel, and it is the
    #: ``categories.path`` canon applied again (``facets.py`` module
    #: docstring): name the field the owner does not serve yet, obey it the
    #: moment they do.
    #:
    #: It is a belt on the DAO path too — an explicit denylist wins over a
    #: missing stamp — and it is optional and empty by default, so an existing
    #: producer indexes exactly what it indexed before. A producer that
    #: populates NEITHER this nor a stamped DAO cannot be defended at write
    #: time by anything in this module; what defends it is the read path,
    #: which reads visibility from ``categories.features`` (``facet_plan``'s
    #: ``hidden``) and refuses to plan, count or filter on the slug.
    hidden_features: tuple[str, ...] = ()
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

    @property
    def multiword_expansions(self) -> tuple[str, ...]:
        """Group members an engine without phrase synonyms cannot honour.

        A single-word expansion is served by every engine — it is one more
        alternative in the same OR. A member with a space in it («бывший в
        употреблении») is a PHRASE, and a `to_tsquery` alternative cannot
        require two lexemes to be adjacent. This is the whole content of
        the ``phrase_synonyms`` capability, and reporting it per ANSWER
        rather than per engine is what keeps the shortfall true.
        """
        seen: list[str] = []
        for group in self.terms:
            for member in group:
                if " " in member and member not in seen:
                    seen.append(member)
        return tuple(seen)


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
    #: The nearby band's centre and edge. Carried SEPARATELY from ``geo`` on
    #: purpose: ``geo`` excludes rows, ``near`` only labels and orders them.
    #: Folding the two together is exactly how a band becomes a hidden
    #: radius filter again. Under ``geo_mode=rank`` the request's own
    #: ``radius_km`` populates THIS and leaves ``geo`` unbounded — proximity
    #: is an ordering, not a gate.
    near: GeoFilter | None = None
    #: ``(slug, value)`` pairs the query produced, INCLUDING the soft ones
    #: that were not applied as filters. The backend counts how many of
    #: these each row satisfies and reports it as ``Hit.match_count``.
    signals: tuple[tuple[str, str], ...] = ()
    sort: str = "relevance"
    limit: int = 24
    cursor: Cursor | None = None
    direction: str = "next"
    #: Scorer slugs active for this query's sort.
    scorers: tuple[str, ...] = ()
    #: WHO is asking — ``anonymous`` | ``owner`` | ``staff``, the audience axis
    #: of ``stapel_attributes.visibility``, the same one that decides who may
    #: read a VIN. It decides one thing here: whether a geo answer is measured
    #: against the stored point or against the ~1.1km grid a public card
    #: publishes (``backends._shared``, "the public grid").
    #:
    #: The default is the WEAKEST audience on purpose. A backend, a comm
    #: caller or a management command that never says who it is gets the
    #: grid, because the only safe answer to "who is this?" when nobody said
    #: is "a stranger".
    audience: str = "anonymous"

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
class ExtractedFilter:
    """One filter the QUERY's own words produced, ready to be replayed.

    ``param`` is the whole contract with the frontend: it is the exact query
    parameter this filter is, so a client re-sends it verbatim to keep the
    filter and simply omits it to remove the chip. Nothing about extraction
    is stateful — the server never remembers what it extracted last time.

    ``span`` indexes the RAW query, so a UI can underline the words that
    became a chip. ``applied`` separates the two populations the eval cares
    about: a filter that actually narrowed the answer, and a signal that
    only contributed to ``Hit.match_count``.
    """

    slug: str
    value: str
    label: str = ""
    value_label: str = ""
    #: ``exact`` | ``translit`` | ``alias`` | ``vector``
    method: str = ""
    confidence: float = 0.0
    span: tuple[int, int] = (0, 0)
    param: str = ""
    applied: bool = True


@dataclass(frozen=True)
class Extraction:
    """What a free-text query turned out to be ABOUT, beside its words.

    ``residual`` is the query with every extracted span removed — the text
    the engine still has to match. An empty residual is normal and correct
    («красные штаны» is entirely filters once the category is resolved).
    """

    filters: tuple[ExtractedFilter, ...] = ()
    category_path: tuple[str, ...] = ()
    category_confidence: float = 0.0
    residual: str = ""
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class BandSummary:
    """One labelled band of an answer — never a filter over it.

    A band says where a row sits relative to the reader, and the reader can
    always keep scrolling into the next one. ``count`` obeys the same
    honesty rule as :class:`QueryResult`: ``None`` when unknown.
    """

    #: ``nearby`` | ``all``
    id: str
    count: int | None = None
    count_is_lower_bound: bool = False
    #: Set on ``nearby`` only: the edge, in km, the band was cut at.
    radius_km: float | None = None


@dataclass(frozen=True)
class Hit:
    """One backend answer: a key, a score, and a distance when geo was asked."""

    key: str
    score: float = 0.0
    distance_km: float | None = None
    #: Value of the active sort key, so the service can build the next cursor
    #: without a second read.
    sort_value: Any = None
    #: ``nearby`` | ``all`` | ``""`` when banding is off. Which partition of
    #: the answer this row sits in; carried on the cursor so one anchor pages
    #: straight out of ``nearby`` into ``all``.
    band: str = ""
    #: How many of the query's extracted signals this row satisfies. The
    #: owner's "strongest first", made countable.
    match_count: int = 0


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
    #: Per-band counts, in render order. Empty when banding is off. The rows
    #: themselves carry ``Hit.band``; this is the summary a heading needs.
    bands: tuple[BandSummary, ...] = ()


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
    #: Slugs the category declares non-public (``FeatureDef.visibility`` is
    #: ``owner`` or ``staff``). A HARD exclusion: not counted, not re-admitted
    #: by an explicit ``facets=<slug>``, and their ``f.``/``r.`` filters are
    #: dropped from the query before it reaches an engine. Carried on the plan
    #: rather than kept private to ``facet_plan`` because the read path needs
    #: the same list — a facet nobody may enumerate is a facet nobody may use
    #: as an exact-match oracle either.
    hidden: tuple[str, ...] = ()
    revision: Any = None
    #: ``{slug: {value: caption}}`` for slugs whose options are inline in the
    #: category config. The captions were always there — the plan reads the
    #: same option dicts to build ``closed_options`` — and not shipping them
    #: is why a panel whose host had not threaded the schema through printed
    #: ``b-u`` at buyers.
    option_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ``{slug: bool}`` — whether that slug's captions are translation KEYS
    #: (``translatable_options``, default true) or literal text. The reader
    #: cannot guess: ``b.apple`` and ``Б/у`` are both strings.
    translatable_labels: dict[str, bool] = field(default_factory=dict)
    #: ``{slug: (vocabulary, level)}`` for the vocabulary-backed slugs in the
    #: plan. Not captions: a level can hold tens of thousands of terms, so the
    #: only affordable resolution is of the codes a query actually COUNTED,
    #: which is not known until the counting is done. This is the address the
    #: post-count pass needs.
    vocabulary_refs: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ``{slug: (name, translatable)}`` — the feature definition's own name,
    #: which is the HEADING a panel puts above the buckets, and whether that
    #: name is a translation key (``FeatureDef.translate``) or literal text.
    #: The same pair an option caption ships as, one level up. A slug with no
    #: definition is simply absent: the answer says ``label: null`` for it
    #: rather than inventing a heading out of the slug.
    group_labels: dict[str, tuple[str, bool]] = field(default_factory=dict)
    #: ``{slug: unit}`` — the unit a MEASUREMENT is written in, read off the
    #: feature config the same way the group's name is (``postfix``; the base
    #: unit of the family for ``convertible_unit``, which is what the stored
    #: number is in). A range answers with two numbers and nothing else, so
    #: without this a panel draws «40 000 … 120 000» and the reader has to
    #: guess whether that is kilometres, miles or hours. Absent for an axis
    #: whose definition names no unit — which is a different fact from an
    #: empty one, and the answer omits the key rather than sending "".
    units: dict[str, str] = field(default_factory=dict)
    #: Reserved range slugs that address a core document column
    #: (``index_schema.CORE_RANGE_FIELDS``). Not part of ``slugs``: there is
    #: nothing to count, only an axis to offer.
    core_ranges: tuple[str, ...] = ()
    #: The axes a range report may put bounds on — the plan's own slugs, on
    #: the plan's own budget since 0.16.0.
    #:
    #: It was uncapped until then, because a bound costs one grouped
    #: aggregate for every axis at once and the budget that governs counting
    #: need not govern a free measurement. That reasoning is about the
    #: SERVER's cost, and the budget is not about the server's cost: it is
    #: how wide a panel may be. Uncapped, a phones leaf shipped six wholesale
    #: measurements past a ``MAX_FACET_FIELDS`` of twelve, and a from/to
    #: picker takes exactly as much of a rail as a bucket list does.
    #:
    #: Still not the same list as ``slugs``: a TERM axis can carry numbers
    #: too (an imported leaf's ``year`` is a ``ref_select`` of numeric codes),
    #: so every admitted slug in the budget is offered to the bounder and the
    #: side table decides which of them has a number behind it.
    range_candidates: tuple[str, ...] = ()
    #: ``{slug: position}`` — where each axis sits in ONE panel, groups and
    #: ranges numbered together. The client needs it because the two halves
    #: arrive in different keys (``facets`` / ``facet_meta.ranges``) and a
    #: panel that draws all the choices and then all the measurements is not
    #: the page the category authored: on a cars leaf the schema puts «Цена»
    #: and «Год» among the makes and models, not below them. Core ranges take
    #: the first positions — they address a column every document in every
    #: corpus has, so they precede anything a category authored — and the
    #: rest follow in the plan's own order (mandatory first, then as
    #: authored, for a category's own schema).
    order: dict[str, int] = field(default_factory=dict)
    #: The subset of ``slugs`` admitted by the CANDIDATE SET's categories
    #: rather than by the queried category's own schema (``evidence_plan``).
    #: Empty for an authored plan. Only these are governed by
    #: ``FACET_MIN_COVERAGE``: a slug the queried category authored answers
    #: with its zeros on purpose (a closed option set that only ever shows
    #: values already present is a panel that cannot narrow anything), while
    #: a slug borrowed from a sibling leaf has earned nothing yet.
    evidence: tuple[str, ...] = ()


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
    "BandSummary",
    "Cursor",
    "ExtractedFilter",
    "Extraction",
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
