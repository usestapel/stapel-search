## What this is

`stapel-search` is the fleet's search module: a **materialized index table it
owns**, one **dotted-path backend seam**, and a hard rule that every indexed
field has a source, a read path and a test.

It knows nothing about listings. Documents arrive through an open source
registry that starts empty, and the composite that is allowed to know both
the corpus and the index declares the entry. That is why the same module
indexes a catalogue, a chat archive or a profile directory without a fork.

## Why the index is its own table

`Projection`'s local mode has, by definition, no table and only a keyed
batch read. Search answers "find the matching ones", not "give me fields for
these keys" — there is no tsvector, no GIN and no facet aggregate to build
on a keyed lookup. So the index is a real table in both topologies, and all
four Projection guarantees are borrowed by name instead: idempotency on
redelivery, ordering by sequence, `rebuild` from the owner's snapshot, and
`drift_check`.

An event is a **signal, not a document**. The `listing.*` payloads are
`additionalProperties: false` and carry identity only, so the indexer pulls
the document through a comm Function — in-process in a monolith, over the
bus in a split, the same `call()` either way.

## Three engines, one conformance suite

| engine | typo tolerance | facet counts | exact total | geo | synonyms |
|---|---|---|---|---|---|
| `postgres` (default) | `pg_trgm`, second arm | exact to the cap, then sampled | exact to the cap, then a floor | geohash prefilter + haversine | query expansion |
| `meili` (`[meili]` extra) | native | exact | exact, a floor past the window | native `_geoRadius` | native |
| `naive` (tests, SQLite demos) | none, declared | exact | exact | python haversine | query expansion |
| `opensearch` | a pointer, not a promise | | | | |

Differences are never hidden. Each backend declares
`BackendCapabilities`, and every response carries `degraded: [...]` naming
what this engine could not do for this query.

**The count is one of those differences, and it says which one it is.**
`count` is nullable, `count_is_lower_bound` marks a floor ("at least N",
rendered `N+`), and `exact_total` describes THIS answer rather than the
engine class — a Postgres candidate set below the cap is counted exactly,
and saying otherwise teaches a frontend to distrust a number that is right.
The invariant the service enforces for every backend: **the answer may never
claim fewer matches than the page shows**, so `count: 0` beside a non-empty
`items[]` is unreachable. Unknown is spelled `null`, never `0`. `stapel_search.testing`
exposes the suite publicly: **a new backend without a green conformance run
does not merge**, and a scenario may only be skipped when the matching
capability is `False`.

## The gate: declared ⇒ wired ⇒ covered

The legacy this replaces died in one specific way — `features_search`,
`description_en` and `geohash` were written, half-indexed, and read by no
query, for years. Three mechanical layers stop that here:

1. `index_schema.py::INDEX_FIELDS` is the contract **as data**, emitted to
   `docs/index.json`. The dataclass refuses a field with no read path and no
   test, so nothing can be declared "for later".
2. `tests/test_index_contract.py` runs a round trip per field, against every
   configured engine, with a mandatory negative half — without one, "finds
   everything" passes.
3. `stapel-index-lint` (in stapel-tools, composed into `stapel-verify`)
   enforces the same rules statically across the fleet.

The boundary is stated as plainly as SUR004 states its own: these gates
prove the promise was not dropped on the floor, not that the branch is
right. Only the round-trip assertions do that.

## Ranking is disclosed from the code that ranks

`GET /search/api/v1/ranking` and `docs/ranking.json` are rendered from the
scorer registry, under the same drift gate as every other artifact — so the
P2B Art. 5 disclosure cannot drift from the behaviour the way a paragraph
copied into terms of service always does. `promoted` is serialized on every
result item under every sort, including when false (DSA Art. 26), and an
explicit sort receives no promotional boost: not a setting, but a structural
property of the registry, since `promotion_boost` declares `relevance` and
nothing else.

## Numbers that came from a benchmark, not a feeling

`FACET_CANDIDATE_CAP = 15000`. Counting remaining facet options is a scan of
the candidate set, and the measured curve (`tasks/search-facet-benchmark.md`)
says a single connection stays comfortable to ~130k candidates while **eight
concurrent clients breach the 200ms target at ~31k**. The cap is set with
room for concurrency, the `TABLESAMPLE` fallback is live from day one rather
than "when needed", and `MAX_FACET_FIELDS = 12` keeps a wide category page
from becoming a dozen sequential scans.
