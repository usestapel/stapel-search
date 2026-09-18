"""Staged dependent facets: general before specific, in search as in posting.

``OptionsRef.parentFeature`` has said since the field existed that a
feature's codes are the children of a sibling's chosen term — brand ->
model, make -> model -> generation, vehicle type -> body type. The posting
form honours it: the child is not offered until the parent carries a value.
The PANEL ignored it entirely, because ``facets.facet_plan`` builds every
group from the index independently, so a phones leaf listed every model of
every make above a reader who had chosen no make. One field, two halves of
one product, two different answers.

What is closed here, and what is deliberately not:

* a dependent group whose parent has no value is returned PRESENT, empty and
  ``gated`` — absent would read as "this leaf has no model filter" — and no
  aggregation is requested for it at all;
* a request that filters on the CHILD and not the parent keeps its filter.
  Dropping it would answer a wider page than the link asked for, which is
  the most expensive kind of wrong answer. The group is counted as usual and
  ``parent_missing`` tells the client to open the parent beside it;
* nothing infers the parent from the child. ``model=iphone-13`` implies
  ``brand=apple`` only to a vocabulary walk, and this iteration does not do
  one.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


BRANDS = {
    "iphone-13": "apple",
    "iphone-14": "apple",
    "galaxy-s23": "samsung",
}


def _feature_def(slug, kind, position, **config):
    return {
        "id": position,
        "slug": slug,
        "name": slug,
        "translate": "none",
        "mandatory": False,
        "show_at_title": False,
        "show_as_badge": False,
        "config": {"type": kind, **config},
    }


def _brand_then_model():
    return [
        _feature_def(
            "brand", "ref_select", 1,
            optionsRef={"vocabulary": "autocatalog", "level": "Make"},
        ),
        _feature_def(
            "model", "ref_select", 2,
            optionsRef={"vocabulary": "autocatalog", "level": "Model",
                        "parentFeature": "brand"},
        ),
    ]


def _register(features):
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)
    # Only the leaf under test declares anything: the conformance corpus is
    # in the index too, and a provider that answered the same schema for
    # every category would report the same defect once per category.
    register_function(
        "categories.features",
        lambda payload: {
            "category_id": payload["category_id"],
            "revision": 1,
            "features": features if str(payload["category_id"]) == "cars" else [],
        },
    )


@pytest.fixture
def cars():
    """A leaf that authors the make above the model, the way it should be."""
    from stapel_core.comm.registry import function_registry

    features = _brand_then_model()
    _register(features)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


@pytest.fixture
def vocabulary():
    """A resolver over the three models and their two makes."""
    from stapel_attributes.vocabularies import register_vocabulary_resolver

    LABELS = {
        "apple": "Apple", "samsung": "Samsung",
        "iphone-13": "iPhone 13", "iphone-14": "iPhone 14",
        "galaxy-s23": "Galaxy S23",
    }

    class _Resolver:
        def describe(self, vocabulary):
            return None

        def exists(self, vocabulary, level, code):
            return code in LABELS

        def is_child(self, vocabulary, level, code, parent_level, parent_code):
            return BRANDS.get(code) == parent_code

        def labels(self, vocabulary, level, codes):
            return {code: LABELS[code] for code in codes if code in LABELS}

    register_vocabulary_resolver(_Resolver())
    yield
    register_vocabulary_resolver(None)


def _index():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document, _feature

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=key,
                card={"title": key},
                category_id="cars",
                category_path=("cars",),
                features={
                    "brand": _feature("ref_select", [BRANDS[model]]),
                    "model": _feature("ref_select", [model]),
                },
            )
            for key, model in (
                ("1", "iphone-13"), ("2", "iphone-14"), ("3", "galaxy-s23")
            )
        ],
    )


def _search(**params):
    from stapel_search.services import search

    return search({"type": DOC_TYPE, "category": "cars", **params})


# ── (a) no parent value: present, empty, gated ───────────────────────────────


def test_a_dependent_group_is_gated_until_its_parent_is_chosen(
    conformance, cars, vocabulary
):
    _index()
    answer = _search()
    assert answer["facet_meta"]["dependent_facets"] == "staged"
    assert answer["facets"]["model"] == {}, "a gated group answers no options"
    label = answer["facet_labels"]["model"]
    assert label["depends_on"] == "brand"
    assert label["gated"] is True
    assert "parent_missing" not in label
    # The parent is untouched and says so.
    assert answer["facet_labels"]["brand"]["depends_on"] is None
    assert answer["facet_labels"]["brand"]["gated"] is False
    assert set(answer["facets"]["brand"]) == {"apple", "samsung"}
    # Not counted, and the answer says which groups were.
    assert "model" not in answer["facet_meta"]["counted"]


# ── the saving is real: no aggregation is asked for ──────────────────────────


def test_no_aggregation_is_requested_for_a_gated_group(conformance, cars, vocabulary):
    """Gating that still paid for the count would be a cosmetic rule."""
    from stapel_search.backends import get_backend

    _index()
    backend = get_backend()
    seen: list[tuple[str, ...]] = []
    original = type(backend).facets

    def spy(self, q, plan):
        seen.append(tuple(plan.slugs))
        return original(self, q, plan)

    type(backend).facets = spy
    try:
        _search()
        assert seen and "model" not in seen[-1], seen
        assert "brand" in seen[-1]
        seen.clear()
        _search(**{"f.brand": "apple"})
        assert seen and "model" in seen[-1], seen
    finally:
        type(backend).facets = original


# ── (b) parent chosen: the children of what was chosen ───────────────────────


def test_a_chosen_parent_opens_the_group_on_its_own_children(
    conformance, cars, vocabulary
):
    _index()
    answer = _search(**{"f.brand": "apple"})
    label = answer["facet_labels"]["model"]
    assert label["gated"] is False
    assert label["depends_on"] == "brand"
    assert "parent_missing" not in label
    assert set(answer["facets"]["model"]) == {"iphone-13", "iphone-14"}, (
        "the parent's filter already narrows the aggregation to its children"
    )
    assert "model" in answer["facet_meta"]["counted"]
    assert label["values"]["iphone-13"] == "iPhone 13"


# ── (c) the deep link: the filter stands, the parent is flagged ──────────────


def test_a_child_filter_without_its_parent_keeps_working_and_says_so(
    conformance, cars, vocabulary
):
    """A bookmark, or an address an older panel wrote. Dropping the filter
    would answer a wider page than the link asked for."""
    _index()
    answer = _search(**{"f.model": "iphone-13"})
    label = answer["facet_labels"]["model"]
    assert label["gated"] is False
    assert label["parent_missing"] is True
    assert label["depends_on"] == "brand"
    # Counted with its OWN filter lifted (drill-down), so the neighbours are
    # still reachable — but the RESULTS are the one the reader asked for.
    assert answer["facets"]["model"], "the group keeps its options"
    assert [item["key"] for item in answer["items"]] == ["1"]
    assert answer["count"] == 1
    assert "model" not in answer["facet_meta"]["dropped_filters"]


# ── (d) flat: the pre-0.18 answer, byte for byte ─────────────────────────────


def test_flat_mode_is_the_old_answer(conformance, cars, vocabulary):
    from django.conf import settings
    from django.test import override_settings

    _index()
    with override_settings(
        STAPEL_SEARCH={**getattr(settings, "STAPEL_SEARCH", {}), "DEPENDENT_FACETS": "flat"}
    ):
        answer = _search()
    assert answer["facet_meta"]["dependent_facets"] == "flat"
    label = answer["facet_labels"]["model"]
    assert "gated" not in label
    assert "depends_on" not in label
    assert "parent_missing" not in label
    assert set(answer["facets"]["model"]) == {"iphone-13", "iphone-14", "galaxy-s23"}
    assert "model" in answer["facet_meta"]["counted"]


# ── (e)/(f) the schema's own two defects ─────────────────────────────────────


def test_a_dependent_authored_above_its_parent_is_moved_and_warned_about(conformance):
    """W011. The answer is rescued; the schema is where it gets fixed."""
    from django.core.checks import Warning as CheckWarning

    from stapel_search.checks import check_dependent_facets
    from stapel_search.facets import facet_plan, order_dependents

    features = list(reversed(_brand_then_model()))
    assert [f["slug"] for f in features] == ["model", "brand"]
    _register(features)
    _index()
    try:
        assert facet_plan("cars").slugs == ("brand", "model"), "moved below its parent"
        assert order_dependents(["model", "brand"], {"model": "brand"}) == [
            "brand", "model"
        ]
        findings = check_dependent_facets(None)
    finally:
        from stapel_core.comm.registry import function_registry

        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)
    warnings = [f for f in findings if isinstance(f, CheckWarning)]
    assert [f.id for f in warnings] == ["stapel_search.W011"], findings
    assert "'model'" in warnings[0].msg and "'brand'" in warnings[0].msg
    assert "cars" in warnings[0].msg


def test_a_parent_that_names_nothing_is_an_error(conformance):
    """E005. Under staging that group can never be opened by anybody."""
    from django.core.checks import Error as CheckError

    from stapel_search.checks import check_dependent_facets

    features = [
        _feature_def(
            "model", "ref_select", 1,
            optionsRef={"vocabulary": "autocatalog", "level": "Model",
                        "parentFeature": "manufacturer"},
        ),
    ]
    _register(features)
    _index()
    try:
        findings = check_dependent_facets(None)
    finally:
        from stapel_core.comm.registry import function_registry

        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)
    errors = [f for f in findings if isinstance(f, CheckError)]
    assert [f.id for f in errors] == ["stapel_search.E005"], findings
    assert "'manufacturer'" in errors[0].msg


def test_an_unrecognised_mode_is_an_error_rather_than_a_silent_default():
    """E006. A typo that quietly reads as `staged` hides a panel."""
    from django.conf import settings
    from django.core.checks import Error as CheckError
    from django.test import override_settings

    from stapel_search.checks import check_dependent_facets

    with override_settings(
        STAPEL_SEARCH={**getattr(settings, "STAPEL_SEARCH", {}), "DEPENDENT_FACETS": "stage"}
    ):
        findings = check_dependent_facets(None)
    errors = [f for f in findings if isinstance(f, CheckError)]
    assert [f.id for f in errors] == ["stapel_search.E006"], findings
