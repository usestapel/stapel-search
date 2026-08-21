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
| `postgres.PostgresSearchBackend` **(default)** | `pg_trgm`, second arm | exact to the cap, then sampled | estimated | geohash prefilter + haversine | query expansion |
| `meili.MeilisearchBackend` (`[meili]`) | native | exact | exact | native `_geoRadius` | native |
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

**Switching engines:**

```
pip install 'stapel-search[meili]'
STAPEL_SEARCH = {"BACKEND": "stapel_search.backends.meili.MeilisearchBackend"}
manage.py search_rebuild --type listing --apply-settings
```

No module code changes — and that is an assertion, not a claim:
`e2e/run_e2e.py` runs one identical assertion function against both engines.

**Writing your own.** Implement nine verbs
(`stapel_search.backends.base.SearchBackend`) and run the public conformance
suite. The release rule: **a new backend without a green conformance run does
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
property of the engine, not of the field type. An eleventh, host-registered
type indexes by a generic default **and raises `search.W002`**.

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
