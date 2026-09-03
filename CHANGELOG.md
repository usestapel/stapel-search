# Changelog

All notable changes to stapel-search are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.13.0] — 2026-09-03

Minor, and it changes numbers a client can see: for an anonymous reader every
geo answer is now measured against the same ~1.1km grid a public card
publishes, and `distance_km` comes back floored to that grid's quantum.

### The public geo grid — a stranger's answer may not beat the card

`stapel-listings` 0.21.0 stopped publishing the seller's pin on the public
card: the coordinates round to `PUBLIC_COORD_PRECISION` (~1.1km) and the
public `geohash` comes back empty. This module defeated that from the other
side. `/query` is `AllowAny`, the caller picks the centre, and each hit
carried `distance_km` computed from the TRUE stored coordinates — rounded to
two decimals (ten metres) on Postgres and Meilisearch, and not rounded at all
on the naive backend. Two cheap attacks followed, and both are now performed
against the fixed code in `tests/test_geo_privacy.py` rather than described:

- **Trilateration.** Three centres, three distances, one exact point.
- **Bisection.** `bbox` EXCLUDES rows, so halving the rectangle around a
  listing converges on it in about forty requests. `radius_km` is the same
  oracle in polar form, and under `geo_mode=rank` so is the `nearby` label.

**Rounding the answer would have closed none of them.** The caller's centre
and rectangle are continuous: whatever number comes back, moving the centre
until it flips traces a circle of known radius around the true point, and
three of those are the point. So the fix is at the position, not at the
number — **for an anonymous reader a row's position is read through the
public grid**, and every geo answer becomes a function of the point the card
already publishes and of nothing finer. Two pins 1.2km apart inside one cell
are now one answer, to any number of requests.

| What | Anonymous | Owner of the `owner=` scope, staff, service |
|---|---|---|
| `distance_km` | measured from the grid point, floored to the cell **diagonal** (~1.574km) | exact |
| `radius_km`, `sort=distance`, the `nearby` band, geo decay | measured from the grid point | exact |
| `bbox` | grown OUTWARD to whole cells; the smallest expressible box is one cell | as drawn |
| `card` | coordinate keys rewritten onto the grid, unrecognised position keys removed | as stored |

The quantum is **derived, not chosen**: the grid declares one square cell of
side `111.32 × 10⁻ᵖ` km (1.113km at `CARD_COORD_PRECISION` 2) the same place,
so the largest distance between two points it cannot tell apart is that
cell's diagonal, `side × √2` = 1.574km. A finer quantum would separate two
points the card does not, and a difference that survives repetition is a
position. It is floored rather than rounded, so proximity is never
overstated, and it travels into the keyset cursor as well — an opaque anchor
is not a private one.

The audience is **`stapel_attributes.visibility`'s**, resolved by the same
rule as `stapel_listings.serializers.AudienceRedactionMixin` (service
transport and staff read as staff; a reader scoped to their own `owner=` key
reads as owner), because the coordinates this gates are the coordinates that
mixin coarsens. `SearchQuery.audience` defaults to `anonymous`: a backend, a
comm caller or a management command that never says who it is gets the grid.
`search.query` takes `audience` in its payload for the callers that are
entitled to more.

Per backend: **naive** measures and bands through the grid and stops emitting
a raw float; **postgres** snaps the columns in SQL with the identical
arithmetic (`floor(v·10ᵖ + 0.5)/10ᵖ`, ties toward +∞ on both sides, which
Python's banker's rounding and Postgres' half-away-from-zero would not have
agreed on) and floors the projected `distance_km`, so `ORDER BY`, the keyset
and the emitted value are one number; **meili** cannot be asked to round a
stored point, so its engine-side geo filter becomes a widened prefilter with
the exact cut in Python, and the `_geoPoint` window sort takes a snapped
centre — either end of the measurement on the grid destroys the continuous
probe. Every coarse prefilter is widened by half a cell diagonal: an
optimisation that removes correct answers is a defect.

Nothing about the two-band work changes shape. `distance_km` still drives
«Объявления поблизости» and the per-card distance — a card saying «12 км»
never needed metres.

### The card promise, enforced on every path

`_card_area`'s docstring promised "the card never carries full-precision
coordinates". The code under it ran only inside `geo_mode=rank`, which needs
`GEO_BANDS`, which is off — so on this fleet it was dead, and safe only by
accident (the host's card carries no coordinates). Where it did run it
*overwrote* `lat`/`lon` instead of removing what it did not recognise, so a
card carrying `geohash`, `latitude` or a nested `location` walked straight
through. Now, on every path and whatever the flags: a key naming half a pair
is rewritten onto the grid from the row's own columns, a key carrying a
position that cannot become an area is removed (the same reason the mixin
blanks the public geohash instead of truncating it — two differently-aligned
areas around one point intersect down to a sliver), and `geo_precision_km`
says how wide the area is.

### Also

- `docs/index.json`, `docs/schema.json` and the `search.query` schema
  regenerated; `SearchItemSerializer.distance_km` and `.card` now describe
  what they actually contain.
- The conformance suite asks as **staff** by default — two engines computing
  the same great circle must be compared on the arithmetic, not on the
  rounding that hides four decimals of it — and gains three `public_grid_*`
  scenarios so every engine is held to the grid as well.
- **Follow-up, deliberately not in this release:** `coarse_coordinates` now
  exists twice in the fleet, here and in `AudienceRedactionMixin`, and the two
  must agree. The durable home is `stapel-core`; that is a release under the
  whole fleet and this one is a privacy fix that ships now. Both call sites
  are named in MODULE.md → Known gaps.

## [0.12.0] — 2026-09-03

Minor, and both halves are flag-gated OFF: with `QUERY_UNDERSTANDING` and
`GEO_BANDS` unset the answer is byte-identical to 0.11.2, which the suite
asserts rather than claims.

### Query understanding — a query's words become filters

`understanding.py` turns «красные штаны» into `f.color=krasnyy` plus one word
of text, against the option space of the RESOLVED category (a global scan over
815k terms is not a query-path operation). Four rungs, one slug claimed at
most once: **exact** (a folded token equals an option caption or code — this
catalogue writes `{"label": "Красный", "value": "krasnyy"}`, so the code IS
the transliterated slug and the singular lands here), **translit**,
**alias**, then **vector** for morphology, which `text.py` deliberately does
not do.

What is extracted becomes a PRE-APPLIED, visible, REMOVABLE filter, reported
under a new `query_understanding` key. Each filter carries `param` — the
literal parameter to replay — plus `span` into the raw query, so a UI can
underline the words that became a chip and drop the chip by omitting the
parameter. New `qu=auto|off` lets a caller that is managing filters itself
stop the server re-adding the one the reader just removed. An explicit
`f.<slug>` from the caller always beats an extracted value, and an extracted
filter that was overruled reports `applied: false` — a chip that narrowed
nothing must not render as one that did.

The **alias rung is not an optimisation**, and the measurement is why it
exists. On this fleet's own corpus (LaBSE, 73,664 vocabulary labels)
«сяоми»~«Xiaomi» is 0.738 while «сяоми»~«Сом» — a fish — is 0.856: the vector
rung ranks the wrong answer first, and no floor separates them. «бош»~«Bosch»
is 0.658. Since `brand` and `model` are `ref_select` on this catalogue (all
704 of them), the vocabulary rung now asks each phrase as typed AND once more
through the curated equivalents, which is the only route from a phonetic
brand to its term.

A **bare numeral never becomes a filter on its own**. On a 20k-listing eval
corpus «айфон 17» sent the lone «17» into the option space, where it matched
at confidence 1.0 on `screen_diagonal`, `rim_diameter`, `residual_tread` and
three more — six columns that have a value called 17 and nothing to do with a
phone — and recall on that query fell from 1.00 to 0.00. A numeral means
something only beside the word it qualifies, so it stays available to the
vocabulary rung as part of a phrase and is refused only standing alone.

A hit whose similarity the provider did not STATE is refused outright rather
than given an invented number, and a hit below `UNDERSTANDING_VECTOR_FLOOR`
(0.86) is not dropped either — it survives as a SIGNAL that ranks through the
new `Hit.match_count` without excluding a row. A suggestion a human reads and
ignores may be wrong; a filter that silently narrows the answer may not.

### Geo bands — distance becomes a label, never a filter

An answer is ordered `near` first, then `far` carrying every remaining row, so
a query can never come back empty because of geo. `radius_km` and `bbox`
remain the only inputs that exclude anything.

One `items` list with a `band` per row, one cursor, plus `bands[]` for the
headings — not two lists, because two lists need two cursors and a client that
decides for itself where the boundary is will disagree with the server about a
row that changed band between requests. The band travels inside the cursor's
`sort_value` as a tagged composite; the wire shape is unchanged.

The bands are executed as two separate indexed queries and concatenated, NOT
as one query sorted by a band expression. Measured on a 1M-row corpus with
production's indexes: band-as-sort-key costs 656–699 ms for a 24-row page,
because the expression must be evaluated over every row before anything can be
sorted; the same page as per-band queries costs 0.3–2.7 ms. A page that
straddles the boundary is filled from both.

The near predicate is a geohash cell cover (`NEAR_BAND_CELL_PRECISION`, 4 —
a 25km disc covers 16–20 cells, each an indexed prefix range) narrowed by an
exact haversine, so a card labelled "nearby" never shows 30 km. The far band
is the NULL-SAFE complement: a row whose geohash sits inside the cover while
its coordinates are NULL would otherwise match neither band and vanish, and
`compute_geohash_draft` maintains the two on separate paths, so the state is
reachable. The invariant `count(near) + count(far) == count(unbanded)` is
asserted with that row deliberately constructed.

Cards gain `lat`/`lon` rounded to `CARD_COORD_PRECISION` (2, ~1.1 km) and
`geo_precision_km`, so a client draws an AREA rather than a seller's pin; the
exact `distance_km` still rides on the hit, computed server-side from the true
coordinates, so the reader loses no accuracy.

### Fixed

**The filtered ANN search was silently returning short answers — and this
one bites 0.11.x in production, not just the new code.** Every corpus shares
one `search_vector_embedding` table and one HNSW index, so an HNSW search
walks the graph for the whole space and only THEN drops the rows whose `kind`
does not match; a caller asking for N gets however many of its own kind
happened to survive. Measured on the live stand at 78k vectors, asking for
50: `vocab_label` returned **40**. Measured on a 118k-vector corpus with five
kinds: `vocab_label` returned 41, and a kind holding 20,000 rows returned
between **0 and 17** depending on the probe.

What that costs, measured on the labelled eval by changing nothing but this
setting: an arm searching listing vectors alone lost **75% of its recall**
(0.300 → 0.075) and **20 of 28 queries came back empty** (against 4). The
isolated arm is the honest measure — a fused condition confounds it, because
a starved facet rung also produces fewer spurious filters and so looks better
on some rows for the wrong reason.

It degrades invisibly — a short answer is indistinguishable from a corpus
with nothing more to offer — and it gets worse as other kinds grow.
`vector/store.search` now sets `hnsw.iterative_scan = relaxed_order`, probed
with `current_setting(name, true)` (a failed `SET` would abort the whole
transaction) and scoped with `SET LOCAL` inside an explicit `atomic()`, since
outside a transaction `SET LOCAL` is a warning and a no-op.

**An extraction that empties the page is withdrawn.** An extracted filter is
a guess about what a reader meant, and an empty page over a catalogue that is
not empty is the one outcome that proves the guess wrong: the query is re-run
without the extraction and the chips come back stamped `applied: false` with
`understanding_withdrawn` in `degraded`. First page only — a cursor names an
anchor inside a population, and changing the population underneath it would
repeat or skip rows rather than rescue anybody. When the plain text search
finds nothing either, the filters were a true description of what was
searched for and stay applied. This is the single measured win of the
2026-09-03 labelled eval: recall@10 **+0.057**, CI [+0.011, +0.125]; MRR@10 **+0.096**,
CI [+0.012, +0.203]; both P(gain) = 1.00, paired bootstrap over 28 queries.
It fired on 7 of them — every one a query whose extraction ANDed the page to
empty, e.g. «телефон на гарантии» picking up `brand=garantnik` at cosine
0.864. Larger than embedding every listing title bought, and free.

`_shared.geo_distance_km` returned `OUT_OF_RANGE` for a coordinate-less row
whenever a centre was given even with no `radius_km`, so the naive backend
dropped such rows while Postgres kept them. A bare centre bounds nothing; it
now returns `None`. Banding walked straight into this — `lat`/`lon` given only
as a band centre would have dropped exactly the rows the design promises never
to drop. The conformance suite never saw it because its only coordinate-less
document is a draft.

### Added

`text.token_spans()` — `tokenize()` with each token's offsets in the original
string, one regex and one definition of a word, so a caller pointing back at
what the reader typed cannot drift from what the normalizer saw.

One equivalents group in `dictionaries/ru.json`: `["брюки", "штаны"]`. The
same rule the brand groups follow, applied to a CATEGORY word — on the
labelled eval «красные штаны» scored 0.10 recall under every condition
because «штаны»~«Брюки» is 0.784 in the embedding space, below any floor
safe to apply, and no letter table reaches it either. The colour half always
resolved; the category half is what failed. Confirmed rather than assumed:
that one line takes q01 from recall 0.10 to **1.00** under every condition
and moves no other query. It also SUPPRESSES a wrong answer — without it
«штаны» is uncurated, so it transliterates to `shtany`, which matches nothing
in a Russian corpus. Only that pair was added: «кеды»
is a different shoe and «порты» would have been a guess, and this file's rule
is measured-not-guessed.

## [0.11.2] — 2026-09-03

Patch. Cap only: `stapel-attributes` admits 0.9.

stapel-attributes 0.9.0 changes one rule semantic — a VALUE predicate (`in` /
`not_in`) no longer matches a controller that reads EMPTY, so a
`require when X not_in […]` rule stops firing before anyone has answered `X`.
Two UX walkers had hit that wall on an imported catalogue: a field starred and
refusing "Next" while its own help line said it was needed only *if* another
field said so, with that field untouched.

This module reads a feature CONFIG to decide what is facetable and never
evaluates rules, so no verdict here shifts. The cap moves so a fleet can
install one attributes version instead of being pinned to the engine with the
defect by its least-recently-capped member. The suite is green against 0.9.0
with no edit.

## [0.11.1] — 2026-09-03

### Fixed

- A client fleet's hostnames were named in 0.11.0's changelog entry. No code
  change; the sweep that catches this runs pre-push in some repos of the
  fleet and not in this one, which is the actual gap.

## [0.11.0] — 2026-09-03

### Added

- Every category suggestion now carries **its own destination**: `count_scope`
  (`category` | `query_in_category`) and `query`, the exact `/query`
  parameters the row's `count` was computed for. Send them verbatim.

### Fixed

- **A suggestion's count promised a page the tap never opened.** The two row
  kinds count different things — a NAME row is a place and its count ignores
  the typed text; a goods-driven (`listings`) row is «where your words lead»
  and its count is already text-conditioned — and the answer did not say
  which. A storefront reading it had one rule for both. On a live stand it
  appended the query to every link, so «Одежда, обувь, аксессуары · 2» opened
  `?category=140/145&q=одежда` and showed NOTHING (no listing under
  that category spells the category's own name), and «Телефоны · 47» opened a
  page with 3. Both existing gates passed throughout: each hard-coded the
  destination its own row kind assumes, so between them they proved every
  arithmetic and nothing about the seam.
  `test_every_row_count_is_the_count_of_the_page_it_opens` is the replacement
  gate — it follows what the ROW declares, for every kind of row present and
  future.

## [0.10.5] — 2026-09-02

Patch (additive): `services.reconcile` + `manage.py search_reconcile`.

### Added — the sweep for writes that never became events

An index is only as honest as the events it is fed. A source write that
skipped its own emitter — a queryset `.update(status=...)`, a raw
`Model.save()` on an emitter too old to guard its index boundary, a lost
delivery — leaves rows the index still SHOWS and the source no longer
serves: ghost cards whose click answers «no longer published». Six of them
sat at the top of a client stand's feed and search results.

`reconcile(doc_type)` re-pulls every VISIBLE row through the same `ingest`
path a live signal uses, so the pulled document's `status` decides
(`visible_statuses`) and a key the source no longer serves is removed — no
second predicate, no special case for the ghost. Keyset-paged by `doc_key`,
so tombstoning a row mid-sweep cannot shift the cursor under the reader.
Idempotent, safe on a live stand.

It is deliberately distinct from the two jobs already here: `rebuild`
replays the source's whole snapshot (heavier, and also ADDS what is
missing), `reindex_stale` is the rolling beat catch-up over a bounded
batch. This one asks exactly one question — "is everything the index still
shows actually there?" — which is the question a ghost card fails.

`manage.py search_reconcile [--type X] [--batch-size N]` runs it over one
source or every registered one, and reports the ghost count per type: zero
is `SUCCESS`, anything else is a `WARNING`, because a sweep that keeps
finding something is a signal about the emitter, not routine hygiene.

## [0.10.4] — 2026-09-02

### Fixed — the goods verb broke the count law on Postgres; 0.10.3 shipped it red

The strict-predicate fix that stopped a brand-word typo from being offered
an unrelated category (`goods_suggestions_do_not_guess`) collided with the
older law the same verb lives under: the count on an offered row is the
count the tap will show (`suggest_categories`), and the tap's query()
widens through the trigram arm below `TYPO_FALLBACK_THRESHOLD`. On
Postgres the two scenarios could not both pass, main went red, and the
v0.10.3 wheel carries the collision. Reconciled by splitting the two
promises the verb was conflating:

- **WHICH** categories may be offered is the strict predicate's answer
  alone — a near-miss still nominates nothing;
- **THE COUNT** on each strictly-nominated path is re-answered by exactly
  query()'s own decision procedure for that (text, category) request —
  strict at or above the threshold, the trigram arm's grouped count below
  it, and a path whose faithful count lands on zero is dropped, because a
  promise of zero goods is not a destination.

Both conformance scenarios pass on naive and on a real Postgres 16.

## [0.10.3] — 2026-09-02

### Fixed — the store takes its promised growth step at 73k vectors

The exact scan store.py shipped with was measured at ~250ms over a
73k-row corpus on the target stand — outside the type-ahead's latency
budget, and exactly the size the module said would want an index. The
step, as promised, changes only the store: `ensure_index(dims)` builds an
HNSW EXPRESSION index over `embedding::vector(<dims>)` (the
pgvector-documented pattern for mixed-dims tables — the untyped column
stays, so two embedding spaces still coexist during a re-embed, each
index serving its own dimensionality), the search's ORDER BY casts
through the same expression whenever the model tag names its dims (one
query text, index or honest exact scan), and the index builder ensures
the index after every build. Measured on the same stand, same corpus,
same query: 249ms exact scan → 2.5ms index scan.

## [0.10.2] — 2026-09-02

### Fixed — the similarity floor becomes kind-aware

Calibrated on a live board: one floor cannot serve two corpora, because a
model's similarity range depends on what it is ranging over. LaBSE
separates cross-script brand-label matches («тимбирленд» → Timberland)
over a wide gap, but against 3.4k short Russian category names it scores
character-overlap accidents («кросовки» ~ «Креветки», 0.85) as high as
real matches — a global floor either drowns the dropdown in accidents or
starves the corpus where the net actually earns its keep. New
`VECTOR_KIND_FLOORS` (`{kind: floor}`, default `{}`) overrides
`VECTOR_SIMILARITY_FLOOR` per corpus; an explicit `floor` in a
`search.similar` payload still overrides both.

## [0.10.1] — 2026-09-02

### Fixed — 0.10.0's wheel did not carry the package 0.10.0 was

Same wound as 0.9.1, one layer down: pyproject's explicit
`[tool.setuptools] packages` list did not name `stapel_search.vector`, so
the PUBLISHED 0.10.0 wheel imports fine, boots fine, and raises
`ModuleNotFoundError` on the first suggest request with a query — for
every PyPI consumer; a git checkout was unaffected. The package is listed
now, and `test_every_python_subpackage_ships_in_the_wheel` derives the
required list from the tree itself, so an explicit list can no longer
silently under-ship. **Pin `!=0.9.1,!=0.10.0`.**

## [0.10.0] — 2026-09-02

### Added — the vector net under the deterministic floor

«тимбирленд» is not a prefix of «Timberland», not a substring, and no
transliteration table maps one onto the other — it is a phonetic spelling,
the class of query every deterministic layer (fold, synonyms, translit,
trigram) misses in its own honest way. An embedding space catches it,
because closeness there is learned from how people actually spell things.
New `vector/` package, flag-gated and OFF by default — off means off:
byte-identical answers, no request, no cache read.

- **Storage: pgvector, behind an honest gate.** One metadata table
  (`search_vector_embedding`, migration `0003`) exists everywhere; the
  `vector` column and every piece of SQL touching it exist only where the
  extension does (`vector/store.py`, raw SQL — no driver dependency, no
  ORM field). Untyped column + a `<model>@<dims>` tag on every row: two
  embedding spaces can coexist during a re-embed and are never compared,
  and changing the model makes the needed re-embed DETECTABLE. Exact
  cosine scans — perfect recall, single-digit ms at the intended corpus
  (10³–10⁵ short strings); the ANN growth step is one module's change.
  Without the extension: `degraded: ["vector_suggestions"]` and
  `stapel_search.W009` at boot, the usual posture.
- **Embeddings by comm name** (`VECTOR_EMBED_FUNCTION` → `llm.embed`,
  stapel-agent's provider seam): no HTTP client, key or proxy here. Query
  embeddings are cached a week under the NORMALIZED query — type-ahead
  traffic is Zipfian; every repeat of a popular misspelling is a cache
  hit, not a proxied round trip.
- **The normalizer is a seam, not a copy** (`VECTOR_QUERY_NORMALIZER`,
  `vector/seam.py`): `(raw query, language) -> canonical string`, used as
  both embedding input and cache key. Defaults to a `text.fold` wrapper;
  point it at the shared translit/alias canon when that layer exports a
  single-string form.
- **Suggest integration, below everything.** Vector rows are considered
  only when NO first-class row exists (the goods-driven fallback's own
  trigger), appended under whatever determinism produced, graded
  `match: "vector"` — a grade the ranking already sorts last-class, so
  the two layers cannot disagree about precedence. A destination already
  offered is never offered twice; the similarity floor
  (`VECTOR_SIMILARITY_FLOOR`) turns nearest-garbage into nothing.
- **`search.similar`** — the comm door for sibling modules
  (stapel-vocabularies' term typeahead): `{kind, q, language?, limit?,
  floor?}` → `{results: [{key, text, payload, similarity}], degraded}`.
  Flag off answers `degraded: ["vector_disabled"]` without paying for an
  embedding, so a caller can stay wired and follow this module's switch.
- **`VECTOR_CORPORA`** — MERGE registry `{kind: provider dotted path}`,
  empty by design (the `SOURCES` rule): the composite that knows
  categories and vocabularies declares the entries.
  `manage.py search_vector_index` builds the store; `--estimate` prices
  the corpus BEFORE any request is made.

### Fixed — 0.9.1 shipped half of this feature's wiring

The 0.9.1 release commit carried the `services.suggest` call into
`stapel_search.vector` without the package (a concurrent-release seam
defect in our own tree): on 0.9.1 every suggest request WITH a query
raises `ModuleNotFoundError`. 0.9.1 is superseded the hour it appeared;
pin `!=0.9.1`.

## [0.9.1] — 2026-09-02

### Fixed — «айфон» suggested plumbing siphons; «samsung» suggested nothing

The type-ahead on a classified stand answered «айфон» with exactly one
suggestion: «Сифоны» — the plumbing traps. Three small correctnesses lined
up to produce it. The ru dictionary's iphone group carries «ифон», a
loose-typing fragment that exists for SERP recall; against category NAMES
that fragment substring-matches exactly one thing on a real board, and it is
not phones. The siphon category happened to be stocked. And the ranking
sorted stocked-before-empty FIRST, across grades, so a stocked mid-word
accident outranked every word-boundary hit on the board. Meanwhile «samsung»
— a query the SERP answers with real listings — suggested nothing at all,
because no category name anywhere contains a brand word. A dropdown is a
promise about the next page; both halves of it were broken in opposite
directions. Three mechanisms, each with the failing case as its test:

- **Match class now outranks stock.** The strong grades (`exact` / `prefix`
  / `word`) sort as one class above `substring`; stocked-before-empty
  decides only BETWEEN real hits, where 0.7.0's product decision was right
  all along. A stocked «Сифоны» substring row loses to an empty «Телефоны»
  word row, and a substring-only answer still shows when nothing better
  exists — on a sparse board it may genuinely be the place.
- **Mid-word fragments no longer reach the name matcher.** `query_terms` —
  the suggest-only path; SERP normalization is untouched — drops a group
  member that is buried mid-word inside a sibling of its own group, with
  combining marks stripped for the containment test («ифон» is «айфон»
  minus the breve, which is precisely why the group carries it). Prefix
  stems survive («самсунг» ⊂ «самсунга», «авто» ⊂ «автомобиль»): the
  shipped groups inflect almost every brand, and a literal drop-every-
  substring rule would have deleted the brands themselves. The dictionary
  file is unchanged; the SERP keeps its recall.
- **When no name answers, the goods do.** A new OPTIONAL backend verb —
  `suggest_categories(doc_type, query, *, language, limit) -> [(category
  path, count)]`, implemented by the Postgres backend (one `GROUP BY
  category_path_arr` over `_where`'s candidate set, trigram fallback
  mirroring `query()`) and by the naive backend (the reference walk) —
  answers which categories hold documents matching the query. When the name
  matcher yields nothing in the strong class, those pairs become rows
  graded `listings`, ranked below `word` and above `substring`, each
  carrying the count the `?q=…&category=…` tap will actually show
  (asserted against the query endpoint, like the name-matched count gate).
  An engine without the verb — Meilisearch and OpenSearch today — answers
  exactly what 0.9.0 answered plus `degraded:
  ["category_listing_suggestions"]`; `search.E002` does not require it and
  the conformance scenario skips it.
- **Goods-driven rows read as places, not numbers.** Their display names
  resolve through ONE batched read of a new comm Function,
  `categories.names` (`CATEGORY_NAMES_FUNCTION`, stapel-categories 0.13+):
  `{ids: […]}` in, `{names: {id: {name, slug}}}` out. Ids are this
  module's, names are the tree provider's — the same ownership rule as
  every other category fact here. A segment the provider does not answer
  for keeps its id (a truthful segment, never an invented name), and a
  provider that is missing or down leaves every id in place with
  `degraded: ["category_names"]`. The `category` filter string keeps the
  IDS regardless: display changed, the tap did not.

## [0.9.0] — 2026-09-02

### Security — an identifier was indexed, countable and exactly queryable

Some catalogue attributes do not describe an object, they *identify* one: a
VIN, an IMEI, a serial, a registry number. They are legitimate fields —
mandatory, validated, moderated — and knowing one lets a stranger act as the
owner of that specific unit. Until this release the indexer was FeatureDef-
blind and the read path was unvalidated, so a VIN was written into all three
index shapes and every one of them answered a question nobody should be able
to ask:

- **`?f.vin=<value>` was a working exact-match oracle.** `build_facets`
  synthesized a `"vin=<value>"` term for every slug it saw, `parse_query`
  turned any `f.<slug>` into a facet filter with no check against the plan or
  the schema, and the match is exact equality. One hit confirms which listing
  is that car.
- **`?r.<slug>=a..b` was the same oracle for numerics.** A numeric mapping
  wrote a `SearchNumber` row, and a range filter is an indexed semi-join on
  it: twenty queries bisect the exact mileage or the exact serial.
- **`?facets=vin` re-enumerated the values.** A slug ranked past
  `MAX_FACET_FIELDS` could be re-admitted by naming it explicitly, and it came
  back as a value list with counts.

`FeatureDef.visibility` (stapel-attributes **0.8**) is the one place that
decision is now recorded — `public` by default, so nothing that existed before
the axis changed — and this release makes it three refusals, because each of
the three shapes leaks on its own.

**The writer is the fix.** `services.build_facets` skips a DAO whose own
`visibility` stamp says it is not public — `stapel_attributes.visibility.
is_public`, fail-closed on a stamp this library does not recognise, because
the alternative to "index nothing" is "index a VIN because somebody wrote
`private`". Nothing is written for it: no `facets` entry, no `facet_terms`
term, no `SearchNumber` row. The stamp travels WITH the value, so the indexer
needs no schema lookup — which matters, because a comm call in the write path
fails *open* when the provider is down.

`SearchDocumentInput.hidden_features` is new, optional and empty by default:
the producer's explicit denylist. It is the only channel the `features_search`
fallback has, because that projection is `{slug: [values]}` — values only, no
stamp, no type — and it is obeyed on the DAO path too, so an explicit denylist
beats a missing stamp. An existing mapper indexes exactly what it indexed
before.

**The plan is the second refusal.** `facet_plan` puts a non-public feature in
the HARD `excluded` set, beside `facet: false`: not counted, and an explicit
`?facets=<slug>` does not re-admit it the way a budget-`skipped` slug can. The
set is reported as `FacetPlan.hidden`. Visibility is read defensively off the
FeatureDef and then off its config, the `categories.path` canon again —
stapel-categories does not serve the field yet, and `public` is what a
category that says nothing reads as.

**The reader is the belt.** `services.search()` drops `f.<slug>` / `r.<slug>`
filters on a slug the category hides, before the query reaches an engine, and
reports them in the new `facet_meta.dropped_filters` — dropped, never
silently ignored. The answer is then a superset of what was asked for, which
is the property that makes it not an oracle: it discriminates nothing. A
cross-category query (no `category`) has no plan and drops nothing; that is
named in the code rather than papered over, because visibility is a property
of a FeatureDef, which is a property of a CATEGORY — the same slug can be
public in one branch and hidden in another — so no fleet-wide
slug→visibility map exists and one could not be right if it did.

### Existing documents stay leaky until they are reindexed

This release stops the indexer from WRITING a hidden value. It does not
retroactively remove the terms and numbers already in the index, and the read
path's belt only covers a query that names a category. Every stand carrying
documents indexed before 0.9.0 must run:

```
python manage.py search_rebuild --type <doc_type>
```

Pair it with the projection side: a value stored before its definition became
non-public still carries no stamp, so `listings_reproject_features` (stapel-
listings) has to re-stamp the values before a rebuild can see them.

### Changed

- Floor raised to `stapel-attributes>=0.8,<0.9` — `stapel_attributes.
  visibility` is imported by the indexer and by the facet plan.
- `facet_meta.dropped_filters` is a new response field (`schema.json`
  regenerated). Additive: a client that ignores it reads the answer as before.

## [0.8.1] — 2026-09-02

Patch. `__version__` said `0.7.0` in the 0.8.0 artifact: the version lives in
two places and the release moved one of them. Nothing else changed — 0.8.0 is
functionally this release and is left on PyPI rather than yanked, but a module
that misreports its own version is exactly the class of fact nothing
downstream can check, so it does not stand.

## [0.8.0] — 2026-09-02

### Fixed — the two rankings that decided what a buyer never saw

Both defects were measured on a live board at full catalogue scale (3583
categories, 3036 leaves, ~100 listings) and both were RANKING, not matching:
the right answer was computed, ranked below something worse, and cut.

**1. `/suggest` ranked an empty catalogue by name.** 0.7.0 sorted by live
count, then depth, then name. On a board where almost every leaf is still
empty every count is `0`, so the tie-break was the NAME — and «Мужская
одежда › Шорты», the node the buyer typed letter for letter, came THIRD
behind two «Брюки и шорты», because Б precedes Ш. A transliterated fragment
made it worse: «iphone» normalizes to «ифон», which is a mid-word substring
of «Сифоны» and of nothing else on the board, so a plumbing trap was the
single suggestion offered.

The order is now **stocked before empty, then match quality, then count,
then depth, then name**:

- stocked first keeps 0.7.0's product decision where it was right — a
  dropdown is a prediction of what the buyer will find;
- match quality is the new key and the only evidence that survives an empty
  corpus. `categories.suggest` grades every hit `exact` / `prefix` / `word` /
  `substring` (stapel-categories **0.10**), and the grade is what puts a
  word-boundary hit above one buried inside a word.

`MATCH_QUALITY` is exported and states the order once. An unknown grade
sorts last rather than raising, so a provider that grows a fifth kind
degrades instead of breaking a dropdown.

**Pairing:** deploy with stapel-categories >= 0.10. Against an older
provider every hit still arrives as `prefix` or `substring` and the ranking
still runs — it just cannot tell an exact name from a prefix, or a
word-boundary hit from a mid-word one.

**2. The facet plan spent its last budget slot on a body number.** On an
imported cars leaf — 59 features against `MAX_FACET_FIELDS` of 12 — 0.7.0
ranked on the author's flags alone (`show_at_title`, `show_as_badge`,
`mandatory`). `vin`, a mandatory `int`, took a slot; the vocabulary chain
(generation, modification, complectation, engine size, power) fell past the
cap. The SERP then offered a car buyer the body number and nine dealer
promotions to filter by, and not the make.

`_facet_rank` now sorts on TWO keys. First the band: a feature with a
bounded option set — inline `options`, or an `optionsRef` into a vocabulary
— outranks one without, always. That is not a slug list; it is the
difference between an axis a panel can draw as a list of choices and one it
cannot, and numbers lose nothing by it, because a range axis is drawn from
the category schema and from `core_ranges`, neither of which this budget
caps. Then the author's own flags, with one insertion: a vocabulary-backed
field ranks directly under `show_at_title`, because a catalogue's identity
chain is made of vocabulary values and a hand-written five-option `select`
is not.

Measured against the same 59-feature shape, the plan is now make, model,
colour, generation, modification, complectation, fuel type, transmission,
engine size, doors, body type, drive type — and the body number, the plate
and the nine promotions are the last things counted.

## [0.7.0] — 2026-08-31

### Added — the type-ahead offers CATEGORIES, with the SERP's own counts

Typing «шорты» into a classified has three right answers and none of them is
a string. 0.6.0's `/suggest` answered `{"items": ["Шорты мужские", "Шорты
карго", …]}` — titles, no destination, no idea which of the three clothing
branches a buyer meant. A search box on a board this size is a navigation
control before it is a query control.

```
GET /search/api/v1/suggest?q=шорты&limit=10
{
  "categories": [
    {"id": 101, "slug": "muzhskaya-odezhda-shorty", "name": "Шорты",
     "path": ["Одежда", "Мужская одежда", "Шорты"],
     "category": "46/48/101", "count": 128, "depth": 3, "match": "prefix"},
    …
  ],
  "terms": [...], "language": "ru", "degraded": [], "backend": "postgres"
}
```

- **`categories[]`** — every row carries the whole ancestor path (the only
  thing that tells three identically named leaves apart), the number of live
  listings behind it, and a `category` string ready to paste into
  `/query?category=`. Serving the joined ids rather than only the segments is
  deliberate: a frontend that invents its own join misses silently.
- **Ranking is live count desc, then depth asc, then name.** The row is a
  prediction of what the buyer will find; a path with two listings above one
  with two hundred is wrong on the only axis they care about.
- **`count` is the SERP's count, and the test says so.**
  `test_suggest_count_equals_the_serp_count` asks this module for the number
  and `/query` for the same category, and fails if they differ — code that
  merely resembles the SERP's predicate would pass review. The rule is the
  SERP's: `doc_type` + `visible`, and a category path PREFIX, so a parent
  counts its descendants.
- **One aggregate, never one count per row.** `GROUP BY category_path` over
  the index plus a prefix rollup, cached per `doc_type` and dropped by the
  indexer when a batch lands. Measured on Postgres 16 at 100k rows / 2850
  distinct paths: HashAggregate over a sequential scan, 35 ms — the right
  plan, not a missing index, since 95% of the table satisfies the predicate.
  `test_counting_is_one_aggregate_and_does_not_grow_with_the_answer` pins it.
- **`terms`** is the old `items`, renamed; `items` still ships as a
  deprecated alias for one minor, because removing a field a live frontend
  reads is a deletion and a deletion gets its own release note.
- **`type` is optional** when exactly one document type is registered. A
  type-ahead should not have to name the only corpus there is.
- **`Cache-Control: public` + a weak `ETag`, and `If-None-Match` → 304.** The
  module's first conditional read: this payload carries nothing that varies
  with the clock, which is what makes a validator worth sending. `query` has
  none and should not.

### Added — `categories.suggest`, declared and provided

Names, ancestry and the retired/test/soft-deleted state of a node belong to
stapel-categories, so they are asked for by comm name — the `categories.path`
canon applied a second time, no import either way. This module sends
already-normalized terms and receives nodes; it does not send a query
language, because there is exactly one of those and it lives here.
stapel-categories 0.9.0 is the provider. Without one, `/suggest` still
answers its `terms` half, reports `degraded: ["category_suggestions"]`, and
**`search.W008`** says so at deploy time.

### Fixed — `shorty` reached `шортй`, a word in no corpus

A SERP fix as much as a dropdown one. GOST sends both `й` and `ы` to `y`, so
the reverse direction is ambiguous, and the table picked `й` unconditionally
— turning the most common shape of a Latin-typed Russian noun, the plural
`-y`, into nothing at all, at HTTP 200, with an empty result set as the only
symptom. Position resolves it: `ы` follows a consonant, `й` follows a vowel.

- `shorty` → `шорты`, `moy` → `мой`, `krasnyy` → `красный`
- a medial `y` is untouched: `mayka` → `майка`
- `transliterate(transliterate("шорты")) == "шорты"` is now a test

### Changed

- `query.resolve_language` is extracted and shared. Suggestions resolving the
  language a second way would reintroduce, as a disagreement between the
  dropdown and the page it leads to, the live defect where `айфон` found 2
  and `iphone` found 15 because no header reached the service.
- `services.suggest(params, *, accept_language="")` — the header now reaches
  it, as it already reached `search`.

### Settings

`CATEGORY_SUGGEST_FUNCTION`, `SUGGEST_CATEGORY_CANDIDATES`,
`SUGGEST_COUNT_CACHE_TTL`, `DEFAULT_SUGGEST_LIMIT`, `MAX_SUGGEST_LIMIT`,
`SUGGEST_CACHE_SECONDS` — all documented in CONFIG.MD.

## [0.6.0] — 2026-08-31

### Added — the other half of the panel gets captions too

0.4.0 shipped `facet_labels` for slugs whose options are inline in the
category config and left the vocabulary-backed slugs out, on the reasoning
that their level lives outside the schema and inventing a caption would lie.
The reasoning was right and the conclusion was wrong, and one screen showed
why: a live panel read

```
Состояние: Новое 12 · Б/у 31          <- captioned by 0.4.0
Производитель: apple 13 · xiaomi 10   <- still a storage code
Модель: redmi-note-12 3 · c55 3
Встроенная память: 128-gb 18
```

Neither half was wrong about its own type. The panel was wrong as a whole,
and the four rows a buyer of a phone actually uses are all in the bottom half.

- **`facets.vocabulary_labels(plan, counts)`** resolves the captions AFTER the
  count, through `stapel_attributes`' vocabulary resolver — the same
  abstraction `ref_select` validates and snapshots through, so a deployment
  wires a vocabulary once and every reader of it agrees. It cannot happen in
  `facet_plan`: a level of a real phone catalogue holds 15 844 terms and the
  plan does not know which of them a query will produce. What a query produces
  is at most `MAX_FACET_VALUES` codes per slug, asked for in one batched call
  per slug — asserted, so a future edit cannot turn it into a loop.
- **`FacetPlan.vocabulary_refs`** — `{slug: (vocabulary, level)}`, the address
  the post-count pass needs. Not captions.
- `translatable` is `False` for these without asking: a vocabulary term's
  label is literal text an owner curated, never a translation key. That is
  most of the difference between a vocabulary and an inline option list.
- **A code the vocabulary cannot resolve is ABSENT from the map, not echoed.**
  This is why the pass calls `VocabularyResolver.labels` rather than
  `refs.resolve_labels`: the latter labels an unresolved code as itself, which
  is correct for a stored DAO (something must be shown) and destroys the
  distinction here, where the map is an overlay. `{"apple": "apple"}` would
  make a map that resolved nothing look exactly like one that resolved every
  term to its own name — and `realme`'s catalogue label really is `realme`.
- **No resolver registered answers exactly as before.** A caption is an
  improvement on a code, never a precondition for answering; a resolver that
  raises is a warning and an uncaptioned facet, never a failed query.

Companion to stapel-attributes 0.7.0, which does the same thing for the same
defect on the write side: an inline `select`'s DAO now carries the option copy
it used to throw away, so `b-u` stops reaching a listing card. Between them a
code no longer reaches a reader on either surface.

## [0.5.0] — 2026-08-31

### Added — the cross-script reach is a conformance scenario, not a unit test

0.4.0 fixed the two mechanisms behind «айфон» finding 2 where `iphone` found
15 — the unresolved language and the brand forms transliteration cannot reach
— and proved both against `normalize_query`. That is the mechanism, but it is
not the defect. The defect was a COUNT, and a normalizer that expands
perfectly into an engine that ignores the expansion answers 2 just the same.
That is not hypothetical: it is exactly how the shipped `ru` dictionary sat
unused since 0.1.0 while every unit test over it passed.

- **`SCENARIOS` gains `synonym_cross_script`.** The existing
  `synonym_expansion` covers Latin query -> Cyrillic text, which is the half
  that was never broken. The new one covers the half a Russian buyer actually
  types against a Latin brand catalogue — «айфон» and «эпл» reach the Apple
  document, «самсунг» reaches the Samsung one — and it carries its own
  negative in the same scenario, so no engine can pass the reach without also
  passing the restraint: «эпоксидка» is Cyrillic, phonetically near two
  curated groups, in none of them, and must find nothing. Green on all three
  engines (naive, Postgres FTS, Meilisearch).
- Corpus-level assertions through `services.search` for the six brands
  measured live, plus the symmetry itself — `iphone` and «айфон» must answer
  the *same* key set, which is the sentence the report actually made.

**A third-party backend must now pass one more scenario**, which is why this
is a minor and not a patch. No runtime behaviour changed.

### A note on where the synonym data comes from

Deriving the brand groups from vocabulary term labels was considered and
measured against the live catalogue rather than assumed. It does not work,
and the measurement is worth keeping:

- A `Vendor` level is `{code: "apple", label: "Apple"}` — **both sides
  Latin**. A code<->label synonym source emits nothing a Cyrillic query can
  use. `эпл` is not derivable from any row that exists.
- A `Color` level is `{code: "chernyy", label: "черный"}` — a genuine
  cross-script pair, and already reachable: `stapel_vocabularies.slug`
  produces a code with a GOST-like table close enough to
  `stapel_search.text.transliterate` that the query term folds onto the code
  without any dictionary at all.

So the curated groups stay curated, because phonetic spelling is not a letter
mapping; and the *labels* problem is closed where labels live, by attribute
DAOs carrying their display snapshot (stapel-attributes 0.7.0), not by
shipping a derived dictionary this module would then have to keep in step
with someone else's catalogue.

## [0.4.0] — 2026-08-31

### Added — a listing's own price is a filter axis

`r.price` answered `count: 0` for every bound, at HTTP 200, on a live
classified board. Every range predicate resolved against the
`SearchNumber(slug, value)` side table, which is written only by numeric
*attribute* mappings; price is a column of the document, so the semi-join
found nothing and said nothing. Meanwhile `price_base` had declared
`filter:range` among its read paths since 0.1.0 — the contract claimed the
capability before any code implemented it, and `IDX002` passed because the
prefix was implemented *somewhere*.

- **`index_schema.CORE_RANGE_FIELDS`** — `{"price": "price_base"}`, emitted
  to `docs/index.json` as `core_range_fields`. An entry reserves the slug
  fleet-wide, so the map is short and lives in the contract rather than
  inside one backend.
- **`backends/_shared.split_ranges`** is the single place the two axes part
  company; postgres, meili and naive all call it, so they cannot drift about
  what `r.price` means. A core range is a plain column comparison, so a NULL
  price is outside every bound — an unpriced listing is not a cheap one.
- **`core_range_price` is a conformance scenario.** A backend that forgets
  the split fails the suite instead of quietly answering zero.
- `facet_meta.core_ranges` announces the axes, so a panel offers «Цена от …
  до …» without keeping its own list of which slugs are core.

### Added — the caption ships with the count

Facet buckets were `{value: count}` and nothing else, on the reasoning that
the frontend has the category schema already (spec §1.3). True of a compose
form, which fetches the category to draw itself; false of a SERP, where a
host rendering a panel from the search answer alone printed storage slugs at
buyers — «Состояние: **b-u**», «Вид объявления: **prodayu-svoe**».

- **`facet_labels`** — `{slug: {translatable, values: {value: caption}}}`,
  built from the same option dicts `facet_plan` already walks for
  `closed_options`, so it costs no extra I/O. `translatable` rides along
  because `b.apple` and `Б/у` are both strings and the reader cannot tell a
  key from a caption by looking.
- A vocabulary-backed slug is **absent** from the map rather than guessed at:
  its level lives outside the category schema.

### Changed — the facet budget goes to what the category flagged

`MAX_FACET_FIELDS` overflow was decided by authoring order, an accident of
the feed a category was imported from. Live, a phone board counted parcel
weight, length, height and width — the delivery block is authored first —
and reported Colour and RAM as `skipped`.

- `facet_plan` ranks by `show_at_title`, then `show_as_badge`, then
  `mandatory`, then the authored order as a stable tie-break. No list of
  slugs lives in this module.
- **`facet: false`** on a FeatureDef (or its config) drops the feature from
  the plan entirely — neither counted nor `skipped`. Default `true`, so a
  category that says nothing is unaffected. The `categories.path` canon
  again: name the field the owner does not serve yet, consume it the moment
  they do. It is what an author needs to say "the parcel's width is a
  shipping input, not a filter" — which no library can infer from the type,
  because the same `int` is a filter axis one category over.

### Fixed — a multi-word synonym was a 500, and the notice about it was false

- **`syntax error in tsquery`.** Every group member was spliced into one
  `to_tsquery` string after stripping `'` and `\`, so a multi-word member
  produced `to_tsquery('(бу | б/у | бывший в употреблении)')`. The shipped
  `ru` dictionary has held that phrase since 0.1.0; it reached no engine only
  because nothing resolved a language. `_tsquery_expression` renders
  single-word members as `to_tsquery` alternatives and multi-word ones as
  `phraseto_tsquery` terms, OR-ed with `||` within a group and AND-ed with
  `&&` across groups.
- **`PostgresSearchBackend` now declares `phrase_synonyms: True`.** The old
  `False` described the rendering, not the engine: `phraseto_tsquery` *is*
  the adjacency the capability names.
- **A shortfall is a property of the answer, not of the engine class.**
  `phrase_synonyms` was reported for every query with text, so every buyer
  saw a yellow «Синонимы не подставлялись» on every SERP — a sentence that
  was also untrue, since query-side expansion runs on every backend and
  `iphone` did reach `айфон`. It is now reported only when
  `NormalizedQuery.multiword_expansions` is non-empty, i.e. when this query
  actually had a phrase to lose. `exact_total` has been governed this way
  since 0.2.0; both are now the same rule.
- **`degraded[]` is deduplicated across layers, not only within each.**
  `_degradations` derived a shortfall from `capabilities()` while the
  backend reported the same one from the branch it took, and every answer
  shipped `["phrase_synonyms", "phrase_synonyms"]`. The postgres backend no
  longer re-emits what the service derives.

### Fixed — the dictionary that silently did not apply

`language` resolves as `lang` → `Accept-Language` → `DEFAULT_LANGUAGE`, and
it selects the dictionary. A Russian board on the `"en"` default therefore
analyzed every header-less request with the English dictionary: no ru
equivalents, no transliteration, no sign of it anywhere. Measured live —
`айфон` 2, `iphone` 15, and the same `айфон` with `?lang=ru` 16.

- **The answer states its `language`.** The only place a caller could ever
  have seen which dictionary answered.
- **`search.W007`** warns at deploy time when `DEFAULT_LANGUAGE` has no
  dictionary while other languages do — silent on a genuinely English
  deployment.
- **`dictionaries/ru.json` v2** adds the brand forms transliteration cannot
  reach, because a Russian buyer types the brand as it *sounds*: `эпл` is
  not `epl`. Apple, realme, honor, oppo, vivo, tecno, infinix, nokia,
  motorola, sony, asus, lenovo, zte, oneplus, google/pixel, nothing, ipad,
  airpods, plus the `ксиоми`/`редми`/`poco` spellings the existing xiaomi
  group missed. Contract data in its designated home, under the existing
  `search_dictionary_lint` gate.

347 passed, 52 skipped (postgres backend conformance included).

## [0.3.2] — 2026-08-31

### Added — the composite type is not a search axis

stapel-attributes 0.6.0 registers `group`, the composite kind: one feature
holding a list of **rows** of child DAOs. Undeclared it would have reached
`DEFAULT_FACET_MAPPING` — the generic term branch `search.W002` exists to
flag — and `test_every_builtin_attribute_type_has_a_declared_mapping` would
have gone red on the floor it already allows.

- `BUILTIN_FACET_MAPPINGS["group"] = FacetMapping("skip", …)`, next to
  `header`. A group has no single value to filter on: five discount-ladder
  steps are one answer, not five terms, and flattening them would count a row
  rather than a listing. A composite is a form shape, not a search axis; a
  child worth filtering on belongs outside the group.
- **A `skip` slug named explicitly in `facets=` is now refused too.** The plan
  built its exclusion from the category schema but then re-admitted any
  requested slug with `kinds.setdefault(slug, "term")`, so an explicit
  `facets=ladder` (or `facets=heading` — `header` had the same hole since
  0.1.0) planned a term facet over a slug the writer never indexes, and the
  panel answered empty for every query. `facet_plan` now carries the skipped
  slugs and drops them from `requested`.

The dependency floor stays `stapel-attributes>=0.5,<1.0`: declaring index
semantics for a slug the installed library may not register costs nothing, and
this module needs nothing from 0.6.

## [0.3.1] — 2026-08-30

### Added — index semantics for the two vocabulary-backed attribute types

stapel-attributes 0.5.0 brings `ref_select` and `ref_hierarchical_select`:
selects whose options live in an external vocabulary instead of inline in the
category schema. `BUILTIN_FACET_MAPPINGS` is keyed by attribute-type slug and
`test_every_builtin_attribute_type_has_a_declared_mapping` compares it against
the live registry, so the moment the floor moved to 0.5 the two new slugs were
undeclared — and an undeclared slug is not an error, it is the `search.W002`
generic branch, which is precisely the silent-default disease that gate exists
to stop. They are now declared:

- `ref_select` -> `term`, `ref_hierarchical_select` -> `path` — the same pair
  their inline twins `select` / `hierarchical_select` get, and for the same
  reason: a ref DAO stores term **codes** in `value` and carries `labels` only
  as a display snapshot, so the codes are the axis and a path still rolls up
  by prefix.
- `facet_plan` builds **no closed option set** for a vocabulary-backed field.
  Zero-filling means answering with every option the schema declares, and a
  ref field declares none — its level lives outside the schema and can hold
  thousands of terms (14 962 phone models in the catalogue this was designed
  against). The test is a config carrying `optionsRef`, not a hard-coded type
  slug, so a host type that points at a vocabulary the same way plans open too.

### Changed
- Dependency floor `stapel-attributes>=0.5,<1.0`.

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
