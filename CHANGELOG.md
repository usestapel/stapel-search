# Changelog

All notable changes to stapel-search are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-08-21

First cut: a materialized search index with one swappable engine seam, three
shipped engines behind a single conformance suite, and a mechanical gate
against the defect that killed the legacy it replaces.

### The index

- `SearchDocument` / `SearchNumber` / `SearchSignal`, owned by this module in
  **both** topologies. `Projection`'s local mode has no table and only a keyed
  batch read, and search answers "find the matching ones" rather than "give me
  fields for these keys" — so the index is a real table, and all four
  Projection guarantees are borrowed **by name** instead: idempotency on
  redelivery (`source_event_id`), ordering (`source_seq`), `rebuild` from the
  owner's snapshot with the `{cursor, limit} → {rows, cursor, total}` contract
  verbatim, and `drift_check`.
- `SearchNumber` is a narrow side table because a btree on a JSONB path would
  need a migration per slug — impossible against an open category schema — and
  `jsonb_path_ops` answers containment, not `<`/`>`. Every range predicate is
  an indexed semi-join instead.
- Models carry `@access.ops`: an admin hand-editing a derived row produces a
  value the next re-index silently reverts, so the admin is read-only too.

### Event → pull

- Sources are an open registry (`BUILTIN_SOURCES = {}`). A `SourceSpec` names
  the invalidation signals, the keyed-batch content Function, the cursor
  snapshot Function and the mapper — and it is declared by the **composite**,
  the one place allowed to know both the corpus and the index.
- The `listing.*` payloads carry identity only, so the event is a signal and
  the document is pulled over comm. The same `call()` is in-process in a
  monolith and a bus round trip in a split.
- **Visibility comes from the pulled document's `status`, never from the event
  name.** Republishing a live listing emits `listing.updated` with
  `status: pending` and emits no `listing.removed` at all; an index reading
  event names would keep a withdrawn listing searchable. A key the source no
  longer serves is the source saying "deleted".
- One dispatcher serves every registered source (the stapel-docs INGEST form);
  a broken registry entry raises `ImproperlyConfigured` rather than
  log-and-skip.

### Engines

- **`PostgresSearchBackend` (default)** — `russian`-family FTS with per-language
  configs, a `pg_trgm` second arm that keeps the AND semantics of the first
  (widening spelling, never meaning), and the two GIN structures: `facets`
  jsonb with `jsonb_path_ops` for filtering, `facet_terms text[]` for counting.
  The three Postgres-only columns are added by migration 0002 and maintained by
  the backend, which keeps the model portable and `FTS_CONFIGS` a setting
  rather than something frozen into a migration.
- **`MeilisearchBackend`** behind the `[meili]` extra — native typo tolerance,
  exact totals, exact facet counts and `_geoRadius`. Ordering, scoring and
  keyset pagination run through the **shared** code the naive backend runs, so
  two engines agree because they execute the same ordering function.
- **`NaiveSearchBackend`**, declared — it exists so the Postgres backend never
  has to degrade into a silent `icontains`. A SQLite demo gets an honest
  `typo_tolerance: False` instead of a worse engine wearing the same name.
- **`OpenSearchBackend`** — a pointer, not a promise.
- Differences travel in `BackendCapabilities` and reach the caller as
  `degraded: [...]`. Nothing degrades into a log line.

### The conformance suite

- `stapel_search.testing` — 48 scenarios, public and importable, written
  **before** the second backend rather than after. All three shipped engines
  pass it; Postgres skips exactly the two capabilities it honestly declares
  false, and Meilisearch skips none.
- Release rule: a new backend without a green run does not merge. A scenario
  may be skipped only when the matching capability is `False`, and it *fails*
  when the capability is `True` and the behaviour diverges.

### The gate against "indexed silently, read by nothing"

- `INDEX_FIELDS` — 28 fields, each with a source, the query capabilities that
  READ it, and the pytest node id that proves the round trip. Emitted to
  `docs/index.json`; the dataclass refuses a field with no read path and no
  test, so "for later" is not expressible.
- `tests/test_index_contract.py` — one round trip per field, against the
  configured engine, every one with a mandatory negative half.
- `stapel-index-lint` (stapel-tools 0.42.0, composed into `stapel-verify`) —
  IDX001…IDX005 statically. It found four real dead-haulage fields on its
  first run here; they are now waived by name with reasons.

### Query contract

- `type`, `q`, `lang`, `category` (prefix rollup), `f.<slug>` (OR within,
  AND between), `r.<slug>` ranges, `lat`/`lon`/`radius_km` or `bbox`, five
  sorts, `facets=on|off|<slugs>`, and an opaque keyset cursor whose envelope
  is `AnchorPagination`'s exactly — `AnchorPagination` anchors on one model
  field and relevance is not a column in any engine, so the cursor differs and
  the envelope does not.
- `min_lon > max_lon` crosses the antimeridian, borrowed verbatim from
  `GeoSearchBackend.bbox` and the first scenario of the conformance suite.
- Deep paging past `MAX_RESULT_WINDOW = 1000` is **refused**, not truncated:
  one explicit shared limit is what makes the engines interchangeable.
- `lang` picks the analyzer always and narrows the corpus only when a caller
  typed it — an `Accept-Language` header must not hide a whole catalogue.

### Facets

- Drill-down semantics: each facet is counted with **its own filter removed**,
  so choosing a value does not zero its neighbours.
- `FACET_CANDIDATE_CAP = 15000`, calibrated by benchmark rather than feel: a
  single connection stays comfortable to ~130k candidates while eight
  concurrent clients breach the 200ms target at ~31k. The `TABLESAMPLE`
  fallback is live from day one and reports `approximate: true`.
- A closed option set answers with **every** option, zeros included — that is
  the reason a category plan exists rather than "count what showed up".
- Facet semantics per attribute type live here, keyed by type slug and cached
  on stapel-attributes' own `registry_version()`. An eleventh, host-registered
  type indexes by a generic default **and raises `search.W002`**.

### Ranking and disclosure

- `GET /search/api/v1/ranking` and `docs/ranking.json` are rendered from the
  scorer registry that does the ranking, under the same drift gate as every
  other artifact — the P2B Art. 5 text cannot drift from the behaviour.
- `promoted` is serialized on every item under every sort, false included
  (DSA Art. 26).
- An explicit sort never receives a promotional boost — structural, not a
  setting: `promotion_boost` declares `relevance` and nothing else.
- `popular` is absent from the shipped sorts, because no popularity signal
  exists in the fleet. `search.W004` applies the same rule to a host.

### Text

- One normalizer for every engine: rewrites, stopwords, symmetric synonym
  groups, transliteration. Morphology stays in the engine. Stopwords are
  removed from the query only — a corpus indexed without them can only be
  repaired by reindexing everything.
- Diacritic folding is done in Python for indexed text and queries alike, so
  the Postgres `unaccent` extension is not required and nothing degrades
  without it. Cyrillic diacritics are deliberately preserved: NFD decomposes
  «й» into «и» + breve, and dropping it would merge two different letters.
- Shipped `ru` and `en` dictionaries as contract data, extensible by settings
  or at runtime, with `manage.py search_dictionary_lint` over cycles,
  duplicate group membership and stopword/synonym collisions.

### Surface and operations

- `GET query|suggest|ranking` (anonymous, module-namespaced throttles),
  `GET health` and `POST reindex` (operator).
- comm Functions `search.query` and `search.reindex`; Actions consumed:
  `search.signal`, `category.changed`, `user.deleted` (GDPR erasure reaches
  the index — derived data is still data).
- Management commands `search_rebuild`, `search_drift_check`,
  `search_apply_settings`, `search_dictionary_lint`; beat tasks
  `search_reindex_stale`, `search_expire_signals`, `search_purge_tombstones`.
- Seven contract artifacts under one drift gate: the quintet plus
  `index.json` and `ranking.json`. `translations/errors.{ru,es}.json` ship in
  the first release — the stapel-docs lesson, not repeated.
- `e2e/run_e2e.py` runs one identical assertion function against Postgres and
  then Meilisearch, after changing a single settings key. That is the proof
  the seam is real, and it also measures the ≤5s freshness target.

### Known gaps

- `categories.path` has no provider in the fleet. The canonical name is
  declared now; until something answers it, `category_path` degrades to one
  segment — exact-category filtering works, rollup does not, `search.W006`
  says so at deploy time and every answer carries
  `degraded: ["category_rollup"]`.
