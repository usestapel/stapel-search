# stapel-search configuration

Everything is read through `search_settings` (`stapel_search.conf`) at call
time. Resolution per key: `settings.STAPEL_SEARCH` dict → flat Django setting
of the same name → environment variable → the default below.

`BACKEND` is the only `import_strings` key — the one REPLACE seam. The four
MERGE registries (`SOURCES`, `FACET_MAPPINGS`, `SCORERS`, `DICTIONARIES`) are
resolved *per entry*, because `None` has to be able to tombstone a builtin,
which a whole-dict import could not express.

Every default is closed. An installed but unconfigured stapel-search indexes
nothing and says so (`search.W001`), rather than guessing what a host wanted.

## The engine

| key | default | what it decides |
|---|---|---|
| `BACKEND` | `stapel_search.backends.postgres.PostgresSearchBackend` | Which engine answers. Postgres needs no new infrastructure and is honest about its limits; `stapel_search.backends.meili.MeilisearchBackend` (the `[meili]` extra) adds typo tolerance, exact totals and exact facet counts at any size; `stapel_search.backends.naive.NaiveSearchBackend` is the declared choice for SQLite demos and tests. Switching is this key plus `manage.py search_rebuild`, with no module code changed. |

## Corpora

| key | default | what it decides |
|---|---|---|
| `SOURCES` | `{}` | `{doc_type: dotted path to a SourceSpec}`. Empty by design: this module knows nothing about listings. A **composite** declares the entry, because a mapper is cross-domain glue. |
| `ACCEPT_FEATURES_SEARCH` | `True` | Whether a source that will not hand over full attribute DAOs may feed the index with its flattened projection. Lossy — no ranges, no colour axis, no unit context — and the loss is reported in `degraded[]`, not absorbed. |
| `CATEGORY_FEATURES_FUNCTION` | `"categories.features"` | The comm Function the facet plan comes from. No import of stapel-categories. |
| `CATEGORY_PATH_FUNCTION` | `"categories.path"` | The comm Function category ancestry comes from (stapel-categories 0.8+). Without a provider, `category_path` degrades to a single segment, `search.W006` says so at deploy time, and every answer carries `degraded: ["category_rollup"]`. Exact-category filtering keeps working; rollup does not. |
| `CATEGORY_SUGGEST_FUNCTION` | `"categories.suggest"` | The comm Function category NAME matching comes from (stapel-categories 0.9+). It is sent already-normalized terms — this module owns the query language, that one owns the tree. Without a provider `/suggest` still answers title prefixes, says `degraded: ["category_suggestions"]`, and `search.W008` says so at deploy time. When the provider answers but nothing matches strongly (brand words never appear in category names), the engine's optional `suggest_categories` verb supplies goods-driven rows instead; an engine without that verb answers name matches only and says `degraded: ["category_listing_suggestions"]`. |
| `CATEGORY_NAMES_FUNCTION` | `"categories.names"` | The comm Function goods-driven suggestion rows resolve display names through (stapel-categories 0.13+): one batched `{ids: […]}` read answering `{names: {id: {name, slug}}}`. A segment the provider does not answer for keeps its id — a truthful segment, never an invented name — and a provider that is missing or down leaves ids in place with `degraded: ["category_names"]`. The `category` filter string keeps the ids regardless: display changes, the tap does not. |
| `CATEGORY_CACHE_TIMEOUT` | `300` | Seconds a resolved facet plan / ancestry is memoized. Invalidated early by `category.changed`. |

## Facet semantics and cost

| key | default | what it decides |
|---|---|---|
| `FACET_MAPPINGS` | `{}` | `{attribute type slug: dotted path to a FacetMapping | None}`. Overlays the thirteen builtins (the two vocabulary-backed `ref_*` types and the composite `group` included). A host's own field type indexes by a generic default and raises `search.W002` until this declares — or explicitly confirms — the mapping. |
| `FACET_CANDIDATE_CAP` | `15000` | Above this many candidates the counts come from `TABLESAMPLE` and the answer says `approximate: true`. **Benchmark-calibrated, not guessed**: under eight concurrent clients, 20000 candidates already breach the 200ms target on the measurement rig (`tasks/search-facet-benchmark.md` §8.3). Re-validate on your own hardware before freezing it. |
| `MAX_FACET_FIELDS` | `12` | How many slugs one query may count. Drill-down semantics mean N active facet filters cost N+1 candidate scans; this is what stops a wide category page becoming a dozen sequential ones. Plan slugs past it land in `facet_meta.skipped`, never silently dropped. |
| `FACET_CACHE_TTL` | `30` | Seconds a candidate set is reusable across the facets of one query. |
| `MAX_FACET_VALUES` | `200` | Buckets one facet group may answer with, biggest first (ties by the term). A cap is a SEMANTIC, so every engine reads this one number — the Postgres backend carried it as a hardcoded `LIMIT 200` until 0.14.2 and the reference walk had none, which is two products. |
| `MAX_FACET_VALUES_VOCABULARY` | `1000` | The same cap for a **vocabulary-backed** group (`optionsRef`), where the option set is DATA rather than a hand-authored list. A live make dictionary holds 418 terms: at 200 the panel silently held the 200 commonest and nothing in the answer said the rest existed, so a client-side dictionary search could not find what it was never sent. Raise it if a level you facet on is larger; the buckets a query actually produces are bounded by the distinct values in its candidate set, never by the level's size. |
| `FACET_EVIDENCE_CATEGORIES` | `24` | How many categories the plan may draw feature definitions from when the queried category's own schema did not fill `MAX_FACET_FIELDS`. `categories.features` resolves own + **ancestor**-inherited features, so a branch owns no axes (its leaves do) and a text query names no category at all — both answered an empty panel over a corpus that plainly had axes (D175). The categories come from ONE grouped aggregate over the query's own candidate set (the optional backend verb `category_counts`), **not** from the catalogue subtree: on the reference stand `elektronika`'s subtree is 210 categories declaring 439 feature definitions and seven of them hold a listing. `0` turns it off and the answer is byte-identical to 0.11.x. An engine without the verb reports `facet_plan_evidence` in `degraded[]`. **Cost:** one grouped scan, plus one `categories.features` call per category in the aggregate — revision-cached for `CATEGORY_CACHE_TIMEOUT`, so this is a cold-cache cost, but a cold cache on a wide root pays it up to this many times. Lower it if your catalogue is broad and your comm transport is slow; the tail of the aggregate contributes slivers that `FACET_MIN_COVERAGE` withholds anyway. |
| `FACET_MIN_COVERAGE` | `0.05` | A slug admitted by that aggregate must describe at least this fraction of the candidate set, or it is withheld — and named, with its count, in `facet_meta.withheld`. Measured, not chosen: `/c/elektronika` on the stand holds 46 of 52 listings in one category and one listing in each of six others, 1.9% apiece. Coverage is the sum of a group's bucket counts, the same quantity `facetCoverage` in `@stapel/search-react` sorts the chip row and the rail by. Never applied to a slug the reader has already filtered on, nor to one the queried category authored itself — a closed option set answers with its zeros on purpose. `0` withholds nothing. |
| `MAX_RANGE_FILTERS` | `4` | Each range predicate is one indexed semi-join against the numeric side table. |

## Query surface

| key | default | what it decides |
|---|---|---|
| `SORTS` | `("relevance", "newest", "price_asc", "price_desc", "distance")` | The closed vocabulary a caller may ask for. `popular` is deliberately absent: no popularity signal exists in the fleet, and a sort that orders every document by zero is a promise with nothing behind it. Add it once you have an emitter for `search.signal`, and `search.W004` stops warning. |
| `DEFAULT_LANGUAGE` | `"en"` | The analyzer **and dictionary** used when neither `lang` nor `Accept-Language` says otherwise. Note that `lang` narrows the corpus only when a caller **typed** it — a browser's `Accept-Language` picks the analyzer and never hides a catalogue. **Set this to the board's own language.** Left at `"en"` on a Russian board, every header-less request is analyzed with the English dictionary: no ru equivalents, no transliteration, and the only symptom is lower counts. Measured live: `айфон` found 2 where `iphone` found 15 and `?lang=ru` found 16. `search.W007` warns at deploy time, and every answer now echoes the resolved `language`. |
| `MAX_QUERY_CHARS` | `200` | Longer is refused with `error.400.search_query_too_long`. |
| `MAX_QUERY_TERMS` | `32` | Counted AFTER synonym and transliteration expansion, because that is what actually reaches the engine. |
| `MAX_PAGE_SIZE` | `100` | A larger `limit` is clamped, not refused. |
| `DEFAULT_PAGE_SIZE` | `24` | |
| `MAX_RESULT_WINDOW` | `1000` | Paging past it is **refused** (`error.400.search_window_exceeded`), not truncated. Meilisearch has its own hard window, and one explicit shared limit is what makes the engines interchangeable instead of interchangeable-until-page-forty. |
| `QUERY_THROTTLE` | `"120/min"` | Rate for the `search-query` scope, from this module's namespace: a library does not own the project's `DEFAULT_THROTTLE_RATES`. |
| `SUGGEST_THROTTLE` | `"300/min"` | Rate for the `search-suggest` scope. Higher than `query` on purpose: a type-ahead fires per keystroke. |

## Suggestions

| key | default | what it decides |
|---|---|---|
| `SUGGEST_CATEGORY_CANDIDATES` | `200` | How many name matches `CATEGORY_SUGGEST_FUNCTION` may hand back for counting. The provider caps this too; both caps exist because a one-letter term matches most of a wide catalogue. |
| `SUGGEST_COUNT_CACHE_TTL` | `60` | Seconds the ONE per-`doc_type` category count aggregate is held. The indexer drops the entry when a batch lands, so this is the ceiling on staleness for a corpus nothing is writing to, not the usual case. |
| `DEFAULT_SUGGEST_LIMIT` | `10` | Rows per half when the caller does not say. |
| `MAX_SUGGEST_LIMIT` | `25` | A larger `limit` is clamped, not refused. |
| `SUGGEST_CACHE_SECONDS` | `30` | `Cache-Control: public, max-age=` on the suggest answer, and therefore how long a shared cache may serve it. The answer carries no per-user state at all, and an `ETag` makes a revalidation cheap. |

## Text

| key | default | what it decides |
|---|---|---|
| `FTS_CONFIGS` | `{"ru": "russian", "en": "english", "de": "german"}` | Language → Postgres text-search configuration. Read at write time (the backend writes the vector) and at query time, so adding a language is a settings change, not a migration. |
| `FTS_FALLBACK_CONFIG` | `"simple"` | Used for a language not in the map. |
| `TRANSLITERATE` | `{"ru": True}` | Whether Cyrillic and Latin spellings of a word find each other without a curated entry per brand. A curated synonym group always wins over the algorithm, because algorithmic transliteration is noisy. |
| `DICTIONARIES` | `{}` | `{language: [dotted path | filesystem path, ...]}` merged over the shipped `dictionaries/<lang>.json`. |
| `TYPO_FALLBACK_THRESHOLD` | `3` | Below this many hits from the exact arm, the trigram arm runs too. It keeps the AND semantics of the exact arm — a query that finds too little is widened in spelling, never in meaning. |
| `TRIGRAM_SIMILARITY` | `0.3` | `word_similarity` floor for the trigram arm. |

There is **no `UNACCENT` key**. Diacritic folding happens in Python
(`stapel_search.text.fold`) for indexed text and queries alike, so the
Postgres `unaccent` extension is not required and nothing degrades when a DBA
has not installed it. One folding path also means two backends cannot
disagree about what "e" equals. Cyrillic diacritics are deliberately kept: NFD
decomposes «й» into «и» + breve, and dropping it would merge two different
letters.

## Indexing and upkeep

| key | default | what it decides |
|---|---|---|
| `SCORERS` | `{}` | `{slug: dotted path to a Scorer | None}` over the five builtins. Weights here drive both the ranking and the generated P2B disclosure, so the two cannot drift. |
| `REINDEX_SCHEDULE` | `{"hour": 3, "minute": 20}` | Cadence of the nightly tombstone purge. |
| `STALE_REINDEX_SCHEDULE` | `{"minute": "*/10"}` | Cadence of the catch-up job. A beat schedule with no entry for it raises `search.W003`: a catch-up nobody schedules is a promise, not a mechanism. |
| `TOMBSTONE_RETENTION_DAYS` | `7` | How long an invisible row is kept before purging. The window is what stops a late event resurrecting a stale version. |

There is **no `INGEST_BATCH_MS` key**. A micro-batching window would hold a
lone event until the next one arrived, trading the ≤5s freshness target for a
saving the keyed-batch pull already provides on rebuild. Signals are indexed
on delivery.

## Vector similarity (flag-gated; default off)

The net under the deterministic floor: on a suggest miss the query is
embedded through the fleet's embedding seam and cosine-matched against the
registered corpora (`vector/`). Off means off — byte-identical answers, no
request, no cache read.

| key | default | what it decides |
|---|---|---|
| `VECTOR_SUGGEST` | `False` | The master switch. Off, the suggest answer is byte-identical to a deployment without the layer, and `search.similar` answers `degraded: ["vector_disabled"]`. |
| `VECTOR_EMBED_FUNCTION` | `"llm.embed"` | Comm name of the embedding Function (stapel-agent's provider seam). API keys, base URLs and proxies live behind THAT seam, never here. |
| `VECTOR_MODEL` | `"text-embedding-3-small"` | The embedding model. Together with `VECTOR_DIMENSIONS` it forms the model tag `<model>@<dims>` stamped on every stored row and cached query vector — changing either makes the needed re-embed detectable instead of silently wrong. |
| `VECTOR_DIMENSIONS` | `256` | Matryoshka cut requested from the provider. Short typed strings do not need 1536 dims; 256 keeps 100k vectors around 100MB. |
| `VECTOR_SIMILARITY_FLOOR` | `0.6` | Below this cosine similarity a hit does not exist. Embeddings degrade into noise gracefully; a dropdown must not. |
| `VECTOR_KIND_FLOORS` | `{}` | `{kind: floor}` overrides of the global floor. One floor cannot serve two corpora: a model's similarity range depends on what it ranges over — measured live, LaBSE separates cross-script brand labels cleanly around 0.65 while a 3.4k-name Russian category corpus puts character-overlap accidents («кросовки»~«Креветки») at 0.85. Calibrate per corpus on real typos; an explicit `floor` in a `search.similar` payload overrides both. |
| `VECTOR_TOP_K` | `10` | Neighbours fetched before the floor is applied. |
| `VECTOR_QUERY_CACHE_TTL` | `604800` | Seconds a query embedding is cached (a week). Type-ahead traffic is Zipfian: every repeat of a popular misspelling is a cache hit instead of a proxied API round trip. |
| `VECTOR_EMBED_TIMEOUT` | `3` | Hard cap (seconds) on one embedding round trip from the query path. |
| `VECTOR_QUERY_NORMALIZER` | `"stapel_search.vector.seam.fold_normalizer"` | The seam to the deterministic normalization layer: dotted path of a `Callable[[str, str], str]` — `(raw query, language) -> canonical string`, used as both embedding input and cache key. Point it at the shared translit/alias canon when that layer exports one. |
| `VECTOR_CORPORA` | `{}` | MERGE registry `{kind: dotted path}` of zero-arg providers yielding `{"key", "text", "payload"}`. Empty by design (the `SOURCES` rule): the composite that knows categories and vocabularies declares the entries. Build/refresh with `manage.py search_vector_index` (`--estimate` prices the corpus first). |

## Query understanding (flag-gated; default off)

A free-text query is matched not only as words but against the option space
of the resolved category, so «красные штаны» arrives as `f.color=krasnyy`
plus one word of text. What is extracted becomes a PRE-APPLIED, visible,
removable filter — never a hidden narrowing. Off means off: no extraction,
no `query_understanding` key, byte-identical answers.

| key | default | what it decides |
|---|---|---|
| `QUERY_UNDERSTANDING` | `False` | The master switch. Off, a query is text and nothing more, exactly as in 0.11.x. |
| `UNDERSTANDING_EXACT_CONFIDENCE` | `1.0` | Confidence stamped on a folded-caption or option-code hit. Not a threshold — these are exact by construction; the number exists so a UI can render one chip differently from another and the eval can slice by rung. |
| `UNDERSTANDING_TRANSLIT_CONFIDENCE` | `0.95` | The same, for a token that transliterates onto an option code. |
| `UNDERSTANDING_VECTOR_FLOOR` | `0.86` | The line between a FILTER and a SIGNAL. Deliberately far above `VECTOR_SIMILARITY_FLOOR`: a suggestion a human reads and ignores may be wrong, a filter that silently narrows an answer may not — a wrong applied filter is indistinguishable from an empty catalogue. Below the floor a hit still survives as `applied=False` and ranks through `match_count` without excluding anything. |
| `UNDERSTANDING_MAX_FILTERS` | `4` | Applied filters one query may produce. The cap spends its slots on the strongest candidates and DEMOTES the rest to signals rather than dropping them. |
| `UNDERSTANDING_MATCH_FUNCTION` | `"vocabularies.match"` | Comm name of the term resolver. A `ref_select` addresses a (vocabulary, level) nobody may enumerate — 697 pairs over 815k terms on the live stand — so the term is resolved through stapel-vocabularies' own exact/prefix/vector ladder rather than a second one here. |

## Geo bands (flag-gated; default off)

Distance stops being a filter and becomes a LABEL. An answer is ordered
«near» first, then «far» — and the far band carries every remaining row, so
a query can never return zero because of geo. `radius_km` and `bbox` remain
the only inputs that actually exclude a row.

| key | default | what it decides |
|---|---|---|
| `GEO_BANDS` | `False` | The master switch. Off, no `band` on a hit and no `bands` on the answer. |
| `NEAR_BAND_RADIUS_KM` | `25.0` | The near band's edge. A band edge, never a radius filter — folding the two together is exactly how a band becomes a hidden filter again. |
| `NEAR_BAND_CELL_PRECISION` | `4` | Geohash precision the band's covering cell set is built at. Measured: a 25km disc covers 16–20 cells of ~39×19.5km, each an indexed prefix range on the existing `varchar_pattern_ops` index. Raising it multiplies the cells; lowering it hands the haversine far more rows than it needs. |
| `NEAR_BAND_MAX_CELLS` | `64` | Above this the OR-of-prefixes stops paying for itself and the band falls back to the bounding box. |
| `CARD_COORD_PRECISION` | `2` | Decimal places a PUBLIC card's coordinates are rounded to (~1.1km) — the approximate area the product already draws on a map, never the seller's pin. Mirrors stapel-listings' `PUBLIC_COORD_PRECISION`; the two must agree, because it is one published area. It must equal stapel-listings' `PUBLIC_COORD_PRECISION` — `stapel_search.W010` says so at boot when it does not, because two differently-sized areas around one point intersect and the finer one decides what a stranger can resolve. It is the whole **public geo grid**, not only the card's: for an anonymous reader every geo answer — `distance_km`, the radius cut, the `nearby` band, `bbox` — is measured against this grid, and `distance_km` is floored to its cell diagonal (~1.574km). A coarse card beside a fine distance is not a coarse answer: the caller's centre is continuous, so three requests rebuild the pin. Staff, the row's owner and the service transport get the exact numbers. |

The store is Postgres + the `pgvector` extension; the metadata table exists
everywhere (migration `0003`), the `vector` column only where the extension
does. Without it the layer degrades to `degraded: ["vector_suggestions"]`
and `stapel_search.W009` says so at boot — same posture as every other
engine difference.

## Meilisearch (read only by that backend)

| key | default | what it decides |
|---|---|---|
| `MEILI_URL` | `"http://127.0.0.1:7700"` | |
| `MEILI_KEY` | `""` | |
| `MEILI_TIMEOUT` | `5` | Seconds; also the floor for waiting on an indexing task. Writes wait for their task, because "searchable within 5s" must not be a coin flip. |

## Testing

Two environment variables gate the suites that need real infrastructure —
behaviour that cannot be faked is tested against a real server or skipped,
never simulated:

- `STAPEL_SEARCH_TEST_DB` — a Postgres URL (`postgres://user:pass@host:port/db`).
  Runs the whole suite, including the index-contract table, against the
  Postgres backend.
- `STAPEL_SEARCH_MEILI_URL` (+ optional `STAPEL_SEARCH_MEILI_KEY`) — runs the
  conformance suite against Meilisearch.

`make containers-up` starts both as throwaway containers; `make test-postgres`
and `make test-meili` run against them.
