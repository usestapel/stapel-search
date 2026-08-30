"""Facet planning, the candidate cap, and the generated ranking disclosure."""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE, _document, _feature

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# facet plan
# --------------------------------------------------------------------------


@pytest.fixture
def category_schema():
    """A stub ``categories.features`` with one closed and one open slug."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "brand", "name": "Brand", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "select",
                "options": [{"value": "apple", "label": "b.apple"},
                            {"value": "samsung", "label": "b.samsung"},
                            {"value": "nokia", "label": "b.nokia"}],
            },
        },
        {
            "id": 2, "slug": "note", "name": "Note", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {"type": "string"},
        },
        {
            "id": 3, "slug": "heading", "name": "Heading", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {"type": "header"},
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


@pytest.fixture
def ref_category_schema():
    """A stub ``categories.features`` whose one facetable slug is a ref type."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "vendor", "name": "Vendor", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "ref_select",
                "optionsRef": {"vocabulary": "phones", "level": "Vendor"},
                "minSelected": 0, "maxSelected": 1, "uiStyle": "dropdown",
            },
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_the_plan_comes_from_the_category_schema(category_schema):
    from stapel_search.facets import facet_plan

    plan = facet_plan("7")
    assert plan.slugs == ("brand", "note"), "a header carries no value and is not indexed"
    assert plan.kinds["brand"] == "term"


def test_a_closed_option_set_owes_the_panel_its_zeros(category_schema):
    """The whole reason a plan exists rather than "count what showed up"."""
    from stapel_search.facets import facet_plan, fill_zero_options

    plan = facet_plan("7")
    filled = fill_zero_options({"brand": {"apple": 3}}, plan)
    assert filled["brand"] == {"apple": 3, "samsung": 0, "nokia": 0}


def test_an_open_option_set_reports_only_what_was_seen(category_schema):
    from stapel_search.facets import facet_plan, fill_zero_options

    plan = facet_plan("7")
    assert "note" not in plan.closed_options
    assert fill_zero_options({"note": {"seen": 1}}, plan)["note"] == {"seen": 1}


def test_slugs_past_the_cap_are_reported_not_dropped(category_schema):
    from django.test import override_settings

    from stapel_search.facets import facet_plan

    with override_settings(STAPEL_SEARCH={"MAX_FACET_FIELDS": 1}):
        plan = facet_plan("7")
        assert plan.slugs == ("brand",)
        assert plan.skipped == ("note",), "the panel is told what was not counted"


def test_a_missing_categories_provider_degrades_to_an_empty_plan():
    from stapel_search.facets import facet_plan

    assert facet_plan("7").slugs == ()


def test_category_path_degrades_loudly_without_a_provider():
    """No ``categories.path`` provider exists in the fleet yet (spec §19.1)."""
    from stapel_search.facets import category_path, path_degradation, reset_path_degradation

    reset_path_degradation()
    assert category_path("7") == ("7",), "exact-category filtering keeps working"
    assert "categories.path" in path_degradation()


def test_category_path_uses_the_provider_when_there_is_one():
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    from stapel_search.facets import category_path

    register_function("categories.path", lambda p: {"7": ["electronics", "phones"]})
    try:
        assert category_path("7") == ("electronics", "phones")
    finally:
        function_registry._providers.pop("categories.path", None)


# --------------------------------------------------------------------------
# facet counting: drill-down and the cap
# --------------------------------------------------------------------------


def test_drilldown_does_not_zero_the_neighbours(conformance):
    from stapel_search.dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    counts = conformance.backend.facets(
        conformance.query(facets={"brand": ["apple"]}), plan
    ).counts["brand"]
    assert counts["apple"] == 1
    assert counts["samsung"] == 1


def test_counts_below_the_cap_are_exact_and_say_so(conformance):
    from stapel_search.dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    result = conformance.backend.facets(conformance.query(), plan)
    assert result.approximate is False
    assert result.degraded == ()


def test_above_the_cap_counts_are_approximate_and_declared(conformance):
    """The cap is benchmark-calibrated, and the fallback is live from day one.

    Under 8 concurrent clients, 20k candidates already breach the 200ms
    target on the proxy rig (``tasks/search-facet-benchmark.md`` §8.3), so
    the cap is 15000 and the sampled path is not a someday feature. Forcing
    the cap to 1 here exercises the real fallback against the real corpus.
    """
    from django.test import override_settings

    from stapel_search.dto import FacetPlan

    if not conformance.capabilities.facet_counts:
        pytest.skip("backend declares facet_counts: False")
    if conformance.capabilities.exact_facet_counts:
        pytest.skip("this engine counts exactly at any size and says so")

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    with override_settings(
        STAPEL_SEARCH={
            "BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend",
            "FACET_CANDIDATE_CAP": 1,
        }
    ):
        result = conformance.backend.facets(conformance.query(), plan)
    assert result.approximate is True
    assert "exact_facet_counts" in result.degraded


def test_the_default_cap_is_the_benchmarked_number():
    from stapel_search.conf import search_settings

    assert search_settings.FACET_CANDIDATE_CAP == 15000
    assert search_settings.MAX_FACET_FIELDS == 12


def test_the_response_carries_facet_meta(conformance, category_schema):
    from stapel_search.services import search

    response = search({"type": DOC_TYPE, "category": "7", "facets": "brand"})
    meta = response["facet_meta"]
    assert meta["counted"] == ["brand"]
    assert meta["approximate"] in (True, False)
    assert "skipped" in meta


def test_facets_off_counts_nothing(conformance, category_schema):
    from stapel_search.services import search

    response = search({"type": DOC_TYPE, "category": "7", "facets": "off"})
    assert response["facets"] == {}
    assert response["facet_meta"]["counted"] == []


# --------------------------------------------------------------------------
# facet mapping registry
# --------------------------------------------------------------------------


def test_every_builtin_attribute_type_has_a_declared_mapping():
    from stapel_search.registry import BUILTIN_FACET_MAPPINGS

    try:
        from stapel_attributes.registry import get_all_type_slugs
    except ImportError:  # pragma: no cover - attributes is a hard dependency
        pytest.skip("stapel-attributes not importable")
    missing = sorted(set(get_all_type_slugs()) - set(BUILTIN_FACET_MAPPINGS))
    assert not missing, f"attribute types with no declared index semantics: {missing}"


def test_the_vocabulary_backed_types_are_declared_not_defaulted():
    """A ref type must never reach ``search.W002``'s generic branch.

    Its DAO carries ``value`` (term codes) plus a ``labels`` snapshot; codes
    are the axis, exactly as for the inline twins, so ``ref_select`` is a
    term and ``ref_hierarchical_select`` a root->leaf path.
    """
    from stapel_search.registry import (
        defaulted_type_slugs,
        get_facet_mapping,
        reset_defaulted_type_slugs,
    )

    reset_defaulted_type_slugs()
    assert get_facet_mapping("ref_select").kind == "term"
    assert get_facet_mapping("ref_hierarchical_select").kind == "path"
    assert not defaulted_type_slugs() & {"ref_select", "ref_hierarchical_select"}


def test_a_ref_dao_indexes_its_codes_not_its_labels():
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={
            "vendor": {"type": "ref_select", "value": ["apple"],
                       "labels": ["Apple"], "vocabulary": "phones", "level": "Vendor"},
            "model": {"type": "ref_hierarchical_select", "value": ["apple", "iphone-15"],
                      "labels": ["Apple", "iPhone 15"], "vocabulary": "phones",
                      "levels": ["Vendor", "Model"]},
        },
    )
    facets, terms, _numbers, _ = build_facets(doc)
    assert facets == {"vendor": ["apple"], "model": ["apple", "iphone-15"]}
    assert terms == ["vendor=apple", "model=apple", "model=apple/iphone-15"]


def test_a_vocabulary_backed_slug_plans_open(ref_category_schema):
    """§3.5: the level lives outside the schema, so there are no zeros to owe."""
    from stapel_search.facets import facet_plan, fill_zero_options

    plan = facet_plan("7")
    assert plan.kinds["vendor"] == "term"
    assert "vendor" not in plan.closed_options
    assert fill_zero_options({"vendor": {"apple": 2}}, plan)["vendor"] == {"apple": 2}


def test_hex_color_indexes_the_simple_axis_not_the_paint_code():
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={"paint": {"type": "hex_color", "simple": "red", "hex": "#ff0000",
                            "label": "colour.red"}},
    )
    facets, terms, _numbers, _ = build_facets(doc)
    assert facets == {"paint": ["red"]}
    assert terms == ["paint=red"], "a paint code and a translation key are not facet axes"


def test_a_path_slug_expands_to_every_prefix():
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={"cat": {"type": "hierarchical_select",
                          "value": ["electronics", "phones", "smart"]}},
    )
    _facets, terms, _numbers, _ = build_facets(doc)
    assert terms == [
        "cat=electronics",
        "cat=electronics/phones",
        "cat=electronics/phones/smart",
    ]


def test_numeric_types_land_in_the_side_table():
    from stapel_search.services import build_facets

    for type_slug in ("int", "float", "date", "convertible_unit"):
        doc = _document(doc_key="x", features={"n": _feature(type_slug, 42)})
        _facets, terms, numbers, _ = build_facets(doc)
        assert str(numbers["n"]) == "42", type_slug
        assert terms == [], f"{type_slug}: a range value is not a countable term"


def test_a_header_is_never_indexed():
    from stapel_search.services import build_facets

    doc = _document(doc_key="x", features={"h": {"type": "header"}})
    facets, terms, numbers, _ = build_facets(doc)
    assert (facets, terms, numbers) == ({}, [], {})


def test_an_unknown_type_takes_the_generic_branch_and_raises_a_warning():
    """Risk §17.6: a host's 11th type changes the DAO shape under us."""
    from stapel_search.checks import check_default_facet_mappings
    from stapel_search.registry import defaulted_type_slugs, reset_defaulted_type_slugs
    from stapel_search.services import build_facets

    reset_defaulted_type_slugs()
    doc = _document(doc_key="x", features={"r": _feature("rating", 5)})
    facets, terms, _numbers, _ = build_facets(doc)
    assert facets == {"r": [5]}
    assert terms == ["r=5"], "indexed, not dropped"
    assert "rating" in defaulted_type_slugs()

    findings = check_default_facet_mappings(None)
    assert findings and findings[0].id == "stapel_search.W002"
    assert "rating" in findings[0].msg


def test_long_terms_are_truncated_and_counted():
    from stapel_search.services import MAX_TERM_CHARS, build_facets

    doc = _document(doc_key="x", features={"s": _feature("string", "z" * 400)})
    _facets, terms, _numbers, truncated = build_facets(doc)
    assert truncated == 1
    assert len(terms[0]) == MAX_TERM_CHARS


# --------------------------------------------------------------------------
# ranking disclosure
# --------------------------------------------------------------------------


def test_the_disclosure_is_rendered_from_the_registry():
    from stapel_search.registry import get_scorers
    from stapel_search.scoring import ranking_disclosure

    document = ranking_disclosure("listing", backend_name="postgres")
    slugs = {entry["slug"] for entry in document["scorers"]}
    assert slugs == set(get_scorers())
    for entry in document["scorers"]:
        assert entry["description"], entry["slug"]


def test_promotion_is_disclosed_as_relevance_only():
    from stapel_search.scoring import ranking_disclosure

    document = ranking_disclosure("listing", backend_name="postgres")
    promotion = next(e for e in document["scorers"] if e["slug"] == "promotion_boost")
    assert promotion["applies_to_sorts"] == ["relevance"]
    assert any("other than 'relevance'" in note for note in document["notes"])
    assert any("promoted" in note for note in document["notes"])


def test_a_scorer_the_engine_cannot_evaluate_is_marked_inactive():
    """A disclosure that lies about which parameters apply is worse than none."""
    from stapel_search.scoring import ranking_disclosure

    document = ranking_disclosure(
        "listing", backend_name="tiny", supported={"relevance"}
    )
    by_slug = {entry["slug"]: entry for entry in document["scorers"]}
    assert by_slug["relevance"]["active"] is True
    assert by_slug["geo_decay"]["active"] is False
    assert "tiny" in by_slug["geo_decay"]["inactive_reason"]


def test_a_host_registered_scorer_appears_in_the_disclosure():
    from stapel_search.registry import Scorer, register_scorer
    from stapel_search.scoring import ranking_disclosure

    register_scorer(Scorer("seller_rating", 0.4, "search.scorer.seller_rating", {}))
    slugs = {e["slug"] for e in ranking_disclosure("listing")["scorers"]}
    assert "seller_rating" in slugs


def test_a_tombstoned_scorer_disappears_from_the_disclosure():
    from django.test import override_settings

    from stapel_search.scoring import ranking_disclosure

    with override_settings(STAPEL_SEARCH={"SCORERS": {"geo_decay": None}}):
        slugs = {e["slug"] for e in ranking_disclosure("listing")["scorers"]}
        assert "geo_decay" not in slugs
