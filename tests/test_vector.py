"""Vector similarity: the net under the deterministic floor.

«тимбирленд» is not a prefix of «Timberland», not a substring of it, and no
transliteration table maps one onto the other — it is a *phonetic spelling*,
and the deterministic layers (fold, synonyms, translit, trigram) each miss
it in their own honest way. An embedding space does not: the two strings
land close because the model has seen both in the wild. This suite pins the
three promises the layer makes:

- **Flag off is byte-identical off.** No request is made, no row is added,
  the objects pass through untouched. A feature flag that leaks even a
  cache read is not a flag.
- **The floor rejects garbage.** Cosine similarity degrades gracefully into
  noise; a dropdown must not. Below ``VECTOR_SIMILARITY_FLOOR`` a hit does
  not exist.
- **Vector rows never displace deterministic rows.** They are appended
  below, only when the deterministic answer produced nothing first-class —
  the exact seam drawn for the goods-driven fallback in 0.9.1.

Embeddings are MOCKED — a toy 4-dimensional space with hand-placed points —
because what is under test is ranking, gating and caching, none of which
needs OpenAI's opinion. The store is mocked on SQLite (the honest cosine
over the same toy space); the real pgvector SQL is exercised by the
postgres-marked suite at the bottom, same policy as the FTS/trigram arms.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings
from stapel_core.comm import register_function
from stapel_core.comm.registry import function_registry

# ─── The toy embedding space ────────────────────────────────────────────
# Unit-ish 4-d vectors, placed by hand. The phonetic misspelling sits next
# to the brand; the plumbing category sits orthogonal to both.

SPACE = {
    "тимбирленд": (0.99, 0.14, 0.0, 0.0),
    "timberland": (1.0, 0.0, 0.0, 0.0),
    "сифоны": (0.0, 0.0, 1.0, 0.0),
    "мусор": (0.0, 0.05, 0.1, 0.99),
}

TIMBERLAND_ROW = {
    "key": "412",
    "text": "Timberland",
    "payload": {
        "id": 412,
        "slug": "obuv-timberland",
        "name": "Timberland",
        "path": ["Обувь", "Timberland"],
        "path_ids": ["400", "412"],
        "depth": 2,
    },
    "vector": SPACE["timberland"],
}

SIPHONS_ROW = {
    "key": "77",
    "text": "Сифоны",
    "payload": {
        "id": 77,
        "slug": "sifony",
        "name": "Сифоны",
        "path": ["Сантехника", "Сифоны"],
        "path_ids": ["70", "77"],
        "depth": 2,
    },
    "vector": SPACE["сифоны"],
}


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


VECTOR_ON = {
    # override_settings replaces the whole namespace dict, so the test
    # backend has to ride along with the flag.
    "BACKEND": "stapel_search.backends.naive.NaiveSearchBackend",
    "VECTOR_SUGGEST": True,
    "VECTOR_SIMILARITY_FLOOR": 0.6,
}


@pytest.fixture
def vector_on():
    with override_settings(STAPEL_SEARCH=VECTOR_ON):
        yield


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake_embed():
    """Register a stand-in ``llm.embed`` over the toy space, counting calls."""
    calls: list[dict] = []

    def _embed(payload):
        calls.append(payload)
        vectors = []
        for text in payload["texts"]:
            if text not in SPACE:
                return {"status": "failure", "reason": f"unknown text {text!r}"}
            vectors.append(list(SPACE[text]))
        return {
            "status": "ok",
            "embeddings": {
                "provider": "fake",
                "model": "text-embedding-3-small",
                "dim": 4,
                "vectors": vectors,
                "usage": {"total_tokens": 3 * len(vectors)},
            },
            "provider_used": "fake",
        }

    register_function("llm.embed", _embed)
    try:
        yield calls
    finally:
        function_registry._providers.pop("llm.embed", None)


@pytest.fixture
def fake_store(monkeypatch):
    """An in-memory store honouring the real store's search contract."""
    from stapel_search import vector

    rows: list[dict] = []

    def _available():
        return True

    def _search(kind, query_vector, *, model_tag, limit):
        scored = [
            (row["key"], row["text"], row["payload"], _cosine(query_vector, row["vector"]))
            for row in rows
            if row.get("kind", "category") == kind
        ]
        scored.sort(key=lambda item: -item[3])
        return scored[:limit]

    monkeypatch.setattr(vector.store, "available", _available)
    monkeypatch.setattr(vector.store, "search", _search)

    class Handle:
        @staticmethod
        def seed(*seeded):
            rows.extend(dict(row) for row in seeded)

    return Handle


# ─── The flag is a wall ─────────────────────────────────────────────────


class TestFlagOff:
    def test_augment_passes_the_objects_through_untouched(self, db, fake_embed):
        from stapel_search.vector import augment_category_suggestions

        rows = [{"match": "substring", "category": "1", "count": 0}]
        degraded: list[str] = []
        out_rows, out_degraded = augment_category_suggestions(
            rows, degraded, doc_type="conformance", q="тимбирленд", language="ru", limit=10
        )
        assert out_rows is rows
        assert out_degraded is degraded
        assert fake_embed == []

    def test_the_comm_function_says_disabled_without_embedding(self, db, fake_embed):
        from stapel_core.comm import call

        answer = call("search.similar", {"kind": "category", "q": "тимбирленд"})
        assert answer == {"results": [], "degraded": ["vector_disabled"]}
        assert fake_embed == []


# ─── The net catches the phonetic miss ──────────────────────────────────


@pytest.mark.usefixtures("vector_on")
class TestVectorFallback:
    def test_timbirlend_reaches_timberland(self, db, fake_embed, fake_store):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW, SIPHONS_ROW)
        rows, degraded = augment_category_suggestions(
            [], [], doc_type="conformance", q="Тимбирленд", language="ru", limit=10
        )
        assert [row["name"] for row in rows] == ["Timberland"]
        row = rows[0]
        assert row["match"] == "vector"
        assert row["category"] == "400/412"
        assert row["path"] == ["Обувь", "Timberland"]
        assert row["count"] == 0
        assert degraded == []

    def test_the_floor_rejects_garbage(self, db, fake_embed, fake_store):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW, SIPHONS_ROW)
        rows, _ = augment_category_suggestions(
            [], [], doc_type="conformance", q="мусор", language="ru", limit=10
        )
        assert rows == []

    def test_a_strong_deterministic_answer_never_pays_for_a_vector(
        self, db, fake_embed, fake_store
    ):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW)
        strong = [{"match": "prefix", "category": "400/412", "count": 3}]
        rows, _ = augment_category_suggestions(
            strong, [], doc_type="conformance", q="timberland", language="ru", limit=10
        )
        assert rows is strong
        assert fake_embed == []

    def test_vector_rows_sit_below_deterministic_rows(self, db, fake_embed, fake_store):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW)
        weak = [
            {
                "match": "substring",
                "category": "70/77",
                "count": 2,
                "name": "Сифоны",
            }
        ]
        rows, _ = augment_category_suggestions(
            weak, [], doc_type="conformance", q="тимбирленд", language="ru", limit=10
        )
        assert [row.get("match") for row in rows] == ["substring", "vector"]

    def test_a_destination_already_offered_is_not_offered_twice(
        self, db, fake_embed, fake_store
    ):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW)
        weak = [{"match": "substring", "category": "400/412", "count": 2, "name": "x"}]
        rows, _ = augment_category_suggestions(
            weak, [], doc_type="conformance", q="тимбирленд", language="ru", limit=10
        )
        assert len(rows) == 1

    def test_an_embed_failure_degrades_and_keeps_the_rows(
        self, db, fake_embed, fake_store
    ):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW)
        rows, degraded = augment_category_suggestions(
            [], [], doc_type="conformance", q="неизвестное слово", language="ru", limit=10
        )
        assert rows == []
        assert degraded == ["vector_suggestions"]

    def test_a_missing_store_degrades_without_paying_for_an_embed(
        self, db, fake_embed, monkeypatch
    ):
        from stapel_search import vector
        from stapel_search.vector import augment_category_suggestions

        monkeypatch.setattr(vector.store, "available", lambda: False)
        rows, degraded = augment_category_suggestions(
            [], [], doc_type="conformance", q="тимбирленд", language="ru", limit=10
        )
        assert rows == []
        assert degraded == ["vector_suggestions"]
        assert fake_embed == []

    def test_the_query_embedding_is_cached(self, db, fake_embed, fake_store):
        from stapel_search.vector import augment_category_suggestions

        fake_store.seed(TIMBERLAND_ROW)
        for _ in range(3):
            augment_category_suggestions(
                [], [], doc_type="conformance", q="тимбирленд", language="ru", limit=10
            )
        assert len(fake_embed) == 1


# ─── The seam to the deterministic normalization layer ──────────────────


def shouting_normalizer(q: str, language: str) -> str:
    """A stand-in for the Д5 canonical normalizer."""
    return f"canon:{q.lower()}:{language}"


class TestNormalizerSeam:
    def test_the_configured_normalizer_owns_the_embedding_input(
        self, db, fake_embed, fake_store, monkeypatch
    ):
        from stapel_search.vector import service

        with override_settings(
            STAPEL_SEARCH={
                **VECTOR_ON,
                "VECTOR_QUERY_NORMALIZER": "tests.test_vector.shouting_normalizer",
            }
        ):
            service.similar("category", "ТИМБИРЛЕНД", language="ru")
        assert len(fake_embed) == 1
        assert fake_embed[0]["texts"] == ["canon:тимбирленд:ru"]

    def test_the_default_normalizer_folds(self, db, fake_embed, fake_store):
        from stapel_search.vector import service

        with override_settings(STAPEL_SEARCH=VECTOR_ON):
            service.similar("category", "  ТимбирлЕнд  ", language="ru")
        assert fake_embed[0]["texts"] == ["тимбирленд"]


# ─── The comm surface ───────────────────────────────────────────────────


@pytest.mark.usefixtures("vector_on")
class TestSimilarFunction:
    def test_answers_hits_above_the_floor(self, db, fake_embed, fake_store):
        from stapel_core.comm import call

        fake_store.seed(TIMBERLAND_ROW, SIPHONS_ROW)
        answer = call("search.similar", {"kind": "category", "q": "тимбирленд"})
        assert answer["degraded"] == []
        assert [hit["text"] for hit in answer["results"]] == ["Timberland"]
        hit = answer["results"][0]
        assert hit["key"] == "412"
        assert 0.9 < hit["similarity"] <= 1.0
        assert hit["payload"]["slug"] == "obuv-timberland"


# ─── End to end through the suggest service ─────────────────────────────


@pytest.fixture
def empty_category_provider():
    register_function("categories.suggest", lambda payload: {"categories": []})
    try:
        yield
    finally:
        function_registry._providers.pop("categories.suggest", None)


class TestSuggestEndToEnd:
    def test_flag_off_pays_nothing(
        self, conformance, fake_embed, empty_category_provider
    ):
        from stapel_search.services import suggest

        answer = suggest({"q": "тимбирленд"})
        assert answer["categories"] == []
        assert fake_embed == []

    def test_flag_on_offers_the_vector_destination(
        self, conformance, fake_embed, fake_store, empty_category_provider
    ):
        from stapel_search.services import suggest

        fake_store.seed(TIMBERLAND_ROW, SIPHONS_ROW)
        with override_settings(STAPEL_SEARCH=VECTOR_ON):
            answer = suggest({"q": "тимбирленд"})
        assert [row["name"] for row in answer["categories"]] == ["Timberland"]
        assert answer["categories"][0]["match"] == "vector"
        assert answer["degraded"] == []


# ─── The real store, on a real Postgres with pgvector ───────────────────

from .marks import POSTGRES_URL, requires_postgres  # noqa: E402


@requires_postgres
class TestPgvectorStore:
    @pytest.fixture(autouse=True)
    def _needs_pgvector(self, db):
        from django.db import connection

        if connection.vendor != "postgresql":
            pytest.skip("store SQL is postgres-only; run under STAPEL_SEARCH_TEST_DB")
        from stapel_search.vector import store

        if not store.ensure_schema():
            pytest.skip("pgvector extension not installed on the test server")

    def test_roundtrip_orders_by_similarity_and_filters_by_model_tag(self):
        from stapel_search.vector import store

        store.upsert_many(
            "category",
            [
                ("412", "Timberland", {"slug": "obuv-timberland"}, SPACE["timberland"]),
                ("77", "Сифоны", {"slug": "sifony"}, SPACE["сифоны"]),
            ],
            model_tag="toy@4",
        )
        hits = store.search(
            "category", SPACE["тимбирленд"], model_tag="toy@4", limit=5
        )
        assert [hit[0] for hit in hits] == ["412", "77"]
        assert hits[0][3] > 0.95
        assert hits[1][3] < 0.2
        assert store.search("category", SPACE["тимбирленд"], model_tag="other@4", limit=5) == []

    def test_upsert_replaces_in_place(self):
        from stapel_search.vector import store

        store.upsert_many(
            "category", [("1", "Old", {}, SPACE["сифоны"])], model_tag="toy@4"
        )
        store.upsert_many(
            "category", [("1", "New", {}, SPACE["timberland"])], model_tag="toy@4"
        )
        hits = store.search("category", SPACE["timberland"], model_tag="toy@4", limit=5)
        assert [(hit[0], hit[1]) for hit in hits] == [("1", "New")]
