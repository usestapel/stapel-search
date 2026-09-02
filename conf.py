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
