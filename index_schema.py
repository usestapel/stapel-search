"""The index contract as DATA: every field has a source, a read path, a test.

This is not a table for a human to read — it is the declaration the gate of
spec §11 runs on. :data:`INDEX_FIELDS` is emitted to ``docs/index.json``
(the module's sixth contract artifact, under the same ``make contract`` /
``make contract-check`` drift gate as the quintet), driven through a
runtime round-trip in ``tests/test_index_contract.py``, and checked
statically by ``stapel_tools.index_lint``.

The legacy this replaces died exactly here: ``features_search``,
``description_en`` and ``geohash`` were written, half-indexed, and read by
no query — for years. A round-trip test would have failed on day one, so
the dataclass below simply refuses to hold a field that has neither a read
path nor a test. You cannot declare an index field "for later".

Boundary, stated as plainly as ``surface_lint``'s SUR004 states its own
(``stapel-tools/stapel_tools/surface_lint.py:64-75``): these gates prove
**the promise was not dropped on the floor** — that a declared field is
reachable by a named query capability and that a test with that name
exists. They do not prove the branch is correct. Only the assertions
inside the round-trip do that, and they are written per field for exactly
that reason.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

#: Closed vocabulary for ``IndexField.kind``. Widening it must be a
#: deliberate act, which is what ``IDX005`` is for.
KINDS = (
    "text",
    "facet",
    "range",
    "sort",
    "geo",
    "score",
    "filter",
    "stored",
    "bookkeeping",
)

#: Read-path prefixes that name a QUERY capability — the ones ``IDX002``
#: requires every shipped backend to implement.
QUERY_READ_PATH_PREFIXES = ("filter:", "q", "facets:", "sort:", "score:", "geo:")

#: Read paths answered by the SERVICE layer, not by an engine. ``card`` and
#: ``promoted`` are read from this module's own table under every backend —
#: the ``stapel_geo`` rule one level up, "backends never shape summaries" —
#: so requiring an engine to implement them would be a rule that pushes you
#: to write a fake. ``IDX002`` checks these against the response builder
#: instead.
SERVICE_READ_PATH_PREFIXES = ("result.",)

#: Everything else (``guard:``, ``drift_check``, ``beat:``, ``health.``)
#: names machinery outside any query builder and is proven by its test
#: alone.

#: ``r.<slug>`` slugs that address a core document COLUMN instead of the
#: ``SearchNumber`` side table, as ``{slug: index field}``.
#:
#: Every other range filter is an *attribute* range: the number was written
#: to ``SearchNumber`` by a numeric ``FacetMapping``, and the predicate is an
#: indexed semi-join on ``(slug, value)``. Price is not an attribute — it is
#: a column of the listing itself — so that semi-join finds no row and
#: answers ``count: 0`` for every bound, at HTTP 200, with nothing in the
#: response saying the filter was never applied. That is what a live
#: classified stand shipped: a buyer could sort by price and not filter by
#: it, and ``r.price=10000..30000`` returned an empty board.
#:
#: ``price_base`` has declared ``filter:range`` among its read paths since
#: 0.1.0 — the claim was in the contract before the code was. This map is
#: what makes it true, and ``IDX002``-style coverage of it is
#: ``tests/test_core_ranges_and_labels.py`` plus the ``core_range_price``
#: conformance scenario every engine must pass.
#:
#: Adding an entry is a deliberate act: the slug becomes reserved fleet-wide,
#: so a category holding an attribute of the same name would be shadowed by
#: it. That is why the map is short and why it is here, in the contract,
#: rather than inside one backend.
CORE_RANGE_FIELDS: dict[str, str] = {"price": "price_base"}


@dataclass(frozen=True)
class IndexField:
    """One indexed field, with the three things that keep it alive."""

    field: str
    kind: str
    #: Where the value comes from: a source Function field, an inbound
    #: signal, or ``derived:<how>``.
    source: str
    #: Named query capabilities that READ this field.
    read_paths: tuple[str, ...]
    #: pytest node id proving the round trip.
    test: str
    #: What that test asserts — rendered into docs/index.json so the
    #: artifact explains itself to a reader who has no repo checkout.
    proves: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"IndexField({self.field!r}).kind={self.kind!r} is outside the closed "
                f"vocabulary {KINDS}"
            )
        if not self.read_paths:
            raise ValueError(
                f"IndexField({self.field!r}) declares no read_paths. A field nothing "
                "reads is the legacy defect this contract exists to prevent."
            )
        if not self.test:
            raise ValueError(
                f"IndexField({self.field!r}) declares no test. A round trip nobody "
                "proves is a promise, not a mechanism."
            )

    @property
    def query_read_paths(self) -> tuple[str, ...]:
        """Read paths a backend query builder must implement (``IDX002``)."""
        return self._matching(QUERY_READ_PATH_PREFIXES)

    @property
    def service_read_paths(self) -> tuple[str, ...]:
        """Read paths the response builder must answer (``IDX002``)."""
        return self._matching(SERVICE_READ_PATH_PREFIXES)

    def _matching(self, prefixes: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            path for path in self.read_paths if any(path.startswith(p) for p in prefixes)
        )


_T = "tests/test_index_contract.py::"


INDEX_FIELDS: tuple[IndexField, ...] = (
    IndexField(
        field="doc_type",
        kind="filter",
        source="registry",
        read_paths=("filter:type",),
        test=_T + "test_doc_type_scopes_the_corpus",
        proves="a query for another type does not find the document",
    ),
    IndexField(
        field="doc_key",
        kind="filter",
        source="source key",
        read_paths=("result.key", "filter:type"),
        test=_T + "test_doc_key_round_trips_and_delete_removes",
        proves="the key in the answer is the source key; delete removes it",
    ),
    IndexField(
        field="visible",
        kind="filter",
        source="derived:status in SourceSpec.visible_statuses",
        read_paths=("filter:type",),
        test=_T + "test_visible_is_the_implicit_predicate",
        proves="a document whose pulled status left the indexed set disappears",
    ),
    IndexField(
        field="language",
        kind="filter",
        source="listings.search_documents.language",
        read_paths=("filter:lang", "q"),
        test=_T + "test_language_filters_and_picks_the_fts_config",
        proves="a ru document is found by a stemmed ru form; an en one is not",
    ),
    IndexField(
        field="owner_key",
        kind="filter",
        source="listings.search_documents.owner_id",
        read_paths=("filter:owner",),
        test=_T + "test_owner_key_filters_to_one_seller",
        proves="the seller's own listings, and only those",
    ),
    IndexField(
        field="category_path",
        kind="facet",
        source="derived:categories.path over category_id",
        read_paths=("filter:category",),
        test=_T + "test_category_prefix_finds_descendants",
        proves="filtering by a parent category finds a child's document",
    ),
    IndexField(
        field="title",
        kind="text",
        source="listings.search_documents.title",
        read_paths=("q",),
        test=_T + "test_title_outranks_body",
        proves="a match in the title ranks above the same match in the body",
    ),
    IndexField(
        field="body",
        kind="text",
        source="listings.search_documents.description",
        read_paths=("q",),
        test=_T + "test_body_only_word_is_found",
        proves="a word occurring only in the description finds the document",
    ),
    IndexField(
        field="text_extra",
        kind="text",
        source="listings.search_documents.features_title",
        read_paths=("q",),
        test=_T + "test_badge_value_is_searchable",
        proves="a badge attribute's value finds the document",
    ),
    IndexField(
        field="text_vec",
        kind="text",
        source="derived:generated STORED tsvector, weights A/B/C",
        read_paths=("q",),
        test=_T + "test_text_vector_is_the_primary_arm",
        proves="the stemmed arm answers before any trigram fallback runs",
    ),
    IndexField(
        field="text_plain",
        kind="text",
        source="derived:lowercased, unaccented",
        read_paths=("q",),
        test=_T + "test_trigram_arm_tolerates_a_typo",
        proves="a misspelling finds the document only through the trigram arm",
    ),
    IndexField(
        field="facets",
        kind="facet",
        source="derived:DAO through FacetMapping",
        read_paths=("filter:facet",),
        test=_T + "test_facet_filter_excludes_non_matches",
        proves="filtering on a value drops the documents that lack it",
    ),
    IndexField(
        field="facet_terms",
        kind="facet",
        source="derived:facets, path slugs expanded to every prefix",
        read_paths=("facets:counts", "filter:facet"),
        test=_T + "test_facet_counts_match_the_candidate_set",
        proves="each count equals the number of candidate documents",
    ),
    IndexField(
        field="numbers",
        kind="range",
        source="derived:numeric FacetMappings (SearchNumber side table)",
        read_paths=("filter:range",),
        test=_T + "test_range_filter_includes_the_bound",
        proves="`2015..` includes 2015 and excludes 2014",
    ),
    IndexField(
        field="price_base",
        kind="sort",
        source="listings.search_documents.price_base",
        read_paths=("sort:price_asc", "sort:price_desc", "filter:range"),
        test=_T + "test_price_sorts_with_nulls_last_both_ways",
        proves="ordering holds and NULL price sorts last in both directions",
    ),
    IndexField(
        field="published_at",
        kind="sort",
        source="listings.search_documents.published_at",
        read_paths=("sort:newest",),
        test=_T + "test_newest_orders_by_publication",
        proves="newest first, with a stable doc_key tie-break",
    ),
    IndexField(
        field="popularity",
        kind="score",
        source="search.signal.popularity",
        read_paths=("score:popularity",),
        test=_T + "test_popularity_signal_moves_the_score",
        proves="a popularity signal raises the score; without one it is 0",
    ),
    # lat/lon are read by two populations of read path, and the split is the
    # point. `filter:*` and `sort:distance` EXCLUDE and ORDER — an engine
    # answers them. `result.band` and `result.card` only LABEL: the geo band
    # a row sits in, and the deliberately coarsened coordinates a public card
    # carries. A label is built by the response builder, which is why they sit
    # under the service prefix and not under `geo:`.
    IndexField(
        field="lat",
        kind="geo",
        source="listings.search_documents.lat",
        read_paths=(
            "filter:radius",
            "filter:bbox",
            "sort:distance",
            "result.band",
            "result.card",
        ),
        test=_T + "test_radius_includes_near_excludes_far",
        proves="a 5km-away document is inside r=10 and outside r=1",
    ),
    IndexField(
        field="lon",
        kind="geo",
        source="listings.search_documents.lon",
        read_paths=(
            "filter:radius",
            "filter:bbox",
            "sort:distance",
            "result.band",
            "result.card",
        ),
        test=_T + "test_bbox_crosses_the_antimeridian",
        proves="min_lon > max_lon selects the box across +/-180",
    ),
    IndexField(
        field="geohash",
        kind="geo",
        source="listings.search_documents.geohash",
        # One capability, two centres: the radius filter's box and the near
        # band's covering cell set are the same coarse indexed narrowing over
        # the same column, so the band adds no read path here.
        read_paths=("geo:prefilter",),
        test=_T + "test_geohash_prefilter_keeps_border_cells",
        proves="the candidate block does not drop a neighbouring-cell document",
    ),
    IndexField(
        field="boost",
        kind="score",
        source="search.signal.boost",
        read_paths=("score:promotion_boost",),
        test=_T + "test_boost_moves_relevance_only",
        proves="boost reorders under sort=relevance and not under sort=price_asc",
    ),
    IndexField(
        field="promoted",
        kind="stored",
        source="search.signal.promoted",
        read_paths=("result.promoted",),
        test=_T + "test_promoted_flag_is_on_every_item",
        proves="the DSA Art. 26 marker is present on every item, false included",
    ),
    IndexField(
        field="promotion_expires_at",
        kind="filter",
        source="search.signal.expires_at",
        read_paths=("beat:search_expire_signals",),
        test=_T + "test_expired_promotion_is_dropped",
        proves="past its expiry the document is no longer promoted or boosted",
    ),
    IndexField(
        field="card",
        kind="stored",
        source="mapper",
        read_paths=("result.card",),
        test=_T + "test_card_is_returned_without_a_second_query",
        proves="the stored row fields come back with the hit",
    ),
    IndexField(
        field="source_seq",
        kind="bookkeeping",
        source="derived:event timestamp / export row seq (unix ms)",
        read_paths=("guard:ordering",),
        test=_T + "test_stale_sequence_does_not_overwrite",
        proves="an out-of-order event does not overwrite a fresher document",
    ),
    IndexField(
        field="source_event_id",
        kind="bookkeeping",
        source="event.event_id",
        read_paths=("guard:idempotency",),
        test=_T + "test_redelivery_is_a_noop",
        proves="the same event delivered twice changes nothing",
    ),
    IndexField(
        field="indexed_at",
        kind="bookkeeping",
        source="derived:now",
        read_paths=("drift_check", "health.lag_seconds"),
        test=_T + "test_drift_is_detected",
        proves="a document missing from the index is reported by drift_check",
    ),
    IndexField(
        field="source_updated_at",
        kind="bookkeeping",
        source="listings.search_documents.updated_at",
        read_paths=("drift_check", "health.lag_seconds"),
        test=_T + "test_stale_documents_are_caught_up",
        proves="search_reindex_stale re-pulls a document the source moved on",
    ),
)


#: Physical model columns -> the index field they realize, or ``None`` for a
#: column that is bookkeeping of the table itself and not an indexed value.
#: ``IDX001`` reads this: adding a column to an index model without deciding
#: which side of this map it belongs on is a red build, which is the whole
#: point — "indexed silently" is legacy disease number one.
INDEX_MODEL_COLUMNS: dict[str, dict[str, str | None]] = {
    "SearchDocument": {
        "id": None,
        "doc_type": "doc_type",
        "doc_key": "doc_key",
        "visible": "visible",
        "language": "language",
        "owner_key": "owner_key",
        "category_path": "category_path",
        "title": "title",
        "body": "body",
        "text_extra": "text_extra",
        "text_plain": "text_plain",
        "facets": "facets",
        "facet_terms": "facet_terms",
        "price_base": "price_base",
        "published_at": "published_at",
        "popularity": "popularity",
        "lat": "lat",
        "lon": "lon",
        "geohash": "geohash",
        "boost": "boost",
        "promoted": "promoted",
        "promotion_expires_at": "promotion_expires_at",
        "card": "card",
        "source_seq": "source_seq",
        "source_event_id": "source_event_id",
        "indexed_at": "indexed_at",
        "source_updated_at": "source_updated_at",
    },
    "SearchNumber": {
        "id": None,
        "document": None,
        # The side table exists so a range predicate is an indexed semi-join
        # instead of a per-slug btree migration on a JSONB path; both columns
        # together ARE the declared `numbers` field.
        "slug": "numbers",
        "value": "numbers",
    },
}


def index_schema() -> dict:
    """The serializable form emitted to ``docs/index.json``."""
    return {
        "module": "stapel_search",
        "kinds": list(KINDS),
        "query_read_path_prefixes": list(QUERY_READ_PATH_PREFIXES),
        "service_read_path_prefixes": list(SERVICE_READ_PATH_PREFIXES),
        "core_range_fields": dict(CORE_RANGE_FIELDS),
        "model_columns": INDEX_MODEL_COLUMNS,
        "fields": [asdict(f) for f in INDEX_FIELDS],
    }


def render_index_json() -> str:
    """Deterministic text of ``docs/index.json`` (trailing newline included)."""
    return json.dumps(index_schema(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_index_json(path: str | Path) -> Path:
    """Write ``docs/index.json`` and return the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_index_json(), encoding="utf-8")
    return target


def field_names() -> tuple[str, ...]:
    return tuple(f.field for f in INDEX_FIELDS)


def all_read_paths() -> tuple[str, ...]:
    seen: list[str] = []
    for f in INDEX_FIELDS:
        for path in f.read_paths:
            if path not in seen:
                seen.append(path)
    return tuple(seen)


def all_query_read_paths() -> tuple[str, ...]:
    return _collect("query_read_paths")


def all_service_read_paths() -> tuple[str, ...]:
    return _collect("service_read_paths")


def _collect(attribute: str) -> tuple[str, ...]:
    seen: list[str] = []
    for f in INDEX_FIELDS:
        for path in getattr(f, attribute):
            if path not in seen:
                seen.append(path)
    return tuple(seen)


__all__ = [
    "CORE_RANGE_FIELDS",
    "INDEX_FIELDS",
    "INDEX_MODEL_COLUMNS",
    "KINDS",
    "QUERY_READ_PATH_PREFIXES",
    "SERVICE_READ_PATH_PREFIXES",
    "IndexField",
    "all_query_read_paths",
    "all_service_read_paths",
    "all_read_paths",
    "field_names",
    "index_schema",
    "render_index_json",
    "write_index_json",
]
