"""Feature keys in the address: `f.make`, not `f.make_ref_select` (0.14.4).

The importer mints a type suffix onto every slug it creates, and the suffix
then travels in the address bar: `/s?category=141/151&f.make_ref_select=
toyota` tells the reader how the make is STORED and nothing they wanted to
know. The suffix is dropped where dropping it stays unambiguous among the
features of the category in scope — that scope is the load-bearing half. An
audit of the imported catalogue found 181 suffixed slugs, and stripping the
suffix GLOBALLY collides for every one of them (29 bases carry two or three
differently-typed variants), so the short form is derived per category, per
request, and nothing about the stored slug changes.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


def _feature(slug, type_slug, **config):
    return {
        "id": abs(hash(slug)) % 10000,
        "slug": slug,
        "name": slug,
        "mandatory": False,
        "show_at_title": False,
        "show_as_badge": False,
        "translate": "none",
        "config": {"type": type_slug, **config},
    }


CAR_FEATURES = [
    # Unambiguous: nothing else in this scope shortens to `make`.
    _feature(
        "make_ref_select",
        "ref_select",
        optionsRef={"vocabulary": "autocatalog", "level": "Make"},
    ),
    _feature("year_int", "int"),
    # Two variants of one base: `condition` names neither of them.
    _feature("condition_select", "select", options=[{"value": "b-u", "label": "Б/у"}]),
    _feature(
        "condition_ref_select",
        "ref_select",
        optionsRef={"vocabulary": "autocatalog", "level": "Condition"},
    ),
    # The short form of `body_select` is a REAL slug of this scope.
    _feature("body", "select", options=[{"value": "sedan", "label": "Sedan"}]),
    _feature("body_select", "select", options=[{"value": "suv", "label": "SUV"}]),
    # No suffix to drop.
    _feature("vin", "string"),
]


@pytest.fixture
def car_scope():
    """`categories.features` for the leaf the address is scoped to."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def provider(payload):
        return {
            "category_id": payload["category_id"],
            "revision": 1,
            "features": CAR_FEATURES,
        }

    register_function("categories.features", provider)
    yield CAR_FEATURES
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def _index_cars():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key="101",
                title="Toyota Camry",
                card={"title": "Toyota Camry"},
                category_id="151",
                category_path=("141", "151"),
                features={
                    "make_ref_select": {"type": "ref_select", "value": ["toyota"]},
                    "year_int": {"type": "int", "value": [2018]},
                    "condition_select": {"type": "select", "value": ["b-u"]},
                    "condition_ref_select": {"type": "ref_select", "value": ["used"]},
                    "body": {"type": "select", "value": ["sedan"]},
                    "body_select": {"type": "select", "value": ["suv"]},
                },
            ),
            _document(
                doc_key="102",
                title="Lada Vesta",
                card={"title": "Lada Vesta"},
                category_id="151",
                category_path=("141", "151"),
                features={
                    "make_ref_select": {"type": "ref_select", "value": ["lada"]},
                    "year_int": {"type": "int", "value": [2012]},
                    "condition_select": {"type": "select", "value": ["b-u"]},
                    "condition_ref_select": {"type": "ref_select", "value": ["used"]},
                    "body": {"type": "select", "value": ["sedan"]},
                    "body_select": {"type": "select", "value": ["suv"]},
                },
            ),
        ],
    )


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------


def test_the_type_suffix_is_dropped_when_the_scope_leaves_it_unambiguous(car_scope):
    from stapel_search.facets import url_keys

    keys = url_keys("151")
    assert keys["make_ref_select"] == "make"
    assert keys["year_int"] == "year"
    assert keys["vin"] == "vin", "no suffix, nothing to drop"


def test_a_collision_inside_the_scope_keeps_the_full_slug(car_scope):
    """Two variants of one base name it equally well, so neither takes it."""
    from stapel_search.facets import url_keys

    keys = url_keys("151")
    assert keys["condition_select"] == "condition_select"
    assert keys["condition_ref_select"] == "condition_ref_select"


def test_a_real_slug_owns_its_key(car_scope):
    """`body` is a feature. `body_select` may not answer to it."""
    from stapel_search.facets import resolve_url_key, url_keys

    keys = url_keys("151")
    assert keys["body"] == "body"
    assert keys["body_select"] == "body_select"
    assert resolve_url_key("body", keys) == "body"


def test_no_category_scope_shortens_nothing(car_scope):
    """A text query addresses no category, so no short form is derived: the
    rule is only sound where it was computed."""
    from stapel_search.facets import resolve_url_key, url_keys

    assert url_keys(None) == {}
    assert resolve_url_key("make", {}) == "make"


def test_the_longest_suffix_wins(car_scope):
    """`make_ref_select` ends with `_select` too; the rule is deterministic."""
    from stapel_search.facets import _short_key

    assert _short_key("make_ref_select") == "make"
    assert _short_key("_select") is None, "a slug that is only a suffix is not one"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_the_short_key_parses_to_what_the_full_slug_parses_to(conformance, car_scope):
    """`f.make=toyota` in scope 141/151 IS `f.make_ref_select=toyota`."""
    from stapel_search.facets import url_keys
    from stapel_search.query import parse_query, resolve_feature_keys

    keys = url_keys("151")
    short = resolve_feature_keys(
        parse_query({"type": DOC_TYPE, "category": "141/151", "f.make": "toyota"}), keys
    )
    full = resolve_feature_keys(
        parse_query(
            {"type": DOC_TYPE, "category": "141/151", "f.make_ref_select": "toyota"}
        ),
        keys,
    )
    assert short.facets == full.facets == {"make_ref_select": ["toyota"]}
    assert short == full


def test_a_range_takes_the_short_key_too(conformance, car_scope):
    from stapel_search.facets import url_keys
    from stapel_search.query import parse_query, resolve_feature_keys

    q = resolve_feature_keys(
        parse_query({"type": DOC_TYPE, "category": "141/151", "r.year": "2015..2020"}),
        url_keys("151"),
    )
    assert [band.slug for band in q.ranges] == ["year_int"]


def test_an_unknown_key_is_left_exactly_as_it_arrived(conformance, car_scope):
    """0.14.3 ignores a facet nothing declares — quietly, at HTTP 200. This
    rule does not turn that into a refusal; it only renames what it knows."""
    from stapel_search.facets import url_keys
    from stapel_search.query import parse_query, resolve_feature_keys

    q = resolve_feature_keys(
        parse_query({"type": DOC_TYPE, "category": "141/151", "f.nonsense": "x"}),
        url_keys("151"),
    )
    assert q.facets == {"nonsense": ["x"]}


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


def test_the_short_key_filters_the_page_the_full_slug_filters(conformance, car_scope):
    from stapel_search.services import search

    _index_cars()
    short = search({"type": DOC_TYPE, "category": "141/151", "f.make": "toyota"})
    full = search(
        {"type": DOC_TYPE, "category": "141/151", "f.make_ref_select": "toyota"}
    )
    assert [i["key"] for i in short["items"]] == [i["key"] for i in full["items"]] == ["101"]


def test_every_group_states_its_url_key(conformance, car_scope):
    """A client writes `url_key` and never derives it, so every group owes
    one — including the groups whose key is the slug itself."""
    from stapel_search.services import search

    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "141/151"})
    assert answer["facets"], "the scope declares axes"
    for slug, labels in answer["facet_labels"].items():
        assert labels.get("url_key"), slug
    assert answer["facet_labels"]["make_ref_select"]["url_key"] == "make"
    assert (
        answer["facet_labels"]["condition_select"]["url_key"] == "condition_select"
    ), "a collision is stated as itself, never guessed at by the client"


def test_outside_a_scope_the_answer_states_the_slug(conformance, car_scope):
    """No category: nothing is shortened, and the answer says so rather than
    handing a client a key that would not resolve."""
    from stapel_search.services import search

    _index_cars()
    answer = search({"type": DOC_TYPE})
    for slug, labels in answer["facet_labels"].items():
        assert labels["url_key"] == slug, slug


# --------------------------------------------------------------------------
# a `chips` parent has a scope now (stapel-categories 0.20.1)
# --------------------------------------------------------------------------


@pytest.fixture
def chips_parent_scope():
    """`categories.features` for a partition parent that declares nothing.

    0.20.1 answers such a node with the INTERSECTION of its children's
    schemas and `effective_from: "children"`. Before it, the node answered
    an empty list — so it had no scope, and 0.14.4's rule shortened nothing
    on the page a chip row is drawn over. The scope now has features, and
    the same rule applies to it unchanged.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        _feature(
            "make_ref_select",
            "ref_select",
            optionsRef={"vocabulary": "autocatalog", "level": "Make"},
        ),
        _feature("year_int", "int"),
        _feature("vin", "string"),
    ]

    def provider(payload):
        cid = str(payload["category_id"])
        return {
            "category_id": cid,
            "revision": 1,
            "effective_from": "children" if cid == "141" else "own",
            "features": features if cid == "141" else [],
        }

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_a_chips_parent_shortens_keys_from_its_effective_schema(chips_parent_scope):
    """The parent's scope is its children's intersection, and it is a scope."""
    from stapel_search.facets import resolve_url_key, scope_slugs, url_keys

    assert scope_slugs("141") == ("make_ref_select", "year_int", "vin")
    keys = url_keys("141")
    assert keys["make_ref_select"] == "make"
    assert keys["year_int"] == "year"
    assert keys["vin"] == "vin"
    assert resolve_url_key("year", keys) == "year_int"


def test_a_node_with_no_effective_schema_still_shortens_nothing(chips_parent_scope):
    """A `tiles` branch answers its own (empty) schema, and the rule holds."""
    from stapel_search.facets import url_keys

    assert url_keys("999") == {}
