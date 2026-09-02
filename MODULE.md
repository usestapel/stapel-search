# stapel-search

Search index, facets, geo radius and ranking for the Stapel framework.

An **L2 module with its own materialized index table and one dotted-path
backend seam**. It knows nothing about listings: documents arrive through an
open source registry that starts empty, and the composite allowed to know
both the corpus and the index declares the entry.

- **Requires:** `stapel-core`, `stapel-attributes` (L1, imported),
  `stapel-geo` (L1, imported for pure geohash arithmetic — never added to
  `INSTALLED_APPS`).
- **Django app label:** `search`. Add `"stapel_search"` to `INSTALLED_APPS`.
- **Mount:** `path("search/", include("stapel_search.urls"))` →
  `/search/api/v1/…`
- **Settings:** `STAPEL_SEARCH` (see `CONFIG.MD`).

---

## The red thread

Three decisions carry the whole design, and each is a consequence of
something in the code rather than a preference.

**1. The index is its own table, in both topologies.** `Projection`'s local
mode has by definition no table and a mandatory `live_query`
(`comm/projections.py:289-299`); its only accessor is a keyed batch
`read(name, keys)`. Search answers "find the matching ones", not "give me
fields for these keys" — you cannot build a tsvector, a GIN index or a facet
aggregate on a keyed lookup. So `SearchDocument` is a real table. What is
borrowed is all four Projection guarantees, **by name**: idempotency on
redelivery (`source_event_id`), ordering by sequence (`source_seq`),
`rebuild` from the owner's snapshot with the `{cursor, limit} → {rows,
cursor, total}` contract verbatim, and `drift_check`.

**2. An event is a signal, not a document.** The `listing.*` payloads are
`additionalProperties: false` and carry identity only — no title, no price,
no coordinates. Fattening them would put every listing's text and PII on a
durable bus for every subscriber. So the indexer treats the event as
invalidation and **pulls** the document through the source's comm Function.
The same `call()` runs in-process in a monolith and over the bus in a split;
the module does not know the difference.

**3. Visibility comes from the document, never from the event name.**
Republishing a live listing sets `status = pending` and emits
`listing.updated` — and emits no `listing.removed` at all. An index that
read event names would keep a withdrawn listing searchable. The predicate is
`status ∈ SourceSpec.visible_statuses`, applied to the **pulled** document.
A key the source no longer serves is the source saying "deleted".

---

## Registering a source

Empty by design (`BUILTIN_SOURCES = {}`, the `stapel-reviews`
`BUILTIN_TARGET_TYPES` precedent). An unbuilt document type is not a
declared-but-unwired artifact — an empty registry is an honest "none", and
`search.W001` says so at deploy time.

The mapper is cross-domain glue, so it lives in the **composite**, the one
place allowed to know both sides (`stapel-shop/projections.py:1-6`). In your
shop or classified preset:

```python
# myshop/search.py
from stapel_search.registry import SourceSpec
from stapel_search.dto import SearchDocumentInput

def listing_to_document(payload: dict) -> SearchDocumentInput:
    return SearchDocumentInput(
        doc_type="listing",
        doc_key=payload["key"],
        status=payload["status"],          # the predicate reads THIS
        language=payload.get("language", ""),
        owner_key=payload.get("owner_id", ""),
        category_id=payload.get("category_id", ""),
        title=payload.get("title", ""),
        body=payload.get("description", ""),
        text_extra=tuple(payload.get("features_title") or ()),
        features=payload.get("features") or {},          # full DAOs, please
        features_search=payload.get("features_search") or {},
        price_base=payload.get("price_base"),
        published_at=payload.get("published_at"),
        source_updated_at=payload.get("updated_at"),
        lat=payload.get("lat"),
        lon=payload.get("lon"),
        geohash=payload.get("geohash", ""),
        card={
            "title": payload.get("title", ""),
            "price": payload.get("price"),
            "currency": payload.get("currency", ""),
            "images": payload.get("images") or [],
            "location_label": payload.get("location_label", ""),
        },
        seq=int(payload.get("seq") or 0),
    )

LISTING_SOURCE = SourceSpec(
    doc_type="listing",
    mapper=listing_to_document,
    content_function="listings.search_documents",
    export_function="listings.search_export",
    signals=("listing.published", "listing.updated", "listing.removed"),
    removal_signals=("listing.removed",),
    key_fields=("listing_id",),
    visible_statuses=frozenset({"published"}),
)
```

```python
# settings
STAPEL_SEARCH = {"SOURCES": {"listing": "myshop.search.LISTING_SOURCE"}}
```

Nothing else. No subscriber to write: `wire_sources()` runs from `ready()`
and subscribes one dispatcher to every signal the registry names (the
`stapel-docs` INGEST form, `actions.py:41-88`). A broken entry raises
`ImproperlyConfigured` — configured-but-broken must not be silent.

**The account life cycle is the exception to "pull, never push".** Two
signals are answered from the index itself rather than through the source,
and core 0.52.x requires both together (`stapel_core.lifecycle.E001`):

| Consume | What it does |
|---|---|
| `user.deleted` | Remove every document with that `owner_key` — the index is derived data, but a listing erased at the source and left searchable here is the erasure failing where a stranger would notice |
| `user.merged` | Re-index every document with `owner_key == from_user_id` under `into_user_id` — `services.reassign_owner` |

A merge changes exactly one indexed thing, and the row already holds
everything else, so `reassign_owner` does not re-pull. A pull would ask the
source about a merge it may not have processed yet and would re-stamp the
guest's id back onto the row — and no source emits a per-document signal
after a bulk reassignment, so nothing would come along to fix it. `ingest`
also treats a document missing from the source's answer as a *delete*, which
a transport hiccup during a merge must not trigger. It is still a re-index and
not an `UPDATE`: the index has two halves, and each touched row is pushed to
the engine, because the engine's copy would otherwise stay filed under an
account that can no longer sign in. There is no ordering raise here (unlike
the modules holding a real FK) — `owner_key` is an opaque `CharField`, so
nothing has to exist before the id can be written. Schema:
`schemas/consumes/user.merged.json`.

**Values on the wire.** stapel-listings 0.4.0 serves `Decimal` as strings
(a float would round a price) and datetimes as ISO 8601; the indexer coerces.
`seq` is unix milliseconds of `updated_at`, the same unit and origin as
`stapel_core.bus.Event.timestamp`, so a snapshot row and a live event compare
directly with no translation table.

---

## Backends

One dotted path swaps the engine. The form is
`stapel_geo/search/__init__.py:31-46` unchanged, but the **check level
differs on purpose**: geo raises W because "a broken search backend only
breaks the search verbs", and here the search verbs *are* the module — hence
`search.E001` / `search.E002`.

| engine | typo | facet counts | exact total | geo | synonyms |
|---|---|---|---|---|---|
| `postgres.PostgresSearchBackend` **(default)** | `pg_trgm`, second arm | exact to the cap, then sampled | exact to the cap, then a floor | geohash prefilter + haversine | query expansion |
| `meili.MeilisearchBackend` (`[meili]`) | native | exact | exact, a floor past the window | native `_geoRadius` | native |
| `naive.NaiveSearchBackend` | none, **declared** | exact | exact | python haversine | query expansion |
| `opensearch.OpenSearchBackend` | a pointer, not a promise | | | | |

`NaiveSearchBackend` exists so the Postgres backend never has to degrade.
`stapel-recordings` and `stapel-studio` both fall back to `icontains` off
Postgres; for a *search library* that is a lie in the shape of a working
answer, because the caller cannot tell the good engine from the bad one by
looking. `PostgresSearchBackend` raises on a non-Postgres connection and
`search.E003` says so at deploy time; a SQLite demo configures naive and gets
an honest `typo_tolerance: False`.

**Differences are declared, not hidden.** Each backend returns
`BackendCapabilities`, the service compares it against what the query asked
for, and the shortfall travels to the caller in `degraded: [...]`. A frontend
that cannot see the shortfall renders a confident wrong answer.

**A shortfall is a property of the ANSWER, not of the engine class** (0.4.0).
Reported per engine, `phrase_synonyms` put a yellow «Синонимы не
подставлялись» over every SERP, on every query, for every buyer — and the
sentence was false, because query-side expansion runs on every backend and
`iphone` did reach `айфон`. What an engine without phrase synonyms cannot do
is match a **multi-word** group member as a phrase, so that is the condition:
`NormalizedQuery.multiword_expansions` names what this query would lose, and
an empty tuple means nothing was lost and nothing is reported. `exact_total`
was already governed this way (0.2.0); the rule is now the same for both.

`degraded[]` is also **deduplicated across the layers that contribute**, not
just within each. `_degradations` derives a shortfall from `capabilities()`
while a backend may report the same one from the branch it took, and the
concatenation shipped `["phrase_synonyms", "phrase_synonyms"]` on every
query with text. The frontend deduped it on arrival, which is exactly why
nobody saw it until a stand was read by hand.

**Postgres does phrase synonyms** since 0.4.0, and the old `False` was about
the rendering rather than the engine. Every group member used to be spliced
into one `to_tsquery` string after stripping `'` and `\`, so a multi-word
member produced `to_tsquery('(бу | б/у | бывший в употреблении)')` — a
`syntax error in tsquery`, i.e. a 500 for any query whose dictionary held a
phrase. The shipped `ru` dictionary has held one since 0.1.0.
`_tsquery_expression` now renders single-word members as `to_tsquery`
alternatives and multi-word ones as `phraseto_tsquery` terms, OR-ed with
`||` inside the group and AND-ed with `&&` across groups. That is the
adjacency the capability names, so the capability is now true rather than
merely unreported.

**Switching engines:**

```
pip install 'stapel-search[meili]'
STAPEL_SEARCH = {"BACKEND": "stapel_search.backends.meili.MeilisearchBackend"}
manage.py search_rebuild --type listing --apply-settings
```

No module code changes — and that is an assertion, not a claim:
`e2e/run_e2e.py` runs one identical assertion function against both engines.

**Writing your own.** Implement nine verbs
(`stapel_search.backends.base.SearchBackend`) — plus one OPTIONAL tenth,
`suggest_categories`, the goods-driven half of the type-ahead: an engine
without it still passes (`search.E002` does not ask for it, the scenario
skips), and the service layer degrades to name-matched suggestions with
`category_listing_suggestions` in `degraded[]` — and run the public
conformance suite. The release rule: **a new backend without a green conformance run does
not merge.** Differences are legitimate only through `capabilities()` — a
scenario is skipped when the matching capability is `False`, and it *fails*
when the capability is `True` and the behaviour diverges.

```python
from stapel_search.testing import SCENARIOS, harness

@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_my_backend(scenario, db):
    with harness() as ctx:
        scenario.run(ctx)
```

---

## The gate: declared ⇒ wired ⇒ covered

The legacy this replaces died in one specific way: `features_search`,
`description_en` and `geohash` were written, half-indexed, and read by no
query, for years. Three mechanical layers:

1. **`index_schema.py::INDEX_FIELDS`** — the contract as data, emitted to
   `docs/index.json`. The dataclass refuses a field with no read path and no
   test, so "for later" is not expressible.
2. **`tests/test_index_contract.py`** — a round trip per field, against every
   configured engine, with a **mandatory negative half**. Without one, "finds
   everything" passes.
3. **`stapel-index-lint`** (stapel-tools ≥ 0.42, composed into
   `stapel-verify`) — IDX001…IDX005 statically, fleet-wide.

The boundary is stated as plainly as SUR004 states its own: these gates prove
**the promise was not dropped on the floor**, not that the branch is right.
Only the round-trip assertions do that.

---

## The count (0.2.0)

Three fields, one rule: **the answer may never claim fewer matches than the
page in front of the reader shows.**

| field | meaning | rendered as |
|---|---|---|
| `count: int` + `count_is_lower_bound: false` | the count | `N объявлений` |
| `count: int` + `count_is_lower_bound: true` | a floor — at least N | `N+ объявлений` |
| `count: null` | the engine cannot say | no count line |

`count: 0` beside a non-empty `items[]` is unreachable by construction:
`services._honest_count` floors every backend's number with what the page
proves — the cursor's offset, the rows on this page, and one more when
`has_next` says another exists. A backend number under that floor is
replaced by the floor and marked a lower bound. **Unknown is spelled
`null`, never `0`**: zero is a claim, and the visible cards disprove it.

`exact_total` now describes THIS answer (`count is not null and not
count_is_lower_bound`), not the engine class, and `degraded: ["exact_total"]`
follows the answer. `BackendCapabilities.exact_total` keeps its old meaning —
"counts exactly at ANY corpus size" — and a Postgres candidate set below
`FACET_CANDIDATE_CAP` is still counted exactly, because reporting an exact
number as degraded teaches a frontend to distrust a number that is right.

The defect this closed: the Postgres typo fallback answers from the
`word_similarity` arm, while the total was counted over the `to_tsquery` arm
that had just found nothing — so a relevance query with one misspelled term
answered `count: 0` with a full page of items, and the storefront printed
«Примерно 0 объявлений» above them. The count is now taken over the same arm
the hits came from. `exact_total` in `degraded[]` is a count nuance, not a
failed search, and a frontend should not raise a degradation banner for it
alone.

---

## Suggestions: a type-ahead that offers PLACES (0.7.0)

A classified's search box is a navigation control before it is a query
control. Typing «шорты» has three right answers and none of them is a
string:

```
Одежда › Мужская одежда › Шорты      128
Детям  › Детская одежда › Шорты       41
Одежда › Женская одежда › Шорты       12
```

`GET /search/api/v1/suggest?q=…` answers `{categories, terms, language,
degraded, backend}`. `terms` is the 0.1.0 title-prefix half, still second;
`categories` is the destination half, and every row carries the whole
ancestor path, a `count`, and a `category` string ready to paste into
`/query?category=`.

Five decisions carry it.

**Names are not matched here.** Names, ancestry and the retired/test/
soft-deleted state of a node belong to stapel-categories and are asked for
by comm name — `categories.suggest`, the `categories.path` canon applied a
second time. This module sends already-normalized terms and receives nodes.
What it deliberately does not send is a query language: there is exactly one
of those, `text.normalize_query`, and suggestions call the same function the
SERP calls with the same dictionaries. A dropdown that finds one thing while
typing and another after Enter is worse than one that finds nothing — which
is also why `resolve_language` is now one function both halves use.

**`count` is the SERP's count, and that is asserted, not asserted-to.**
`tests/test_suggest.py::test_suggest_count_equals_the_serp_count` asks this
module for the number and asks `/query` for the same category, and fails if
they differ. Code that merely resembles the SERP's predicate would pass
review; only the comparison proves it. The rule is the SERP's own: `doc_type`
+ `visible`, and a category path PREFIX, so a parent counts its descendants.

**Counting is one aggregate, ever.** `GROUP BY category_path` over the index,
then a prefix rollup in Python — never one count per suggestion. Measured on
Postgres 16 at 100k rows / 2850 distinct paths: `HashAggregate` over a
sequential scan, **35 ms**, which is the right plan and not a missing index
(95% of the table satisfies the predicate). It runs once per
`SUGGEST_COUNT_CACHE_TTL` per document type and the indexer drops the entry
when a batch lands, so a buyer's keystrokes share one aggregate and a freshly
seeded stand never shows zeros. Past the point where that stops being enough
the answer is a materialized per-category counter maintained by the indexer;
`suggest.category_counts` is the single named read, so that is one function
body.

The aggregate reads the index TABLE rather than going through the backend
seam, and that is the same line `card`, `promoted` and `health()`'s document
count already sit on: `SearchDocument` is this module's own materialized
table in **both** topologies, and `category_path` is written here under every
backend. A `category_counts` verb on the protocol would oblige four engines
to reimplement a group-by over a column this module maintains itself, and the
first engine to get it subtly wrong would be invisible.

**Ranking is match class, then stocked-before-empty, then grade, then count
desc, then depth asc, then name** — and the order of the first two is a
lesson a live stand taught the hard way. «айфон» expands to a ru group whose
SERP-recall fragment «ифон» is a mid-word substring of exactly one category
name on a real board — «Сифоны», the plumbing traps — and 0.9.0's
stocked-before-empty-first sort put that stocked accident above every real
hit: one suggestion, and it was siphons. So the strong grades
(`exact`/`prefix`/`word`) plus `listings` rows now sort as one class above
`substring`, stock decides only BETWEEN real hits, and the fragment itself no
longer reaches the matcher at all — `query_terms` drops a group member that
is buried mid-word inside a sibling (combining marks stripped for the test:
«ифон» is «айфон» minus the breve), while keeping prefix stems («самсунг» ⊂
«самсунга»), so the SERP keeps its recall and the dropdown loses the poison.

**When no NAME answers, the goods do.** Category names carry nouns, not
brands: nothing is named «Samsung», so «samsung» suggested nothing while the
SERP found real listings. When the name matcher yields nothing in the strong
class, the engine is asked which categories hold matching documents —
`suggest_categories`, the optional tenth verb, implemented by the naive and
Postgres backends over their own SERP predicate so the count on the row is
the count the tap will find (asserted end to end, like the name-matched
gate). Those rows are graded `listings`, sort below `word` (a name that says
the word is a promise about the whole category; a co-occurrence in documents
is weaker evidence) and above `substring`, and carry the path IDS as their
display segments: no comm Function in the fleet resolves category ids to
names yet, so the id is served as a truthful segment — the
stapel-categories convention — rather than invented here.

`type` is optional here, unlike on `query`: a deployment with one registered
document type has one answer, and making a storefront name it on every
keystroke is ceremony that can only be got wrong. With several registered
types it is required again.

The answer is public, identical for every reader and requested per keystroke,
so it carries `Cache-Control: public, max-age=SUGGEST_CACHE_SECONDS` and a
weak `ETag` over the payload, and honours `If-None-Match` with a 304. This is
the module's first conditional read; `query` has none, because a SERP answer
embeds `took_ms` and a cursor and would revalidate to a miss every time.
Nothing time-varying is in this payload, which is what makes the validator
worth sending.

Without a `categories.suggest` provider the endpoint still answers its
`terms` half, reports `degraded: ["category_suggestions"]`, and `search.W008`
says so at deploy time. Without `categories.path` the stored paths are one
segment long and no candidate's ancestry can match, so the answer reports
`degraded: ["category_rollup"]` rather than printing a column of zeros. And
when no name matched and the configured engine does not implement (or fails)
the optional goods-driven verb, the answer is 0.9.0's answer plus
`degraded: ["category_listing_suggestions"]` — the difference is declared,
never absorbed.

**The transliteration table gained a rule** in the same release, and it is a
SERP fix as much as a dropdown one. GOST sends both `й` and `ы` to `y`, so
the reverse direction is ambiguous, and through 0.6.0 it picked `й`
unconditionally: `shorty` became `шортй`, a word in no corpus, and found
nothing. Position resolves it — `ы` follows a consonant, `й` follows a vowel
— so `shorty` → `шорты`, `moy` → `мой`, `krasnyy` → `красный`, and a medial
`y` is untouched (`mayka` → `майка`).

---

## Facets

`facets jsonb` + `jsonb_path_ops` GIN is the authoritative **filter**
structure; `facet_terms text[]` + `array_ops` GIN is the **counting** one.
Both are required: counting remaining options needs the document's values
unfolded, which over a flat array is one lateral `unnest` per candidate and
over jsonb would be two lateral joins and a parse per row.

**Drill-down is the semantics**: each facet is counted over the candidate set
with **its own filter removed**. Otherwise the chosen value shows N and every
neighbour shows 0, and the panel becomes a dead end. The honest consequence
is that N active facet filters cost N+1 candidate sets — hence
`MAX_FACET_FIELDS = 12` as a closed switch.

**The cap came from a benchmark** (`tasks/search-facet-benchmark.md`).
A single connection stays comfortable to ~130k candidates; **eight concurrent
clients breach the 200ms target at ~31k**. So `FACET_CANDIDATE_CAP = 15000`,
the `TABLESAMPLE` fallback is live from day one, and above the cap the answer
carries `approximate: true` + `degraded: ["exact_facet_counts"]` — an empty
panel is worse than an approximate one. Re-validate the number on your own
hardware before freezing it.

Index semantics per attribute type live **here**, not in stapel-attributes:
that library carries no index metadata at all, and index semantics are a
property of the engine, not of the field type. A host-registered type this
module has never seen indexes by a generic default **and raises
`search.W002`**.

The two vocabulary-backed types (`ref_select`, `ref_hierarchical_select`,
stapel-attributes 0.5.0) are declared like their inline twins — a DAO stores
term **codes** in `value` and labels only as a display snapshot, so codes are
the axis. They carry no closed option set: the level lives in a vocabulary
outside the category schema and can hold thousands of terms, so the planner
counts what is present and owes the panel no zeros.

The composite `group` (stapel-attributes 0.6.0) is declared `skip`, next to
`header`. Its DAO value is a list of **rows** of child DAOs, so it has no
single value to filter on — five discount-ladder steps are one answer, not five
terms, and flattening them would count a row rather than a listing. A composite
is a form shape, not a search axis; a child worth filtering on belongs outside
the group. A `skip` slug is also refused when a caller names it explicitly in
`facets=`: the writer never indexed it, so planning it as a term facet would
answer every query with an empty panel.

### Core range fields (0.4.0)

`r.<slug>` used to mean one thing only: an **attribute** range, resolved by an
indexed semi-join on `SearchNumber(slug, value)`, which is written by the
numeric `FacetMapping`s. A slug naming a column of the document itself found
no row there and answered `count: 0` — for every bound, at HTTP 200, with
nothing in the response saying the filter had never been applied. Live, that
meant a classified board where a buyer could **sort** by price and not
**filter** by it, while `index_schema.py` had declared `filter:range` among
`price_base`'s read paths since 0.1.0.

`index_schema.CORE_RANGE_FIELDS` (`{"price": "price_base"}`) is the
declaration that makes the claim true. `backends/_shared.split_ranges` is the
one place the two axes part company, so the three engines cannot drift about
what `r.price` means, and `core_range_price` is a **conformance scenario**:
a backend that forgets the split fails the suite instead of answering zero.
A core range is a plain column comparison, so a NULL price is outside every
bound — an unpriced listing is not a cheap one.

Adding an entry reserves the slug fleet-wide (a category attribute of the
same name would be shadowed), which is why the map is short, lives in the
contract, and is emitted to `docs/index.json` as `core_range_fields`.

The plan announces the axes in `facet_meta.core_ranges` rather than requiring
every frontend to keep its own list of which slugs are core. They are not in
`slugs`: there is nothing to count, only an axis to offer.

### Option captions ship with the counts (0.4.0)

Until 0.4.0 a bucket was `{value: count}` and nothing else, on the reasoning
that the frontend has the category schema already. That holds for a compose
form, which fetches the category to draw itself. It does **not** hold for a
SERP: a host rendering a panel from the search answer alone has no schema,
and the panel then prints storage slugs — «Состояние: **b-u**», «Вид
объявления: **prodayu-svoe**» — at buyers.

`facet_labels` is `{slug: {translatable, values: {value: caption}}}`, built
from the very option dicts `facet_plan` already walks to compute
`closed_options`. It costs no extra I/O. `translatable` rides along because
the reader cannot tell a translation key from a caption by looking —
`b.apple` and `Б/у` are both strings — and guessing wrong prints either a
dotted key or an untranslated word. A vocabulary-backed slug is **absent**
from the map: its level lives outside the schema, and the plan will not
invent a caption it has not read.

### What the facet budget is spent on (0.4.0)

`MAX_FACET_FIELDS` is a real cap and a wide imported category overruns it.
Until 0.4.0 the overflow was decided by *authoring order*, which is an
accident of the feed the category came from: a live phone board counted
parcel weight, length, height and width — the delivery block is authored
first — and reported Colour and RAM as `skipped`, which are the two a phone
buyer actually narrows by.

`facet_plan` now ranks by what the category **already says** about each
feature: `show_at_title`, then `show_as_badge`, then `mandatory`, then the
authored order as a stable tie-break. No list of slugs lives in this module;
a search library holding opinions about phones is the thing being avoided.

A feature can also refuse outright: `facet: false` on the FeatureDef (or its
config) drops it from the plan entirely, neither counted nor `skipped`. The
default is **true**, so a category that says nothing keeps today's behaviour.
This is the `categories.path` canon applied again — name the field the owner
does not serve yet, consume it the moment they do — and it is what a category
author needs in order to say "the parcel's width is a shipping input, not a
filter", a thing no library can infer from the type, because the same `int`
is a filter axis one category over.

### A hidden attribute is not a search axis (0.9.0)

Some attributes do not describe an object, they *identify* one: a VIN, an
IMEI, a serial, a registry number. `FeatureDef.visibility` (stapel-attributes
0.8) records that once, in the catalogue — `public` by default, `owner` or
`staff` otherwise — and this module owes three separate refusals, because
each of the index's three shapes is an exact-value oracle on its own:

| shape | the question it answered |
|---|---|
| `facet_terms` | `?f.vin=<value>` — one hit confirms *which listing is that car* |
| `SearchNumber` | `?r.mileage=X..X` — twenty queries bisect the exact number |
| the counted panel | `?facets=vin` re-enumerated the values, with counts |

**The writer is the fix.** `build_facets` skips a DAO whose own stamp says it
is not public (`visibility.is_public`, fail-closed on a stamp this library
does not recognise) and a slug the producer named in
`SearchDocumentInput.hidden_features`. Nothing is written: no `facets` entry,
no term, no number. The stamp travels with the value, so the indexer needs no
schema lookup — which matters, because a comm call in the write path fails
*open* when the provider is down.

`features_search` — the lossy `{slug: [values]}` fallback — carries no stamp
and cannot be defended by the value in hand. `hidden_features` is the channel
a producer of that projection uses instead; it is optional and empty by
default, so an existing mapper indexes exactly what it indexed before.

**The plan is the second refusal.** A non-public feature lands in
`facet_plan`'s hard `excluded` set, beside `facet: false` — so it is not
counted, and an explicit `?facets=vin` does **not** re-admit it the way a
budget-`skipped` slug can. The plan reports the set as `hidden`.

**The reader is the belt**, and it is not redundant: documents indexed before
0.9.0 still carry their terms and numbers. `services.search()` drops
`f.<slug>` / `r.<slug>` filters on a slug the category hides, and reports
them in `facet_meta.dropped_filters` rather than ignoring them silently. The
answer is then a superset of what was asked for — it discriminates nothing,
which is exactly the property that makes it not an oracle.

A **cross-category** query (no `category`) has no plan and drops nothing.
That is accepted and named rather than papered over: visibility is a property
of a FeatureDef, which is a property of a category — the same slug can be
public in one branch and hidden in another — so no fleet-wide slug→visibility
map exists, and one could not be right if it did. What closes the case is the
writer: after

```
python manage.py search_rebuild --type <doc_type>
```

there is no term and no number for a hidden slug in any document, so the
filter matches nothing whatever category it was aimed at.

---

## Ranking, promotion and disclosure

`GET /search/api/v1/ranking` and `docs/ranking.json` are rendered from the
scorer registry that does the ranking, under the same drift gate as every
other artifact. The P2B Art. 5 disclosure therefore cannot drift from the
behaviour the way a paragraph copied into terms of service always does.

- `promoted` is on **every** result item under **every** sort, including
  when false (DSA Art. 26). The serializer cannot omit it.
- **An explicit sort never receives a promotional boost.** Not a setting:
  `promotion_boost` declares `applies_to_sorts = {"relevance"}` and nothing
  else, so under any other sort it is simply not in the loop.
- A scorer the configured engine cannot evaluate is listed with
  `active: false` — a disclosure that lies about which parameters apply is
  worse than none.
- `popular` is **absent** from the shipped sorts: no popularity signal exists
  in the fleet, and a sort that orders every document by zero is a promise
  with nothing behind it. `search.W004` enforces the same rule on a host that
  enables it without an emitter.

Promotion has one inbound door, the `search.signal` Action
(`{doc_type, doc_key, boost?, popularity?, promoted?, expires_at?}`), with an
audit row per signal and a `[-1.0, 5.0]` clamp at the seam. Who may open it
is a product decision left outside this module.

---

## Operations

```
manage.py search_rebuild --type listing [--apply-settings]
manage.py search_drift_check --type listing [--strict]
manage.py search_apply_settings --type listing
manage.py search_dictionary_lint [--lang ru] [--strict]
```

```python
from stapel_search.tasks import get_search_beat_schedule
CELERY_BEAT_SCHEDULE = {**get_search_beat_schedule(), ...}
```

Celery is optional — every task is a plain callable. But a catch-up job
nobody schedules is a promise, not a mechanism, so `search.W003` fires when a
beat schedule exists with no entry for `search_reindex_stale`.

`GET /search/api/v1/health` reports the engine, its capabilities,
`lag_seconds` and a `stale_reason`. Target freshness is **≤ 5s** from source
commit to visibility, asserted in `e2e/run_e2e.py`.

### Testing

Behaviour that cannot be faked is tested against a real server or skipped,
never simulated:

```
make containers-up      # throwaway postgres:16 + meilisearch
make test               # sqlite + naive
make test-postgres      # the whole suite, including the index-contract table
make test-meili         # the conformance suite against Meilisearch
make containers-down
```

---

## Reserved, not built

- **Saved searches / alerts.** The *names* are reserved: model `SavedQuery`,
  event `search.saved_query.matched`
  `{saved_query_id, owner_id, doc_type, doc_key, query_hash, matched_at}`,
  notification type `saved_search_match`. **No JSON schema is committed in
  `schemas/emits/`** — a schema with no emitter is exactly the
  declared-but-unwired artifact this module's own gate exists to catch. The
  schema appears when the emitter does.
- **Promotion beyond the `boost` field.** No `PromotedListing`, no tiers, no
  durations, no promo-slot marketplace. The seam is laid so adding them is a
  new emitter, not a redesign of the index.

## Deliberately not in v1

Vector/semantic search; "did you mean"; personalization and ML ranking;
federated search across several `type`s in one query; autocomplete from a
query log (no query log is kept — a privacy decision before a product one);
search analytics and CTR; searching masked or secret fields (forbidden by
`stapel-core/django/admin/base.py:213` — icontains probing is an oracle);
realtime result streams; query moderation; and any scheduler, cache layer or
HTTP client of our own.

## Known gaps

- **`categories.path` has no provider in the fleet.** The canonical name is
  declared here now (the `stapel-shop/projections.py:23-35` move). Until
  something answers it, `category_path` degrades to a single segment:
  exact-category filtering works, **rollup does not**, `search.W006` says so
  at deploy time and every answer carries `degraded: ["category_rollup"]`.
- **No `-react` pair yet.** Result cards are a product slot; there is no
  `listings-react` in the fleet to render one.
