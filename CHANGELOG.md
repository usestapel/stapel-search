# Changelog

All notable changes to stapel-search are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-08-30

### Added — `user.merged`: a guest's documents stay findable after signing in

When stapel-auth folds an anonymous guest into an existing account on sign-in
it DELETES the guest row. `SearchDocument.owner_key` is a copy of the source's
owner, and nothing else in the fleet corrects it: the source modules re-parent
their own rows with a bulk `UPDATE` and emit no per-document signal, so an
index left alone kept every one of the guest's documents filed under an id
that can no longer sign in — missing from "my listings", and never erased,
because no erasure was ever requested for it. `user.deleted` was the wrong
tool for it in both directions: that handler *removes* documents, and a merge
removes nothing.

stapel-core 0.52.1 makes the omission a system-check ERROR
(`stapel_core.lifecycle.E001`): an app that answers one half of an account's
life cycle and not the other has a silent wrong answer for the other half, and
the failure has no symptom at the seam.

`services.reassign_owner(from_key, into_key)` is the new service; the
`user.merged` subscriber in `actions.py` is a thin wrapper over it.

- **It is a re-index, not an `UPDATE`.** The index has two halves. Rewriting
  only the table would leave the engine's copy of every document owned by the
  account that stopped existing, so each touched row is pushed back through
  the same `_row_to_index_document` path a signal write uses.
- **It is deliberately not a re-pull**, which is the shape every other
  invalidation in this module takes. Two reasons: `ingest` re-reads the
  source, which may not have processed its own merge yet, and would then
  re-stamp the guest's id onto the row this handler had just corrected — with
  no per-document signal ever coming to fix it; and `ingest` treats a
  document missing from the source's answer as a *delete*, which a transport
  hiccup during a merge must not trigger. A merge changes exactly one indexed
  thing, and the row already holds the rest.
- **Signal-owned columns are untouched** (`boost`, `promoted`, `popularity`,
  `promotion_expires_at`), and so are the ordering tokens (`source_seq`,
  `source_event_id`) — those record the SOURCE's last word about a document,
  and a merge is not one. A merge must not undo a promotion, and must not make
  a later real event look stale.
- **No ordering raise.** Unlike the modules that hold a real FK to the user
  table, `owner_key` is an opaque `CharField`: nothing has to exist here
  before the id can be written, so there is no "survivor not projected yet"
  case to retry. Idempotent for the same reason — a redelivery matches no rows
  and reports zero.
- **Malformed and missing ids are logged and dropped.** An escaping exception
  is a poison pill the bus redelivers forever, and no redelivery fixes a typo.

The handler walks index rows rather than the source registry, so a host that
has dropped a source from `STAPEL_SEARCH["SOURCES"]` has not thereby given up
its documents' ownership.

Schema: `schemas/consumes/user.merged.json`. Tests: `tests/test_user_merged.py`.
No migration, no query-surface change.

## [0.2.2] — 2026-08-24

### Fixed — `stapel_search.__version__` said 0.2.0 in the 0.2.1 release

The version bump moved `pyproject.toml` and not the module constant beside it,
so 0.2.1 shipped reporting itself as 0.2.0. Package metadata was correct (pip
resolves the right thing), but anything reading `stapel_search.__version__` —
a host's drift check, a support log — was told the wrong release. `make lint`
and `make contract-check` do not cover it; `tests/test_contract.py::
test_the_version_matches_pyproject` does, and it is the gate that caught this.
0.2.1 cannot be re-uploaded, so the correction is a release of its own.

No behaviour change from 0.2.1.

## [0.2.1] — 2026-08-24

### Fixed — a tighter radius stopped returning MORE results, and then any

The Postgres backend's geo prefilter narrows candidates by the geohash cell
containing the whole search box, and it ANDed `d.geohash LIKE 'prefix%'`
unconditionally. `SearchDocument.geohash` is `blank=True, default=""`, so a
source may fill `lat`/`lon` and leave it empty — which is the case on any
deployment where nothing has stamped the column on the source rows yet. Every
such document failed the LIKE and left the candidate set, in front of an exact
`lat`/`lon` box test three lines below that would have matched it.

The failure was silent (no degradation flag, no log line) and INVERTED: a
tighter box shares a longer geohash prefix, so the smaller the radius the more
documents were dropped. Measured on a live fleet whose listings all carry
coordinates and no geohash — `radius=50km` → 0 results, `radius=500km` → 3
results with the nearest at 11.67 km, a listing the 50 km search must have
returned.

A document with no geohash is *unknown*, never *elsewhere*. The prefilter is an
optimisation over the box beside it, and an optimisation that removes correct
answers is a defect, so it now narrows only documents that carry the column it
reads. The naive backend (no prefilter) and Meilisearch (`_geoRadius`, no
geohash column) were already correct.

Pinned by `test_a_document_with_no_geohash_is_still_found_by_a_tight_radius`.

**Note for hosts.** Stamping `geohash` on the source rows is still worth doing —
it is what makes the prefilter an index-backed narrowing rather than a full box
scan. `stapel-geo` publishes `geo.geohash_encode` for exactly that, and
`stapel-geo`'s MODULE.md documents listings' `geohash` column as its consumer.
Correctness no longer depends on it; performance at scale does.

## [0.2.0] — 2026-08-24

### Fixed — the count contract: never `0` beside items

A relevance query answered `count: 0` with a full page of `items` and
`degraded: ["exact_total"]`, and the storefront printed «Примерно 0
объявлений» over the visible cards. Two causes, both closed:

- **The Postgres total was counted over the wrong arm.** The typo fallback
  answers from the `word_similarity` arm; the total kept counting the
  `to_tsquery` arm that had just found nothing. `_candidate_count(q,
  trigram=...)` now counts the same question the page answered.
- **Nothing enforced the invariant above the backends.** `services`
  now floors every engine's number with what the page proves — the cursor's
  offset, the rows on this page, and one more when `has_next` says another
  exists — so a number under that floor becomes the floor and is marked a
  lower bound. `count: 0` beside a non-empty `items[]` is unreachable by
  construction, for a third-party backend too.

### Changed — wire meaning (the minor)

- **`count` is now nullable.** `null` means the engine cannot say and is
  rendered as no count at all. Unknown was previously spelled `0`, which is
  a claim rather than an absence.
- **New `count_is_lower_bound: bool`.** True when `count` is a floor: at
  least this many match, possibly more. Render `N+`, never `N`. Postgres
  sets it past `FACET_CANDIDATE_CAP`; Meilisearch sets it when the result
  window truncated the engine's answer.
- **`exact_total` describes the ANSWER, not the engine**, and equals `count
  is not null and not count_is_lower_bound`. `degraded: ["exact_total"]`
  follows it, so an engine with no guaranteed exact total no longer reports
  a small, exactly counted candidate set as degraded.
  `BackendCapabilities.exact_total` keeps its meaning ("exact at ANY corpus
  size") and stays the engine-level declaration.
- Meilisearch no longer reports `estimatedTotalHits` as an exact total: below
  the window the ranked rows are counted (the estimate also counts the rows
  the geo pass just removed), and past the window the number is a floor.
- `QueryResult.total` is `int | None` and gains `total_is_lower_bound`.
  A backend that returns the old shape still works — the service enforces
  the invariant on top of it.

### Added

- Conformance scenario `count_never_contradicts_hits`: no engine may report
  fewer matches than the page it just returned, checked on the plain and the
  fuzzy (relevance) path.
- Frontend note: a `degraded[]` that contains **only** `exact_total` is a
  count nuance, not a failed search, and must not raise a degradation
  banner (`@stapel/search-react` 0.4.0 implements this).

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
