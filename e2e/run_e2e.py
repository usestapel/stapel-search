"""End-to-end: the same assertions, twice, on two different engines.

This is the proof that the backend seam is real rather than claimed. The
run indexes one corpus, exercises the whole public surface over real HTTP
against Postgres, then changes **one settings key**, runs
``search_rebuild``, and executes the *identical* assertion function against
Meilisearch. Not a parallel suite per engine — the same code, so a
divergence cannot hide in two sets of expectations.

It also measures the freshness target the module publishes: the time from
"the source has a new document" to "the query returns it" must stay under
five seconds (`catalog-split-v2.md:71`, exported as ``lag_seconds`` in
``/health``).

Run it against the throwaway containers::

    make containers-up
    STAPEL_SEARCH_TEST_DB=postgres://postgres:stapel@127.0.0.1:55433/search_test \\
    STAPEL_SEARCH_MEILI_URL=http://127.0.0.1:57700 \\
    STAPEL_SEARCH_MEILI_KEY=stapel-test-key \\
    python -m stapel_search.e2e.run_e2e

Exit code 0 = both engines answered identically.
"""
from __future__ import annotations

import os
import sys
import time

FRESHNESS_BUDGET_SECONDS = 5.0

POSTGRES_BACKEND = "stapel_search.backends.postgres.PostgresSearchBackend"
MEILI_BACKEND = "stapel_search.backends.meili.MeilisearchBackend"


def _configure() -> None:
    from django.conf import settings

    if settings.configured:
        return
    if not os.environ.get("STAPEL_SEARCH_TEST_DB"):
        raise SystemExit(
            "e2e needs a real Postgres: set STAPEL_SEARCH_TEST_DB "
            "(make containers-up starts one)."
        )
    from stapel_search._codegen_settings import settings_kwargs

    kwargs = settings_kwargs(root_urlconf="stapel_search.codegen_urls", contract=True)
    kwargs["STAPEL_SEARCH"] = {
        "BACKEND": POSTGRES_BACKEND,
        "MEILI_URL": os.environ.get("STAPEL_SEARCH_MEILI_URL", ""),
        "MEILI_KEY": os.environ.get("STAPEL_SEARCH_MEILI_KEY", ""),
    }
    settings.configure(**kwargs)

    import django

    django.setup()


def assertions(client, *, engine: str) -> None:
    """Every assertion, run once per engine. No engine-specific branch.

    Where the engines legitimately differ, the difference is read from
    ``capabilities()`` — which is the contract's own mechanism, not an
    exception carved out for the test.
    """
    from stapel_search.backends import get_backend

    capabilities = get_backend().capabilities()

    def get(path, **params):
        response = client.get(path, params)
        assert response.status_code == 200, (engine, path, response.status_code)
        return response.json()

    # 1. the corpus is findable
    body = get("/search/api/v1/query", type="listing")
    keys = {item["key"] for item in body["items"]}
    assert keys == {"1", "2", "3"}, (engine, keys)
    assert body["backend"] == ("postgres" if engine == "postgres" else "meili")

    # 2. text finds by title and by body, and misses what is absent
    assert {i["key"] for i in get("/search/api/v1/query", type="listing", q="Samsung")["items"]} == {"2"}
    assert get("/search/api/v1/query", type="listing", q="unobtainium")["items"] == []

    # 3. a facet narrows, and the panel keeps its neighbours
    narrowed = get("/search/api/v1/query", type="listing", **{"f.brand": "apple"})
    assert {i["key"] for i in narrowed["items"]} == {"1"}

    # 4. a range includes its bound
    ranged = get("/search/api/v1/query", type="listing", **{"r.year": "2015.."})
    assert "1" in {i["key"] for i in ranged["items"]}
    assert "2" not in {i["key"] for i in ranged["items"]}

    # 5. geo cuts by distance
    near = get(
        "/search/api/v1/query", type="listing",
        lat="49.6116", lon="6.1319", radius_km="10",
    )
    assert {i["key"] for i in near["items"]} == {"1", "2"}, (engine, near["items"])

    # 6. every sort orders, and every item carries the promoted marker
    for sort, expected in (
        ("newest", ["3", "2", "1"]),
        ("price_asc", ["2", "1", "3"]),
        ("price_desc", ["3", "1", "2"]),
    ):
        page = get("/search/api/v1/query", type="listing", sort=sort)
        assert [i["key"] for i in page["items"]] == expected, (engine, sort)
        assert all("promoted" in item for item in page["items"]), (engine, sort)

    # 7. the card comes back with the hit — one query, no hydration hop
    card = next(i for i in body["items"] if i["key"] == "1")["card"]
    assert card.get("title") == "Apple iPhone 13 Pro", (engine, card)

    # 8. paging is stable and covers everything exactly once
    seen: list[str] = []
    params = {"type": "listing", "sort": "newest", "limit": "1"}
    for _ in range(5):
        page = get("/search/api/v1/query", **params)
        seen.extend(i["key"] for i in page["items"])
        if not page["has_next"]:
            break
        params = {**params, "anchor": page["next_anchor"]}
    assert seen == ["3", "2", "1"], (engine, seen)

    # 9. the disclosure is served and names promotion honestly
    ranking = get("/search/api/v1/ranking", type="listing")
    promotion = next(s for s in ranking["scorers"] if s["slug"] == "promotion_boost")
    assert promotion["applies_to_sorts"] == ["relevance"], engine

    # 10. the engine's shortfalls are declared, not hidden
    with_text = get("/search/api/v1/query", type="listing", q="samsung")
    if not capabilities.typo_tolerance:
        assert "typo_tolerance" in with_text["degraded"], engine
    # `exact_total` is a property of the ANSWER, not of the engine.
    assert ("exact_total" in with_text["degraded"]) is not with_text["exact_total"], engine
    # and the count never contradicts the page it is printed over
    assert with_text["count"] is None or with_text["count"] >= len(with_text["items"]), engine

    # 11. deep paging is refused, not truncated
    from stapel_search.dto import Cursor
    from stapel_search.query import encode_cursor

    deep = encode_cursor(Cursor(sort_value=None, doc_key="1", offset=100000))
    assert client.get("/search/api/v1/query", {"type": "listing", "anchor": deep}).status_code == 400

    print(f"  [{engine}] 11 assertion groups passed")


def seed(store: dict) -> None:
    """Register a source backed by *store* and index it through the real path."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from stapel_geo import geohash as gh

    from stapel_search.dto import SearchDocumentInput
    from stapel_search.registry import SourceSpec, register_source

    def mapper(payload: dict) -> SearchDocumentInput:
        return SearchDocumentInput(**{k: v for k, v in payload.items() if k != "key"})

    register_source(
        SourceSpec(
            doc_type="listing",
            mapper=mapper,
            content_function="listings.search_documents",
            export_function="listings.search_export",
            signals=("listing.published", "listing.updated", "listing.removed"),
            key_fields=("listing_id", "key"),
        )
    )

    def document(key, title, body, brand, year, price, lat, lon, when):
        return {
            "doc_type": "listing",
            "doc_key": key,
            "status": "published",
            "language": "ru",
            "owner_key": "u1",
            "category_path": ("electronics", "phones"),
            "title": title,
            "body": body,
            "features": {
                "brand": {"type": "string", "value": brand},
                "year": {"type": "int", "value": year},
            },
            "price_base": Decimal(price),
            "published_at": when,
            "lat": Decimal(lat),
            "lon": Decimal(lon),
            "geohash": gh.encode(float(lat), float(lon), precision=12),
            "card": {"title": title, "price": price},
            "seq": int(when.timestamp() * 1000),
        }

    store.update({
        "1": document("1", "Apple iPhone 13 Pro", "отличный телефон", "apple", 2015,
                      "500.00", "49.6116", "6.1319",
                      datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "2": document("2", "Samsung Galaxy", "самсунг телефон", "samsung", 2014,
                      "300.00", "49.6516", "6.1319",
                      datetime(2026, 2, 1, tzinfo=timezone.utc)),
        "3": document("3", "Ноутбук Lenovo", "мощный ноутбук", "lenovo", 2020,
                      "900.00", "50.9375", "6.9603",
                      datetime(2026, 3, 1, tzinfo=timezone.utc)),
    })


def main() -> int:
    _configure()

    from django.core.management import call_command
    from django.test import Client
    from django.test.utils import override_settings

    from stapel_search.backends import reset_backend_cache

    store: dict = {}

    # The source Functions, in-process. In a split deployment the identical
    # `call()` crosses the bus; the module does not know the difference.
    from stapel_core.comm import register_function

    register_function(
        "listings.search_documents",
        lambda payload: {k: store[k] for k in payload["keys"] if k in store},
    )
    register_function(
        "listings.search_export",
        lambda payload: {
            "rows": [{"key": k, **v} for k, v in sorted(store.items())],
            "cursor": None,
            "total": len(store),
        },
    )
    seed(store)

    client = Client()
    failures = 0

    print("== Postgres ==")
    reset_backend_cache()
    call_command("migrate", "--noinput", verbosity=0)
    call_command("search_rebuild", "--type", "listing", "--apply-settings", verbosity=0)
    try:
        assertions(client, engine="postgres")
    except AssertionError as exc:
        print(f"  FAILED: {exc}")
        failures += 1

    # Freshness: source commit -> visible in the answer, on the live engine.
    from datetime import datetime, timezone
    from decimal import Decimal

    started = time.monotonic()
    store["4"] = dict(
        store["1"], doc_key="4", title="Freshly published", price_base=Decimal("1.00"),
        published_at=datetime.now(timezone.utc), seq=int(time.time() * 1000),
        card={"title": "Freshly published"},
    )
    from stapel_search.services import ingest

    ingest("listing", ["4"])
    visible = {
        item["key"]
        for item in client.get("/search/api/v1/query", {"type": "listing"}).json()["items"]
    }
    elapsed = time.monotonic() - started
    if "4" not in visible or elapsed > FRESHNESS_BUDGET_SECONDS:
        print(f"  FAILED: freshness {elapsed:.2f}s, visible={'4' in visible}")
        failures += 1
    else:
        print(f"  [postgres] freshness {elapsed:.2f}s (budget {FRESHNESS_BUDGET_SECONDS}s)")
    del store["4"]
    from stapel_search.services import remove_documents

    remove_documents("listing", ["4"])

    meili_url = os.environ.get("STAPEL_SEARCH_MEILI_URL")
    if not meili_url:
        print("== Meilisearch == SKIPPED (set STAPEL_SEARCH_MEILI_URL)")
        print("NOTE: the seam is only PROVEN when both halves run.")
        return 1 if failures else 0

    print("== Meilisearch == (one settings key, a rebuild, and not one line of "
          "module code)")
    with override_settings(
        STAPEL_SEARCH={
            "BACKEND": MEILI_BACKEND,
            "MEILI_URL": meili_url,
            "MEILI_KEY": os.environ.get("STAPEL_SEARCH_MEILI_KEY", ""),
        }
    ):
        reset_backend_cache()
        call_command("search_rebuild", "--type", "listing", "--apply-settings", verbosity=0)
        try:
            assertions(client, engine="meili")
        except AssertionError as exc:
            print(f"  FAILED: {exc}")
            failures += 1
    reset_backend_cache()

    if failures:
        print(f"\ne2e: {failures} failure(s)")
        return 1
    print("\ne2e: both engines answered the same assertions — the seam holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
