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


def test_an_empty_category_is_offered_and_says_so(api_client, conformance, provider):
    """A catalogue nobody has stocked yet is still a catalogue you can walk.

    Live measurement, 2026-09-01: 3036 leaves, 100 listings, so 2924 leaves
    read zero. Hiding them left «шорты», «квартира» and «камри» with no
    suggestion panel at all — the type-ahead answering "that does not exist"
    about six categories that do. Empty rows sort BELOW stocked ones and
    carry an honest 0; they are not dropped.
    """
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [_document(doc_key="w1", title="Шорты женские", category_path=("46", "47", "102"))],
    )
    provider.answers_with(MENS, WOMENS, KIDS)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()

    assert [row["count"] for row in body["categories"]] == [1, 0, 0]
    assert {row["slug"] for row in body["categories"]} == {
        MENS["slug"],
        WOMENS["slug"],
        KIDS["slug"],
    }
    assert body["categories"][0]["slug"] == WOMENS["slug"]


def test_an_all_empty_catalogue_is_ranked_by_match_quality(
    api_client, conformance, provider
):
    """The defect this closes, exactly as it was measured on the stand.

    «шорты» answered six rows, every count 0, and the node the buyer typed —
    «Личные вещи › Одежда, обувь, аксессуары › Мужская одежда › **Шорты**» —
    came THIRD, behind two «Брюки и шорты», because the tie-break after count
    was depth and then the NAME, and Б precedes Ш. With no stock anywhere,
    the grade `categories.suggest` puts on each hit is the only evidence left.
    """
    exact = {**MENS, "match": "exact"}
    word = {
        "id": 201,
        "slug": "dlya-devochek-bryuki-i-shorty",
        "name": "Брюки и шорты",
        "path": ["Личные вещи", "Для девочек", "Брюки и шорты"],
        "path_ids": ["70", "71", "201"],
        "depth": 3,
        "match": "word",
    }
    buried = {
        "id": 202,
        "slug": "sifony",
        "name": "Сифоны",
        "path": ["Для дома", "Трубы и фитинги", "Сифоны"],
        "path_ids": ["80", "81", "202"],
        "depth": 3,
        "match": "substring",
    }
    provider.answers_with(buried, word, exact)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()

    assert [row["match"] for row in body["categories"]] == [
        "exact",
        "word",
        "substring",
    ]
    assert body["categories"][0]["slug"] == MENS["slug"]
    assert [row["count"] for row in body["categories"]] == [0, 0, 0]


def test_stock_outranks_grade_only_inside_the_strong_class(api_client, shorts, provider):
    """What survives of 0.7.0's "stock first": stock breaks ties BETWEEN real hits.

    Both rows here matched a whole word of the name or better — the strong
    class — and inside that class the stocked place is still the better
    prediction: a word-graded row with three listings leads an exact-graded
    row with none. What stock may no longer do is lift a mid-word fragment
    over a real hit; that boundary has its own test below.
    """
    provider.answers_with(
        {**MENS, "match": "word"},
        {**CLOTHES, "match": "exact", "path_ids": ["99"], "id": 99, "slug": "empty"},
    )

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "одежда", "lang": "ru"}).json()

    assert [(row["match"], row["count"]) for row in body["categories"]] == [
        ("word", 3),
        ("exact", 0),
    ]


def test_a_stocked_substring_never_outranks_a_word_hit(api_client, conformance, provider):
    """The live defect, exactly as a buyer met it on a classified stand.

    «айфон» answered ONE suggestion: «Сифоны» — plumbing siphons — because
    the transliterated fragment «ифон» is a mid-word substring of that name,
    the siphon category happened to be stocked, and the sort put
    stocked-before-empty ahead of every grade. A word-boundary hit with no
    stock yet must beat a stocked mid-word accident: match CLASS first,
    stock only within it.
    """
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [
            _document(doc_key="s1", title="Сифон для раковины", category_path=("80", "81", "202")),
            _document(doc_key="s2", title="Сифон для ванны", category_path=("80", "81", "202")),
        ],
    )
    siphons = {
        "id": 202,
        "slug": "sifony",
        "name": "Сифоны",
        "path": ["Для дома", "Трубы и фитинги", "Сифоны"],
        "path_ids": ["80", "81", "202"],
        "depth": 3,
        "match": "substring",
    }
    phones = {
        "id": 205,
        "slug": "telefony",
        "name": "Телефоны",
        "path": ["Электроника", "Телефоны"],
        "path_ids": ["90", "205"],
        "depth": 2,
        "match": "word",
    }
    provider.answers_with(siphons, phones)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "айфон", "lang": "ru"}).json()

    assert [(row["match"], row["count"]) for row in body["categories"]] == [
        ("word", 0),
        ("substring", 2),
    ], "a stocked plumbing trap must not lead the dropdown"


def test_an_unknown_match_grade_sorts_last_and_does_not_crash(
    api_client, conformance, provider
):
    """A provider that grows a fifth kind degrades to "worst", never to a 500."""
    provider.answers_with({**MENS, "match": "fuzzy-ngram"}, {**KIDS, "match": "prefix"})

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "шорты", "lang": "ru"}).json()

    assert [row["match"] for row in body["categories"]] == ["prefix", "fuzzy-ngram"]


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


def test_a_mid_word_fragment_never_reaches_the_category_matcher(
    api_client, shorts, provider
):
    """«ифон» exists for SERP recall; against category NAMES it is poison.

    The ru group ["iphone", "айфон", "ифон", ...] is right for the SERP,
    where every variant meets full document text under AND-of-groups. The
    category matcher substring-matches NAMES, and there the fragment «ифон»
    finds exactly one thing on a real board: «Сифоны». A variant that is
    buried MID-WORD in a sibling of its own group is dropped for this path
    only; a variant that is a PREFIX of a sibling is a stem («самсунг» ⊂
    «самсунга», «авто» ⊂ «автомобиль») and must survive, or an exact-named
    category stops matching its own name. The dictionary itself is untouched
    — the SERP keeps its recall.
    """
    from stapel_search.suggest import query_terms

    terms = query_terms("айфон", "ru")
    assert "ифон" not in terms, "the fragment must not meet category names"
    assert "айфон" in terms
    assert "iphone" in terms

    # Stems that are prefixes of their inflected siblings survive.
    assert "самсунг" in query_terms("самсунг", "ru")
    assert "авто" in query_terms("авто", "ru")

    # And the wire payload to `categories.suggest` is the filtered set.
    api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "айфон", "lang": "ru"})
    assert "ифон" not in provider.payloads[-1]["terms"]


def test_accept_language_selects_the_dictionary(api_client, shorts, provider):
    """No `lang` parameter: the header must still load the ru dictionary."""
    provider.answers_with(MENS)
    body = api_client.get(
        SUGGEST, {"type": DOC_TYPE, "q": "айфон"}, HTTP_ACCEPT_LANGUAGE="ru-RU,ru;q=0.9"
    ).json()
    assert body["language"] == "ru"
    assert "iphone" in provider.payloads[-1]["terms"]


# --------------------------------------------------------------------------
# goods-driven suggestions: when no NAME matches, the documents answer
# --------------------------------------------------------------------------


@pytest.fixture
def samsung_stock(conformance):
    """Brand-word listings. No category NAME on any board contains «samsung».

    The conformance harness has already loaded its fixed corpus, whose doc 2
    («Samsung Galaxy», electronics/phones) matches the same query — so three
    categories hold matching goods, which is exactly the multi-destination
    shape the dropdown exists for.
    """
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [
            _document(doc_key="g1", title="Samsung Galaxy S21", category_path=("90", "205")),
            _document(doc_key="g2", title="Samsung Galaxy A52", category_path=("90", "205")),
            _document(doc_key="g3", title="Чехол Samsung", category_path=("90", "206")),
        ],
    )
    return conformance


def test_goods_lead_where_names_cannot(api_client, samsung_stock, provider):
    """«samsung» found nothing by NAME and real listings by SERP — a dropdown
    that answers "that does not exist" about goods the very next page shows
    is the second half of the live defect. When the name matcher has no
    strong answer, the backend is asked which categories hold matching
    documents, and those become rows graded ``listings``.
    """
    provider.answers_with()

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    rows = body["categories"]
    assert rows, "the goods exist; the dropdown must lead to them"
    assert [row["match"] for row in rows] == ["listings", "listings", "listings"]
    # Busier place first, and the filter is ready to paste into /query.
    assert rows[0]["category"] == "90/205"
    assert rows[0]["count"] == 2
    assert {(row["category"], row["count"]) for row in rows[1:]} == {
        ("90/206", 1),
        ("electronics/phones", 1),
    }


def test_a_goods_row_count_is_the_serp_count(api_client, samsung_stock, provider):
    """The suggest gate, applied to the new rows: the number shown is the
    number the tap finds. Asserted against the query endpoint, not a
    hand-written expectation, for the same reason as the name-matched gate."""
    provider.answers_with()

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    assert body["categories"]
    for row in body["categories"]:
        serp = api_client.get(
            QUERY,
            {
                "type": DOC_TYPE,
                "q": "samsung",
                "lang": "ru",
                "category": row["category"],
                "facets": "off",
            },
        ).json()
        assert row["count"] == serp["count"], row["category"]


def test_a_strong_name_match_keeps_the_goods_fallback_silent(
    api_client, samsung_stock, provider
):
    """The canon, half one: a name that really says the word IS the answer.

    Goods-driven rows are a fallback, not a second voice: when the name
    matcher produced anything in the strong class (exact/prefix/word), the
    backend is not consulted and no ``listings`` row appears. A name row is
    a promise about the whole category; a co-occurrence in documents is
    weaker evidence and must not dilute it.
    """
    phones = {
        "id": 205,
        "slug": "telefony",
        "name": "Телефоны",
        "path": ["Электроника", "Телефоны"],
        "path_ids": ["90", "205"],
        "depth": 2,
        "match": "word",
    }
    provider.answers_with(phones)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    assert [row["match"] for row in body["categories"]] == ["word"]


def test_goods_outrank_a_substring_name_row(api_client, samsung_stock, provider):
    """The canon, half two: goods beat a mid-word accident.

    A substring row is not a strong name answer, so the fallback still runs
    beside it — and the documents themselves are better evidence of where
    the query leads than a fragment buried in an unrelated name. The
    substring row is kept (it may still be the right place on a sparse
    board), below.
    """
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [_document(doc_key="s1", title="Сифон для раковины", category_path=("80", "81", "202"))],
    )
    siphons = {
        "id": 202,
        "slug": "sifony",
        "name": "Сифоны",
        "path": ["Для дома", "Трубы и фитинги", "Сифоны"],
        "path_ids": ["80", "81", "202"],
        "depth": 3,
        "match": "substring",
    }
    provider.answers_with(siphons)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    assert [row["match"] for row in body["categories"]] == [
        "listings",
        "listings",
        "listings",
        "substring",
    ]
    assert body["categories"][0]["category"] == "90/205"


def test_a_backend_without_the_verb_degrades_loudly(
    api_client, samsung_stock, provider, monkeypatch
):
    """`suggest_categories` is an OPTIONAL backend verb: an engine that does
    not implement it answers exactly what 0.9.0 answered, and the shortfall
    travels in `degraded[]` rather than being absorbed — the same rule every
    other engine difference follows."""
    from stapel_search.backends import get_backend

    # Whichever engine this suite runs under (naive on SQLite, Postgres on a
    # real server), strip the verb from ITS class so the probe finds nothing.
    monkeypatch.delattr(type(get_backend()), "suggest_categories", raising=False)
    provider.answers_with()

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    assert body["categories"] == []
    assert "category_listing_suggestions" in body["degraded"]


@pytest.fixture
def names_provider():
    """Register a stand-in for ``categories.names`` — the id -> display-name
    read the goods rows resolve through. Same division of labour as the
    ``categories.suggest`` stand-in above: the real provider is
    stapel-categories\' business; what is under test HERE is that this module
    asks, applies the answer, and says so when no answer comes.
    """
    calls: list[dict] = []
    names: dict[str, dict] = {}

    def _provider(payload):
        calls.append(payload)
        return {"names": {k: dict(v) for k, v in names.items()}}

    register_function("categories.names", _provider)

    class Handle:
        payloads = calls

        @staticmethod
        def resolves(mapping):
            names.clear()
            names.update(mapping)

    try:
        yield Handle
    finally:
        function_registry._providers.pop("categories.names", None)


def test_goods_rows_carry_display_names(api_client, samsung_stock, provider, names_provider):
    """A goods row is a place, not a number: every path segment the names
    provider knows is rendered by its display name, the leaf name and slug
    ride the row, and the ``category`` filter string keeps the IDS — the
    tap must land on ``?category=90/205`` exactly as before.
    """
    provider.answers_with()
    names_provider.resolves(
        {
            "90": {"name": "Электроника", "slug": "elektronika"},
            "205": {"name": "Телефоны", "slug": "telefony"},
            "206": {"name": "Аксессуары", "slug": "aksessuary"},
        }
    )

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    rows = {row["category"]: row for row in body["categories"]}
    assert rows["90/205"]["name"] == "Телефоны"
    assert rows["90/205"]["slug"] == "telefony"
    assert rows["90/205"]["path"] == ["Электроника", "Телефоны"]
    assert rows["90/206"]["name"] == "Аксессуары"
    # A segment the provider does not know keeps its id — a truthful
    # segment, never an invented name (the conformance corpus path).
    assert rows["electronics/phones"]["name"] == "phones"
    assert "category_names" not in body["degraded"]
    # One batched round trip, ids deduplicated.
    assert len(names_provider.payloads) == 1
    assert sorted(names_provider.payloads[0]["ids"]) == [
        "205", "206", "90", "electronics", "phones",
    ]


def test_goods_rows_without_a_names_provider_keep_ids_and_declare_it(
    api_client, samsung_stock, provider
):
    """No ``categories.names`` responder: the rows still lead somewhere (id
    segments are truthful), and the shortfall travels as
    ``degraded: ["category_names"]`` instead of being absorbed."""
    provider.answers_with()

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    rows = {row["category"]: row for row in body["categories"]}
    assert rows["90/205"]["name"] == "205"
    assert "category_names" in body["degraded"]


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


# --------------------------------------------------------------------------
# the destination the row itself declares (0.11.0)
# --------------------------------------------------------------------------


def _follow(api_client, row, *, doc_type, language):
    """Run /query with EXACTLY the parameters the row declares, and count."""
    params = {"type": doc_type, "lang": language, "facets": "off", **row["query"]}
    return api_client.get(QUERY, params).json()["count"]


def test_a_name_row_declares_a_query_free_destination(api_client, shorts, provider):
    """A name row is a PLACE. Its count is the place's stock, so its
    destination must not carry the typed text.

    The live defect this closes: «одежда» offered «Одежда, обувь,
    аксессуары · 2», the storefront followed it to `?category=…&q=одежда`,
    and the page was EMPTY — no listing spells the category's own name in
    its title. The count was honest about a page the tap never opened.
    """
    provider.answers_with(CLOTHES)
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "одежда", "lang": "ru"}).json()

    row = body["categories"][0]
    assert row["count_scope"] == "category"
    assert row["query"] == {"category": "46"}, "a place is not a text query"


def test_a_goods_row_declares_a_destination_that_keeps_the_query(
    api_client, samsung_stock, provider
):
    """A goods row is «where your words lead», so its destination keeps them.

    The two row kinds mean different things by `count`, and before 0.11.0
    the answer never said which — so one storefront rule had to be wrong for
    one of them. The row now carries its own parameters.
    """
    provider.answers_with()
    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "samsung", "lang": "ru"}).json()

    rows = body["categories"]
    assert rows
    for row in rows:
        assert row["count_scope"] == "query_in_category"
        assert row["query"] == {"category": row["category"], "q": "samsung"}


@pytest.mark.parametrize(
    "stock,answers,q",
    [
        ("shorts", (MENS, WOMENS, KIDS, CLOTHES), "шорты"),
        ("shorts", (CLOTHES,), "одежда"),
        ("samsung_stock", (), "samsung"),
    ],
)
def test_every_row_count_is_the_count_of_the_page_it_opens(
    api_client, request, provider, stock, answers, q
):
    """THE gate, and the one the two older ones could not be.

    ``test_suggest_count_equals_the_serp_count`` follows a name row with no
    ``q``; ``test_a_goods_row_count_is_the_serp_count`` follows a goods row
    WITH ``q``. Each hard-codes the destination its own row kind assumes, so
    between them they prove every arithmetic and nothing about the seam: a
    storefront reading the answer has no field telling it which rule applies
    and must guess — and the guess it made on the stand was wrong for every
    name row.

    This one follows what the ROW declares. It fails on any row whose
    ``query`` does not open the page its ``count`` promises, whatever kind
    of row it is and whatever kinds are added later.
    """
    request.getfixturevalue(stock)
    provider.answers_with(*answers)

    body = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": q, "lang": "ru"}).json()

    assert body["categories"], "nothing to prove"
    for row in body["categories"]:
        assert _follow(api_client, row, doc_type=DOC_TYPE, language="ru") == row["count"], (
            row["category"],
            row["match"],
            row["query"],
        )
