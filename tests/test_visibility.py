"""The visibility axis: what an identifier must never make searchable.

A VIN, an IMEI, a serial number is a legitimate catalogue field and an
illegitimate search axis. Indexing one gives a stranger three exact-value
oracles for free — ``?f.vin=<value>`` matched a synthesized term,
``?r.mileage=X..X`` answered off the ``SearchNumber`` side table, and
``?facets=vin`` re-enumerated the values with counts — each of which turns
"do you know the number?" into "which listing is that car?".

Every test here is a fence, not a demonstration: each one fails the moment
somebody re-admits a hidden slug on one of the three paths.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE, _document, _feature

pytestmark = pytest.mark.django_db


def _hidden(type_slug: str, value, visibility: str = "owner") -> dict:
    """A DAO as ``registry.dto_to_dao`` stamps it for a non-public FeatureDef."""
    return {"type": type_slug, "value": value, "visibility": visibility}


@pytest.fixture
def hidden_category():
    """A ``categories.features`` whose `vin`/`mileage` are not public.

    Shaped like the imported cars leaf the axis was written for: a public
    `brand` beside a `vin` the catalogue marks `owner`, so every assertion
    below can prove the filter is TARGETED and not a blanket refusal.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "brand", "name": "Brand", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "select",
                "options": [{"value": "apple", "label": "Apple"},
                            {"value": "toyota", "label": "Toyota"}],
            },
        },
        {
            "id": 2, "slug": "vin", "name": "VIN", "mandatory": True,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "visibility": "owner",
            "config": {"type": "string"},
        },
        {
            "id": 3, "slug": "mileage", "name": "Mileage", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            # The second reading position: on the config, not on the feature.
            "config": {"type": "int", "visibility": "staff"},
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


# --------------------------------------------------------------------------
# A — the writer. A hidden value never enters the index.
# --------------------------------------------------------------------------


def test_a_hidden_dao_is_not_written_to_any_of_the_three_index_shapes():
    """No `facets` key, no `facet_terms` entry, no `SearchNumber` row.

    All three, because each is an exact-value oracle on its own and dropping
    two of them would leave the third answering the same question.
    """
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="v1",
        features={
            "vin": _hidden("string", "JTDBR32E0"),
            "mileage": _hidden("int", 120000, "staff"),
        },
    )
    facets, terms, numbers, _ = build_facets(doc)
    assert facets == {}
    assert terms == []
    assert numbers == {}


def test_a_public_sibling_in_the_same_document_is_still_indexed():
    """The filter is targeted at the value, not at the document.

    Without this, "no VIN leaked" would also be satisfied by an indexer that
    quietly stopped indexing every attribute of every car.
    """
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="v2",
        features={
            "brand": _feature("string", "toyota"),
            "year": _feature("int", 2015),
            "vin": _hidden("string", "JTDBR32E0"),
        },
    )
    facets, terms, numbers, _ = build_facets(doc)
    assert facets == {"brand": ["toyota"], "year": [2015]}
    assert terms == ["brand=toyota"]  # `int` is a range axis, not a term one
    assert set(numbers) == {"year"}


def test_an_unrecognized_visibility_is_treated_as_hidden():
    """`normalize_visibility` raises on a typo; the indexer's answer to "I
    cannot tell what this means" is not to publish a VIN."""
    from stapel_search.services import build_facets

    doc = _document(doc_key="v3", features={"vin": _hidden("string", "JTDBR32E0", "private")})
    facets, terms, numbers, _ = build_facets(doc)
    assert (facets, terms, numbers) == ({}, [], {})


def test_the_features_search_fallback_obeys_the_producers_denylist():
    """`features_search` is `{slug: [values]}` — values only, no stamp — so
    `hidden_features` is the only channel it has. A producer that hands over
    the lossy projection must hand over the denylist with it."""
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="v4",
        features={},
        features_search={"brand": ["toyota"], "vin": ["JTDBR32E0"]},
        hidden_features=("vin",),
    )
    facets, terms, numbers, _ = build_facets(doc)
    assert facets == {"brand": ["toyota"]}
    assert terms == ["brand=toyota"]
    assert numbers == {}


def test_the_denylist_is_obeyed_on_the_dao_path_too():
    """An explicit denylist beats a missing stamp: a document projected by an
    older producer carries no `visibility`, and the producer may still know."""
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="v5",
        features={"brand": _feature("string", "toyota"), "vin": _feature("string", "JTDBR32E0")},
        hidden_features=("vin",),
    )
    facets, terms, _, _ = build_facets(doc)
    assert "vin" not in facets
    assert terms == ["brand=toyota"]


def test_hidden_features_defaults_to_empty_so_existing_producers_are_unaffected():
    """The field is optional by construction — a mapper written against 0.8
    keeps compiling and keeps indexing exactly what it indexed."""
    from stapel_search.dto import SearchDocumentInput

    assert SearchDocumentInput(doc_type=DOC_TYPE, doc_key="1").hidden_features == ()


# --------------------------------------------------------------------------
# B — the plan. A hidden slug is never counted and never re-admitted.
# --------------------------------------------------------------------------


def test_a_non_public_feature_is_excluded_from_the_plan(hidden_category):
    from stapel_search.facets import facet_plan

    plan = facet_plan("7")
    assert plan.slugs == ("brand",)
    assert "vin" not in plan.kinds
    assert "mileage" not in plan.kinds
    assert plan.hidden == ("mileage", "vin")


def test_an_explicitly_requested_hidden_slug_is_not_re_admitted(hidden_category):
    """`?facets=vin` re-enumerated the values with counts. The exclusion is
    the HARD one: unlike a slug skipped at MAX_FACET_FIELDS, no request
    brings it back."""
    from stapel_search.facets import facet_plan

    plan = facet_plan("7", requested=("vin", "mileage", "brand"))
    assert plan.slugs == ("brand",)
    assert "vin" not in plan.kinds
    assert "mileage" not in plan.kinds
    assert "vin" not in plan.skipped


def test_a_category_that_says_nothing_about_visibility_plans_as_before():
    """`public` is the default: a definition written before the axis existed
    must not lose its facet."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry
    from stapel_search.facets import facet_plan

    features = [{
        "id": 1, "slug": "brand", "name": "Brand", "mandatory": False,
        "show_at_title": False, "show_as_badge": False, "translate": "none",
        "config": {"type": "select", "options": [{"value": "apple", "label": "Apple"}]},
    }]
    register_function(
        "categories.features",
        lambda payload: {"category_id": payload["category_id"], "revision": 1,
                         "features": features},
    )
    try:
        plan = facet_plan("7")
        assert plan.slugs == ("brand",)
        assert plan.hidden == ()
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)


# --------------------------------------------------------------------------
# C — the reader. A hidden slug is not filterable, indexed or not.
# --------------------------------------------------------------------------


def _index(**overrides):
    from stapel_search.services import index_documents

    overrides.setdefault("category_id", "c1")
    overrides.setdefault("category_path", ("c1",))
    overrides.setdefault("card", {"title": overrides.get("title", "")})
    index_documents(DOC_TYPE, [_document(**overrides)])


def test_an_exact_vin_query_finds_nothing_because_nothing_was_indexed(
    conformance, hidden_category
):
    """The end-to-end shape of the leak: `?f.vin=<value>` was a working
    exact-match oracle over an identifier.

    Asked WITHOUT a category on purpose, so nothing drops the filter and the
    engine really goes looking for the term. It finds none, because the writer
    never wrote one — which is also the answer to the cross-category hole
    `_drop_hidden_filters` documents: after a rebuild there is nothing to
    match, whatever category the filter was aimed at.
    """
    from stapel_search.services import search

    _index(
        doc_key="c-1",
        title="Toyota Camry",
        features={
            "brand": _feature("select", ["toyota"]),
            "vin": _hidden("string", "JTDBR32E0"),
        },
    )
    assert search({"type": DOC_TYPE, "f.vin": "JTDBR32E0"})["items"] == []
    # The control: the same document IS findable by its public attribute.
    # Without this, an index that simply lost the document would pass.
    assert [item["key"] for item in search(
        {"type": DOC_TYPE, "f.brand": "toyota"}
    )["items"]] == ["c-1"]


def test_a_range_query_on_a_hidden_numeric_finds_nothing(conformance, hidden_category):
    """The `SearchNumber` oracle: a bisection on `r.mileage` recovers the exact
    value in twenty queries even though no term was ever written. No row is
    written now, so the semi-join has nothing to join to."""
    from stapel_search.services import search

    _index(
        doc_key="c-2",
        title="Toyota Camry",
        features={
            "brand": _feature("select", ["toyota"]),
            "mileage": _hidden("int", 120000, "staff"),
            "year": _feature("int", 1999),
        },
    )
    assert search({"type": DOC_TYPE, "r.mileage": "100000..200000"})["items"] == []
    # The control: a PUBLIC numeric on the same document still answers a range.
    assert [item["key"] for item in search(
        {"type": DOC_TYPE, "r.year": "1990..2000"}
    )["items"]] == ["c-2"]


def test_a_filter_on_a_hidden_slug_is_dropped_even_when_the_index_still_has_it(
    conformance, hidden_category
):
    """The belt to the writer's braces, and the reason it is not redundant:
    documents indexed BEFORE this release still carry the term until somebody
    runs `search_rebuild`. The read path refuses the filter meanwhile.

    Dropped rather than answered-empty, and the assertion says what that
    buys: the answer is IDENTICAL to the one without the filter, so it
    discriminates nothing. A filter that narrowed — either to the matching
    document or to none — would still be an oracle.
    """
    from stapel_search.services import search

    # Two documents as an older release indexed them: the VINs carry no stamp,
    # so the writer had no way to know and the terms ARE in the index.
    _index(doc_key="c-3", title="Toyota Camry",
           features={"brand": _feature("select", ["toyota"]),
                     "vin": _feature("string", "JTDBR32E0")})
    _index(doc_key="c-4", title="Toyota Corolla",
           features={"brand": _feature("select", ["toyota"]),
                     "vin": _feature("string", "SOMETHINGELSE")})

    answer = search({"type": DOC_TYPE, "category": "c1", "f.vin": "JTDBR32E0"})
    assert answer["facet_meta"]["dropped_filters"] == ["vin"], "dropped, never silently"
    unfiltered = search({"type": DOC_TYPE, "category": "c1"})
    assert [item["key"] for item in answer["items"]] == [
        item["key"] for item in unfiltered["items"]
    ], "the stale terms are still there; the filter distinguishes nothing"


def test_a_dropped_range_filter_is_reported_too(conformance, hidden_category):
    from stapel_search.services import search

    _index(doc_key="c-6", title="Toyota Camry",
           features={"mileage": _feature("int", 120000)})
    answer = search({"type": DOC_TYPE, "category": "c1", "r.mileage": "0..1"})
    assert answer["facet_meta"]["dropped_filters"] == ["mileage"]
    assert [item["key"] for item in answer["items"]] == ["c-6"], (
        "a range nothing satisfies would have emptied the board if it had run"
    )


def test_a_public_filter_is_never_dropped(conformance, hidden_category):
    """The targeting assertion for the read path: dropping everything would
    also make every leak test above pass."""
    from stapel_search.services import search

    _index(doc_key="c-5", title="Toyota Camry",
           features={"brand": _feature("select", ["toyota"])})
    answer = search({"type": DOC_TYPE, "category": "c1", "f.brand": "toyota"})
    assert [item["key"] for item in answer["items"]] == ["c-5"]
    assert answer["facet_meta"]["dropped_filters"] == []


def test_a_hidden_slug_is_absent_from_the_counted_panel(conformance, hidden_category):
    """`?facets=vin` end to end: the panel does not re-enumerate the values."""
    from stapel_search.services import search

    _index(doc_key="c-8", title="Toyota Camry",
           features={"brand": _feature("select", ["toyota"]),
                     "vin": _feature("string", "JTDBR32E0")})
    answer = search({"type": DOC_TYPE, "category": "c1", "facets": "vin,brand"})
    assert "vin" not in answer["facets"]
    assert "vin" not in answer["facet_meta"]["counted"]
    assert "brand" in answer["facets"], "the public sibling still counts"
