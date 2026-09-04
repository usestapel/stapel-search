"""`category=` takes ids, slugs and any mix, and says which node it landed on.

A person reading the address bar must know what the page is (readable
addresses, 2026-09-04), so a category link is `/c/avtomobili` and the feed
behind it is `?category=avtomobili`. The id stays authoritative — it is what
the index filters on — but it is no longer the only thing the parameter
accepts, and a client that holds one form must be able to write the other.

`Category.slug` is `unique=True` across the whole tree, so a single leaf
slug is a complete address: `avtomobili` names exactly one node, and its
ancestry comes back with it. Everything here is the 0.14.2 bare-id
resolution over that second key — same provider abstraction, same three
outcomes, and an unreachable provider still degrades rather than 400s.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db

#: One branch: 141 `transport` -> 151 `avtomobili`.
PATHS = {"141": ["141"], "151": ["141", "151"]}
SLUG_PATHS = {"transport": ["141"], "avtomobili": ["141", "151"]}
NAMES = {
    "141": {"name": "Транспорт", "slug": "transport"},
    "151": {"name": "Автомобили", "slug": "avtomobili"},
}


def _register(name, provider):
    from stapel_core.comm import register_function

    register_function(name, provider)


def _unregister(*names):
    from stapel_core.comm.registry import function_registry

    for name in names:
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)


@pytest.fixture
def ids_only():
    """Only `categories.path` — the fleet as 0.14.2 shipped into it."""
    def paths(payload):
        wanted = [str(i) for i in payload.get("category_ids") or []]
        return {i: PATHS[i] for i in wanted if i in PATHS}

    _register("categories.path", paths)
    yield PATHS
    _unregister("categories.path")


@pytest.fixture
def tree(ids_only):
    """The whole seam: ids, slugs, and the names that caption them."""
    def by_slug(payload):
        wanted = [str(s) for s in payload.get("slugs") or []]
        return {s: SLUG_PATHS[s] for s in wanted if s in SLUG_PATHS}

    def names(payload):
        wanted = [str(i) for i in payload.get("ids") or []]
        return {"names": {i: NAMES[i] for i in wanted if i in NAMES}}

    _register("categories.by_slug", by_slug)
    _register("categories.names", names)
    yield SLUG_PATHS
    _unregister("categories.by_slug", "categories.names")


def _index_two_cars():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=title,
                card={"title": title},
                category_id="151",
                category_path=("141", "151"),
            )
            for key, title in (("201", "Toyota Camry"), ("202", "Lada Vesta"))
        ],
    )


def _keys(answer):
    return [item["key"] for item in answer["items"]]


# --------------------------------------------------------------------------
# every form addresses the same node
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    ["141/151", "151", "avtomobili", "transport/avtomobili", "141/avtomobili",
     "transport/151"],
)
def test_every_form_of_the_address_answers_the_same_page(conformance, tree, category):
    """Six spellings of one node: an id path, a bare id, a bare slug, a slug
    path and both mixtures. The index holds ids, so all six have to become
    `141/151` before the prefix filter looks at them."""
    from stapel_search.services import search

    _index_two_cars()
    answer = search({"type": DOC_TYPE, "category": category})

    assert answer["count"] == 2
    assert _keys(answer) == ["201", "202"]
    assert answer["category_resolved"]["path"] == "141/151"


def test_a_slug_path_still_filters_by_prefix(conformance, tree):
    """Resolution does not turn a branch into a leaf: `transport` is the same
    prefix filter `141` is, and it still finds what is under it."""
    from stapel_search.services import search

    _index_two_cars()
    assert search({"type": DOC_TYPE, "category": "transport"})["count"] == 2
    assert search({"type": DOC_TYPE, "category": "141"})["count"] == 2


# --------------------------------------------------------------------------
# the answer says which node, in both forms
# --------------------------------------------------------------------------


def test_the_answer_echoes_the_slug_path_for_a_request_made_of_ids(conformance, tree):
    """The rewrite a storefront needs: it was linked to by ids and has to put
    `/c/avtomobili` in the address bar."""
    from stapel_search.services import search

    _index_two_cars()
    answer = search({"type": DOC_TYPE, "category": "141/151"})

    assert answer["category_resolved"] == {
        "path": "141/151",
        "slugs": ["transport", "avtomobili"],
    }


def test_the_answer_echoes_the_id_path_for_a_request_made_of_slugs(conformance, tree):
    """And the other direction: a page addressed by slug still has to send
    ids to anything that speaks ids (a saved search, an analytics event)."""
    from stapel_search.services import search

    _index_two_cars()
    answer = search({"type": DOC_TYPE, "category": "avtomobili"})

    assert answer["category_resolved"] == {
        "path": "141/151",
        "slugs": ["transport", "avtomobili"],
    }


def test_no_category_means_no_resolved_form(conformance, tree):
    """A key that is always present and null when there is nothing to say —
    a client reads it without asking whether it is there."""
    from stapel_search.services import search

    _index_two_cars()
    assert search({"type": DOC_TYPE})["category_resolved"] is None


def test_the_slug_half_is_null_rather_than_partial(conformance, ids_only):
    """`categories.names` is down, so the ancestor's slug is unknown. Half a
    slug path builds a WRONG address and a client cannot see that by looking,
    so the list is null and `degraded[]` says which provider is missing."""
    from stapel_search.services import search

    _index_two_cars()
    answer = search({"type": DOC_TYPE, "category": "141/151"})

    assert answer["category_resolved"] == {"path": "141/151", "slugs": None}
    assert "category_names" in answer["degraded"]


# --------------------------------------------------------------------------
# unknown, and unreachable, stay two different answers
# --------------------------------------------------------------------------


def test_a_slug_no_category_has_is_a_400_naming_it(conformance, tree):
    """The bare-id rule, unchanged for the other key: `count: 0` cannot be
    told apart from an empty category, so a typo in a link and a catalogue
    that lost a branch looked identical."""
    from stapel_search.errors import ERR_400_UNKNOWN_CATEGORY, SearchValidationError
    from stapel_search.services import search

    with pytest.raises(SearchValidationError) as raised:
        search({"type": DOC_TYPE, "category": "avtomobil"})
    assert raised.value.code == ERR_400_UNKNOWN_CATEGORY
    assert raised.value.params["category"] == "avtomobil"


def test_an_unknown_segment_of_a_slug_path_names_that_segment(conformance, tree):
    """Which of the segments is wrong is the whole content of the message."""
    from stapel_search.errors import SearchValidationError
    from stapel_search.services import search

    with pytest.raises(SearchValidationError) as raised:
        search({"type": DOC_TYPE, "category": "transport/avtomobil"})
    assert raised.value.params["category"] == "avtomobil"


def test_the_unknown_slug_reaches_the_client_as_a_400(conformance, tree, api_client):
    response = api_client.get("/api/v1/query", {"type": DOC_TYPE, "category": "avtomobil"})
    assert response.status_code == 400
    assert "search_unknown_category" in response.content.decode()


def test_an_unreachable_slug_provider_is_not_the_callers_fault(conformance, ids_only):
    """No `categories.by_slug` in the fleet — which is every fleet today. A
    slug segment stands as it was written, the answer says the rollup could
    not be built, and nothing about the request became invalid."""
    from stapel_search.services import search

    _index_two_cars()
    answer = search({"type": DOC_TYPE, "category": "avtomobili"})

    assert "category_rollup" in answer["degraded"]
    assert answer["count"] is not None
    assert answer["category_resolved"]["path"] == "avtomobili"


def test_an_unknown_id_is_still_a_400_while_slugs_have_no_provider(
    conformance, ids_only
):
    """The trap this ordering avoids: reading every unknown id as a possible
    slug would have turned 0.14.2's 400 into a degradation everywhere,
    because the slug provider does not exist yet."""
    from stapel_search.errors import ERR_400_UNKNOWN_CATEGORY, SearchValidationError
    from stapel_search.services import search

    with pytest.raises(SearchValidationError) as raised:
        search({"type": DOC_TYPE, "category": "999999"})
    assert raised.value.code == ERR_400_UNKNOWN_CATEGORY


def test_a_numeric_slug_is_found_after_the_id_lookup_misses(conformance, tree):
    """Ids win a numeric segment, but they do not own it: a catalogue whose
    slug is `2107` still answers, once no category has that id."""
    from stapel_search.services import search

    _index_two_cars()
    SLUG_PATHS["2107"] = ["141", "151"]
    try:
        answer = search({"type": DOC_TYPE, "category": "2107"})
    finally:
        SLUG_PATHS.pop("2107")
    assert answer["count"] == 2
    assert answer["category_resolved"]["path"] == "141/151"
