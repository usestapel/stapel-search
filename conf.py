"""Settings namespace for stapel-search.

All configuration is read through ``search_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at
import). Resolution order per key: ``settings.STAPEL_SEARCH`` dict -> flat
Django setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string``: ``BACKEND`` is the single REPLACE seam of the module
(the ``stapel-geo`` ``SEARCH_BACKEND`` form, one level up). The MERGE
registries (``SOURCES`` / ``FACET_MAPPINGS`` / ``SCORERS`` /
``DICTIONARIES``) are NOT import_strings: their values are dotted paths
resolved per entry by ``registry.py``, where ``None`` tombstones a builtin.

Every default is closed: an installed-but-unconfigured stapel-search
indexes nothing and says so (``search.W001``) rather than guessing what a
host wanted indexed.
"""
from stapel_core.conf import AppSettings

#: Top-level literal so the capabilities emitter can read it without
#: booting the settings layer, and so a reader can diff the defaults
#: against CONFIG.MD without running anything.
DEFAULTS = {
    # --- the one REPLACE seam ------------------------------------------
    # Dotted path to a class implementing stapel_search.backends.base
    # .SearchBackend. Swapping engines is this key plus a rebuild; no
    # module code changes (spec §9.3, proven by the e2e seam test).
    "BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend",
    # --- MERGE registries (BUILTIN -> settings overlay -> runtime) -----
    # {doc_type: dotted path to a mapper callable}. Empty by design:
    # this module knows nothing about listings. The composite that is
    # allowed to know both sides declares the entry (spec §3).
    "SOURCES": {},
    # {attribute type slug: dotted path to a FacetMapping | None}.
    "FACET_MAPPINGS": {},
    # {scorer slug: dotted path to a Scorer | None}.
    "SCORERS": {},
    # {language: [dotted path | filesystem path, ...]}.
    "DICTIONARIES": {},
    # --- query surface (closed switches, not open strings) -------------
    # `popular` is deliberately absent: no popularity signal exists in
    # the fleet (spec §7.3 / verdict §18.3). A host that has an emitter
    # for `search.signal` popularity adds it here and search.W004 stops
    # warning.
    "SORTS": ("relevance", "newest", "price_asc", "price_desc", "distance"),
    "DEFAULT_LANGUAGE": "en",
    "MAX_QUERY_CHARS": 200,
    "MAX_QUERY_TERMS": 32,
    "MAX_FACET_FIELDS": 12,
    "MAX_RANGE_FILTERS": 4,
    "MAX_PAGE_SIZE": 100,
    "DEFAULT_PAGE_SIZE": 24,
    # Deep pagination is refused, not silently truncated: Meilisearch has
    # its own hard window, and one explicit shared limit is what makes
    # the backends interchangeable (spec §7.5, verdict §18.7).
    "MAX_RESULT_WINDOW": 1000,
    # --- facet counting ------------------------------------------------
    # Benchmark-calibrated (tasks/search-facet-benchmark.md §8.3): 20000
    # already breaches the 200ms target under 8 concurrent clients on the
    # proxy rig. Re-validate on the target 12c/64g rig before freezing.
    "FACET_CANDIDATE_CAP": 15000,
    "FACET_CACHE_TTL": 30,
    # --- a plan the RESULT SET justifies (D175) ---------------------------
    # How many categories the plan may draw feature definitions from when
    # the queried category's own schema did not fill MAX_FACET_FIELDS — a
    # branch owns nothing, and a text query names no category at all, so
    # for both of them "the category's plan" is an empty panel over a
    # corpus that plainly has axes. 0 turns the mechanism off and the
    # answer is byte-identical to 0.11.x.
    #
    # The categories come from ONE aggregate over the query's own candidate
    # set, not from the catalogue subtree: on the reference stand the
    # subtree of `elektronika` is 210 categories declaring 439 feature
    # definitions, and SEVEN of them hold a listing.
    "FACET_EVIDENCE_CATEGORIES": 24,
    # A slug admitted by that aggregate must describe at least this
    # fraction of the candidate set, or it is withheld and SAID to be
    # withheld. 5% is measured, not chosen: `/c/elektronika` on the stand
    # holds 46 of 52 listings in one category and one listing in each of
    # six others — 1.9% apiece, six axes that narrow to a single row.
    # A slug the reader has already filtered on is never withheld, and a
    # slug the queried category authored itself is not governed by this at
    # all (a closed option set answers with its zeros on purpose).
    "FACET_MIN_COVERAGE": 0.05,
    # --- text ------------------------------------------------------------
    "FTS_CONFIGS": {"ru": "russian", "en": "english", "de": "german"},
    "FTS_FALLBACK_CONFIG": "simple",
    # NOTE there is no UNACCENT key. Diacritic folding happens in Python
    # (stapel_search.text.fold) for both the indexed text and the query,
    # so the Postgres `unaccent` extension is not required and nothing
    # degrades when a DBA has not installed it. One folding path also
    # means the backends cannot disagree about what "e" equals.
    # Language-conditioned default, not an open switch (verdict §18.9).
    "TRANSLITERATE": {"ru": True},
    # Below this many primary-arm hits the trigram arm runs too.
    "TYPO_FALLBACK_THRESHOLD": 3,
    "TRIGRAM_SIMILARITY": 0.3,
    # --- ingest ----------------------------------------------------------
    # NOTE there is no INGEST_BATCH_MS. A micro-batching window would hold
    # a lone event until the next one arrived, trading the <=5s freshness
    # target for a saving the keyed-batch pull already provides on
    # rebuild. Signals are indexed on delivery.
    # Accept the source's `features_search` projection when a mapper does
    # not hand over full DAOs. Lossy (spec §5.3) — declared in degraded[].
    "ACCEPT_FEATURES_SEARCH": True,
    # Category schema + ancestry, by comm name (NO import of
    # stapel-categories). CATEGORY_PATH_FUNCTION has no provider in the
    # fleet yet: absent, category_path degrades to a single segment and
    # search.W006 says so (see CONFIG.MD).
    "CATEGORY_FEATURES_FUNCTION": "categories.features",
    "CATEGORY_PATH_FUNCTION": "categories.path",
    # Category NAME matching for the type-ahead, by comm name. Same rule as
    # the two above: the tree's names, ancestry and retired state are
    # stapel-categories', asked for rather than mirrored. Unreachable, the
    # dropdown keeps its `terms` half and says `degraded:
    # ["category_suggestions"]` (search.W007).
    "CATEGORY_SUGGEST_FUNCTION": "categories.suggest",
    # Display names for category ids, by comm name — what lets a GOODS-driven
    # suggestion row (a category offered because matching documents live
    # there, not because its name says the word) read as a place instead of
    # a number. Same ownership rule again: ids are this module's, names are
    # stapel-categories'. Unreachable, the rows keep their id segments and
    # the answer says `degraded: ["category_names"]`.
    "CATEGORY_NAMES_FUNCTION": "categories.names",
    "CATEGORY_CACHE_TIMEOUT": 300,
    # Source document pull, by comm name; overridable per source entry.
    "REINDEX_SCHEDULE": {"hour": 3, "minute": 20},
    "STALE_REINDEX_SCHEDULE": {"minute": "*/10"},
    "TOMBSTONE_RETENTION_DAYS": 7,
    # --- suggestions -------------------------------------------------------
    # How many name matches `categories.suggest` may hand back for counting.
    # The provider caps this too; both caps exist because a one-letter term
    # matches most of a wide catalogue.
    "SUGGEST_CATEGORY_CANDIDATES": 200,
    # Seconds the ONE per-doc_type category count aggregate is held. The
    # indexer drops the entry when a batch lands, so this is the ceiling on
    # staleness for a corpus nothing is writing to, not the usual case.
    "SUGGEST_COUNT_CACHE_TTL": 60,
    # Largest `limit` a suggest request may ask for.
    "MAX_SUGGEST_LIMIT": 25,
    "DEFAULT_SUGGEST_LIMIT": 10,
    # --- vector similarity (flag-gated; default OFF) ----------------------
    # The net under the deterministic floor: on a suggest miss the query is
    # embedded (through the fleet's `llm.embed` seam) and cosine-matched
    # against the corpora registered in VECTOR_CORPORA. Off means off:
    # byte-identical answers, no request, no cache read. See vector/.
    "VECTOR_SUGGEST": False,
    # Comm name of the embedding Function (stapel-agent's provider seam).
    # Keys, base URLs and proxies live THERE, never here.
    "VECTOR_EMBED_FUNCTION": "llm.embed",
    # The embedding space, jointly: <model> at <dimensions> is stamped on
    # every stored row and every cached query vector as `<model>@<dims>`,
    # so changing either makes the needed re-embed DETECTABLE (searches
    # filter by tag and find nothing) instead of silently wrong.
    # text-embedding-3-small at 256 (matryoshka cut): short typed strings
    # do not need 1536 dims, and 256 keeps 100k vectors ~100MB.
    "VECTOR_MODEL": "text-embedding-3-small",
    "VECTOR_DIMENSIONS": 256,
    # Below this cosine similarity a hit does not exist. Embeddings degrade
    # into noise gracefully; a dropdown must not.
    "VECTOR_SIMILARITY_FLOOR": 0.6,
    # {kind: floor} overrides of the global floor, because one floor cannot
    # serve two corpora: a model's similarity range depends on what it is
    # ranging over. Calibrate per corpus on real typos (see CONFIG.MD).
    "VECTOR_KIND_FLOORS": {},
    "VECTOR_TOP_K": 10,
    # Seconds a query embedding is cached (a week): type-ahead traffic is
    # Zipfian, and every repeat of a popular misspelling is a cache hit
    # instead of a proxied API round trip.
    "VECTOR_QUERY_CACHE_TTL": 604800,
    # Hard cap (seconds) on one embedding round trip from the query path.
    "VECTOR_EMBED_TIMEOUT": 3,
    # The seam to the deterministic normalization layer: dotted path of a
    # Callable[[str, str], str] (raw query, language) -> canonical string,
    # used as both embedding input and cache key. Point it at the shared
    # translit/alias canon when that layer exports one.
    "VECTOR_QUERY_NORMALIZER": "stapel_search.vector.seam.fold_normalizer",
    # MERGE registry {kind: dotted path of a zero-arg provider yielding
    # {"key", "text", "payload"}}. Empty by design (the SOURCES rule):
    # the composite that knows categories and vocabularies declares these.
    "VECTOR_CORPORA": {},
    # --- Query understanding: a query's words become filters --------------
    # Off by default. With it off the answer is byte-identical to 0.11.x:
    # a query is text and nothing more. The switch flips on the labelled
    # eval, not on a hunch.
    "QUERY_UNDERSTANDING": False,
    # Confidence STAMPED on a deterministic option match. These are not
    # thresholds — a folded-label or transliterated-code hit is exact by
    # construction; the numbers exist so a frontend can render one chip
    # differently from another, and so the eval can slice by method.
    "UNDERSTANDING_EXACT_CONFIDENCE": 1.0,
    "UNDERSTANDING_TRANSLIT_CONFIDENCE": 0.95,
    # Cosine floor for a VECTOR-matched option. Deliberately far above
    # VECTOR_SIMILARITY_FLOOR (0.6): a suggestion a human reads and ignores
    # may be wrong, a filter that silently narrows the answer may not. A
    # wrong applied filter is indistinguishable from an empty catalogue.
    "UNDERSTANDING_VECTOR_FLOOR": 0.86,
    # At most this many filters come out of one query; the rest of the
    # words stay text. A query is not a form.
    "UNDERSTANDING_MAX_FILTERS": 4,
    # The comm Function resolving one term inside one (vocabulary, level) —
    # stapel-vocabularies' own exact/prefix/vector ladder, reused rather
    # than reimplemented here.
    "UNDERSTANDING_MATCH_FUNCTION": "vocabularies.match",
    # --- Geo bands: a label, never a filter -------------------------------
    # Off by default. On, an answer is ordered near-first but NOTHING is
    # withheld: the far band carries every remaining row, so a query can
    # never return zero because of distance.
    "GEO_BANDS": False,
    # The near band's edge. A BAND EDGE, not a radius filter: `radius_km`
    # remains the only thing that actually excludes a row.
    "NEAR_BAND_RADIUS_KM": 25.0,
    # Geohash precision the near band's covering cell set is built at. 4 is
    # measured: a 25km disc covers 16-20 cells of ~39x19.5km, each an
    # indexed prefix range. Raising it multiplies the cell count; lowering
    # it hands the haversine far more rows than it needs.
    "NEAR_BAND_CELL_PRECISION": 4,
    # Above this many covering cells the OR-of-prefixes stops paying for
    # itself and the band falls back to the bounding box.
    "NEAR_BAND_MAX_CELLS": 64,
    # Decimal places a PUBLIC card's coordinates are rounded to. 2 is ~1.1km
    # — the approximate area the product already draws on a map, never the
    # seller's pin. Mirrors stapel-listings' PUBLIC_COORD_PRECISION, and the
    # two must agree: one product, one published area.
    #
    # It is the WHOLE public geo grid, not just the card's. Every geo answer
    # an anonymous reader gets — the distance on a hit, the radius cut, the
    # nearby band, the bbox — is measured against this grid, because a coarse
    # card beside a fine distance is not a coarse answer: the caller's centre
    # is continuous, so an oracle reading the true point can be probed to
    # arbitrary precision by moving it. Raising this number narrows the area
    # AND every answer with it. See `backends/_shared.py`, "the public grid".
    "CARD_COORD_PRECISION": 2,
    # --- HTTP ------------------------------------------------------------
    "QUERY_THROTTLE": "120/min",
    "SUGGEST_THROTTLE": "300/min",
    # `Cache-Control: public, max-age=<this>` on the suggest answer, which is
    # also how long a shared cache may serve it. A type-ahead is the highest
    # request rate in the product and its answer is the same for everyone:
    # the anonymous dropdown carries no per-user state at all.
    "SUGGEST_CACHE_SECONDS": 30,
    # --- Meilisearch (read only by the meili backend) ---------------------
    "MEILI_URL": "http://127.0.0.1:7700",
    "MEILI_KEY": "",
    "MEILI_TIMEOUT": 5,
}

search_settings = AppSettings(
    "STAPEL_SEARCH",
    defaults=DEFAULTS,
    import_strings=("BACKEND",),
)

__all__ = ["DEFAULTS", "search_settings"]
