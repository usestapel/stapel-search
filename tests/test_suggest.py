"""The type-ahead: category destinations with the SERP's own counts.

The case the whole feature exists for is one word with three right answers.
«Шорты» is a leaf under men's clothing, under women's clothing and under
children's clothing; a dropdown that offers the string three times is
useless, and one that offers the wrong one first is worse than useless
because the buyer follows it.
"""
from __future__ import annotations

import pytest
from stapel_core.comm import register_function
from stapel_core.comm.registry import function_registry

from stapel_search.testing import DOC_TYPE, _document

SUGGEST = "/api/v1/suggest"
QUERY = "/api/v1/query"

#: The fixture tree, as `categories.suggest` would answer it: display names
#: and ids side by side, root first. Ids are strings on both sides for the
#: same reason the real provider keeps them so — a JSON round trip must not
#: change a key's type.
MENS = {
    "id": 101,
    "slug": "muzhskaya-odezhda-shorty",
    "name": "Шорты",
    "path": ["Одежда", "Мужская одежда", "Шорты"],
    "path_ids": ["46", "48", "101"],
    "depth": 3,
    "match": "prefix",
}
WOMENS = {
    "id": 102,
    "slug": "zhenskaya-odezhda-shorty",
    "name": "Шорты",
    "path": ["Одежда", "Женская одежда", "Шорты"],
    "path_ids": ["46", "47", "102"],
    "depth": 3,
    "match": "prefix",
}
KIDS = {
    "id": 103,
    "slug": "detskaya-odezhda-shorty",
    "name": "Шорты",
    "path": ["Детям", "Детская одежда", "Шорты"],
    "path_ids": ["62", "63", "103"],
    "depth": 3,
    "match": "prefix",
}
CLOTHES = {
    "id": 46,
    "slug": "odezhda",
    "name": "Одежда",
    "path": ["Одежда"],
    "path_ids": ["46"],
    "depth": 1,
    "match": "prefix",
}


@pytest.fixture
def provider():
    """Register a stand-in for ``categories.suggest`` and record its payloads.

    The real matcher lives in stapel-categories and is tested there against a
    real tree (``tests/test_comm.py::TestSuggestFunction``). What is under
    test HERE is everything this module owns: which terms it sends, how it
    counts, and how it ranks.
    """
    calls: list[dict] = []
    answers: list[list[dict]] = [[]]

    def _provider(payload):
        calls.append(payload)
        return {"categories": answers[0]}

    register_function("categories.suggest", _provider)

    class Handle:
        payloads = calls

        @staticmethod
        def answers_with(*categories):
            answers[0] = [dict(c) for c in categories]

    try:
        yield Handle
    finally:
        function_registry._providers.pop("categories.suggest", None)


@pytest.fixture
def shorts(conformance):
    """Live listings spread over the three «Шорты» categories, plus a hidden one."""
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [
            _document(doc_key="m1", title="Шорты мужские", category_path=("46", "48", "101")),
            _document(doc_key="m2", title="Шорты карго", category_path=("46", "48", "101")),
            _document(doc_key="m3", title="Шорты джинсовые", category_path=("46", "48", "101")),
            _document(doc_key="w1", title="Шорты женские", category_path=("46", "47", "102")),
            _document(doc_key="k1", title="Шорты детские", category_path=("62", "63", "103")),
            _document(doc_key="k2", title="Шорты для мальчика", category_path=("62", "63", "103")),
            # Withdrawn: in the table, out of the indexed set. A count that
            # included it would promise a page that shows one listing fewer.
            _document(
                doc_key="m4",
                title="Шорты снятые с публикации",
                status="draft",
                category_path=("46", "48", "101"),
            ),
        ],
    )
    return conformance


# --------------------------------------------------------------------------
# the three-parent case
# --------------------------------------------------------------------------


def test_one_word_offers_every_category_path(api_client, shorts, provider):
    provider.answers_with(WOMENS, KIDS, MENS)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()

    assert [row["path"] for row in body["categories"]] == [
        ["Одежда", "Мужская одежда", "Шорты"],
        ["Детям", "Детская одежда", "Шорты"],
        ["Одежда", "Женская одежда", "Шорты"],
    ]
    assert [row["count"] for row in body["categories"]] == [3, 2, 1]


def test_ranking_is_by_live_count_not_by_the_provider_order(api_client, shorts, provider):
    """The provider answers tree order; the dropdown answers stock order."""
    provider.answers_with(WOMENS, KIDS, MENS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    counts = [row["count"] for row in body["categories"]]
    assert counts == sorted(counts, reverse=True)
    assert body["categories"][0]["slug"] == MENS["slug"]


def test_a_tie_puts_the_broader_place_first(api_client, conformance, provider):
    """Count first, then depth: among equals the shallower page is the safer landing."""
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [_document(doc_key="c1", title="Куртка", category_path=("46", "48", "101"))],
    )
    provider.answers_with(MENS, CLOTHES)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "одежда", "lang": "ru"}).json()
    assert [row["depth"] for row in body["categories"]] == [1, 3]
    assert [row["count"] for row in body["categories"]] == [1, 1]


def test_the_row_carries_a_ready_made_category_filter(api_client, shorts, provider):
    """`category` is pasted into /query verbatim — a frontend must not re-join."""
    provider.answers_with(MENS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    assert body["categories"][0]["category"] == "46/48/101"


# --------------------------------------------------------------------------
# the count is the SERP's count
# --------------------------------------------------------------------------


def test_suggest_count_equals_the_serp_count(api_client, shorts, provider):
    """The gate. A dropdown number that is not the page's number is a lie.

    Asserted against the query endpoint rather than against a hand-written
    expectation, because the failure this guards is precisely the two
    diverging: code that merely *resembles* the SERP's predicate passes
    review, and only this comparison proves it.
    """
    provider.answers_with(MENS, WOMENS, KIDS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()

    for row in body["categories"]:
        serp = api_client.get(
            QUERY, {"type": DOC_TYPE, "category": row["category"], "facets": "off"}
        ).json()
        assert row["count"] == serp["count"], row["path"]


def test_a_parent_counts_its_descendants(api_client, shorts, provider):
    """The same prefix rule the SERP's `category=` filter has."""
    provider.answers_with(CLOTHES)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "одежда", "lang": "ru"}).json()
    assert body["categories"][0]["count"] == 4  # 3 men's + 1 women's


def test_a_withdrawn_listing_is_not_counted(api_client, shorts, provider):
    provider.answers_with(MENS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    assert body["categories"][0]["count"] == 3, "the draft row must stay out"


def test_indexing_refreshes_the_counts(api_client, shorts, provider):
    """A cached aggregate must not outlive the corpus it summarized."""
    from stapel_search.services import index_documents

    provider.answers_with(MENS)
    first = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    index_documents(
        DOC_TYPE,
        [_document(doc_key="m5", title="Шорты новые", category_path=("46", "48", "101"))],
    )
    second = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    assert second["categories"][0]["count"] == first["categories"][0]["count"] + 1


# --------------------------------------------------------------------------
# cross-script: the 0.6.0 query layer, reused rather than reimplemented
# --------------------------------------------------------------------------


def test_transliteration_reaches_the_category_matcher(api_client, shorts, provider):
    """«shorty» must find «Шорты» — the same expansion the SERP applies."""
    provider.answers_with(MENS)
    api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "shorty", "lang": "ru"})
    assert "шорты" in provider.payloads[-1]["terms"]


def test_a_curated_synonym_group_reaches_it_too(api_client, shorts, provider):
    """«айфон» -> «iphone» comes from the ru dictionary, not from a rule here."""
    provider.answers_with(MENS)
    api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "айфон", "lang": "ru"})
    assert "iphone" in provider.payloads[-1]["terms"]


def test_accept_language_selects_the_dictionary(api_client, shorts, provider):
    """No `lang` parameter: the header must still load the ru dictionary."""
    provider.answers_with(MENS)
    body = api_client.get(
        SUGGEST, {"type": DOC_TYPE, "q": "айфон"}, HTTP_ACCEPT_LANGUAGE="ru-RU,ru;q=0.9"
    ).json()
    assert body["language"] == "ru"
    assert "iphone" in provider.payloads[-1]["terms"]


# --------------------------------------------------------------------------
# the edges
# --------------------------------------------------------------------------


def test_an_empty_query_asks_nobody_anything(api_client, shorts, provider):
    provider.answers_with(MENS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": ""}).json()
    assert body["categories"] == []
    assert body["terms"] == []
    assert provider.payloads == [], "an empty box must not cost a round trip"


def test_no_match_answers_empty_and_not_an_error(api_client, shorts, provider):
    provider.answers_with()
    response = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "квадрокоптер", "lang": "ru"})
    assert response.status_code == 200
    assert response.json()["categories"] == []


def test_type_is_optional_when_there_is_one_corpus(api_client, shorts, provider):
    provider.answers_with(MENS)
    body = api_client.get(SUGGEST, {"q": "шорты", "lang": "ru"}).json()
    assert body["categories"][0]["slug"] == MENS["slug"]


def test_an_unknown_type_is_still_refused(api_client, shorts):
    response = api_client.get(SUGGEST, {"type": "nope", "q": "шорты"})
    assert response.status_code == 400
    assert "search_unknown_doc_type" in response.content.decode()


def test_limit_caps_the_rows(api_client, shorts, provider):
    provider.answers_with(MENS, WOMENS, KIDS)
    body = api_client.get(
        SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru", "limit": 2}
    ).json()
    assert len(body["categories"]) == 2


def test_a_missing_provider_degrades_loudly_and_still_answers(api_client, shorts):
    """No `categories.suggest` in the fleet: terms survive, and the answer says so."""
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()
    assert body["categories"] == []
    assert body["degraded"] == ["category_suggestions"]
    assert body["backend"]


def test_terms_keeps_its_deprecated_alias(api_client, shorts, provider):
    provider.answers_with(MENS)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "Шорты", "lang": "ru"}).json()
    assert body["items"] == body["terms"]


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def test_counting_is_one_aggregate_and_does_not_grow_with_the_answer(
    django_assert_num_queries, api_client, shorts, provider
):
    """The N+1 gate: one category or three, the counting costs one query."""
    from stapel_search.suggest import category_counts, invalidate_counts

    provider.answers_with(MENS)
    invalidate_counts(DOC_TYPE)
    with django_assert_num_queries(1):
        one = category_counts(DOC_TYPE)

    provider.answers_with(MENS, WOMENS, KIDS, CLOTHES)
    invalidate_counts(DOC_TYPE)
    with django_assert_num_queries(1):
        many = category_counts(DOC_TYPE)

    assert one == many, "the aggregate does not depend on who is asking"
    assert many[("46", "48", "101")] == 3


def test_a_warm_count_cache_costs_no_query(django_assert_num_queries, shorts):
    from stapel_search.suggest import category_counts

    category_counts(DOC_TYPE)
    with django_assert_num_queries(0):
        category_counts(DOC_TYPE)


# --------------------------------------------------------------------------
# cache-friendliness
# --------------------------------------------------------------------------


def test_the_answer_is_publicly_cacheable_and_revalidates(api_client, shorts, provider):
    provider.answers_with(MENS)
    first = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"})
    assert first.status_code == 200
    assert "public" in first["Cache-Control"]
    etag = first["ETag"]
    assert etag

    again = api_client.get(
        SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}, HTTP_IF_NONE_MATCH=etag
    )
    assert again.status_code == 304
    assert again["ETag"] == etag


def test_a_changed_answer_changes_the_etag(api_client, shorts, provider):
    from stapel_search.services import index_documents

    provider.answers_with(MENS)
    before = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"})["ETag"]
    index_documents(
        DOC_TYPE,
        [_document(doc_key="m6", title="Шорты ещё", category_path=("46", "48", "101"))],
    )
    after = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"})["ETag"]
    assert before != after
