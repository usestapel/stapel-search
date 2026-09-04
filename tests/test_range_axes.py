"""The numeric half of a panel: what is written, and what the answer says.

The live gap this closes (audit 2026-09-04): ``search_number`` existed and
was EMPTY on a stand holding a full corpus of cars. Every listing carried a
year, a mileage, an engine volume and a power; ``r.year=2015..2020`` matched
nothing and the rail's from/to block never rendered, because

1. the producer hands over the ``features_search`` projection rather than
   stapel-attributes DAOs, and that branch of ``build_facets`` returned
   ``numbers = {}`` unconditionally — the loss was total, not partial;
2. a vocabulary-backed or inline CHOICE whose codes are numbers (`year` on
   an imported leaf, `floor`, `doors`) was a term and only a term, so the
   axis a buyer reads as a from/to had no side-table row either;
3. nothing in the answer ever reported a MIN and a MAX, so even a corpus
   with numbers behind it could not tell a client where to put the ends of
   a slider.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from stapel_search.testing import DOC_TYPE, _document

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# the write side
# --------------------------------------------------------------------------


def test_the_lossy_projection_still_yields_numbers():
    """The defect, at its origin: values only, and the values are numbers."""
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={},
        features_search={
            "year": ["2015"],
            "mileage": [120000],
            "make": ["toyota"],
        },
    )
    _facets, terms, numbers, _ = build_facets(doc)
    assert numbers == {"year": Decimal("2015"), "mileage": Decimal("120000")}
    # The term is still written: an axis can be both, and `facets=year`
    # keeps counting buckets while `r.year` gets its bounds.
    assert "year=2015" in terms
    assert "make" not in numbers


def test_a_vocabulary_backed_year_is_a_range_too():
    """`ref_select` with numeric codes — an imported leaf's `year`."""
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={
            "year": {"type": "ref_select", "value": ["2018"]},
            "doors": {"type": "select", "value": ["4"]},
            "make": {"type": "ref_select", "value": ["toyota"]},
        },
    )
    _facets, terms, numbers, _ = build_facets(doc)
    assert numbers == {"year": Decimal("2018"), "doors": Decimal("4")}
    assert "make" not in numbers
    assert "year=2018" in terms


@pytest.mark.parametrize(
    "features",
    [
        # A bool is False == 0, which is a term and never a bound.
        {"delivery": {"type": "bool", "value": False}},
        # A multi-value axis has no ONE number to bound.
        {"sizes": {"type": "select", "value": ["38", "40"]}},
        # A root->leaf address is not a magnitude, however numeric a segment.
        {"tree": {"type": "hierarchical_select", "value": ["2020", "b"]}},
    ],
)
def test_what_is_deliberately_not_a_number(features):
    from stapel_search.services import build_facets

    _facets, _terms, numbers, _ = build_facets(_document(doc_key="x", features=features))
    assert numbers == {}


# --------------------------------------------------------------------------
# a cars leaf, end to end
# --------------------------------------------------------------------------


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


@pytest.fixture
def cars_category():
    """The imported shape: a choice chain plus four measurements."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        _feature("make", "ref_select", optionsRef={"vocabulary": "cars", "level": "Make"}),
        _feature("year", "int"),
        _feature("mileage", "int"),
        _feature("engine_volume", "float"),
        _feature("power", "int"),
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def _index_cars():
    """Three cars, indexed the way the live producer indexes them.

    Through ``features_search`` — values, no types — because that is the
    projection stapel-listings serves and the branch that wrote no numbers.
    """
    from stapel_search.services import index_documents

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=title,
                card={"title": title},
                category_id="cars",
                category_path=("cars",),
                features={},
                features_search={
                    "make": [make],
                    "year": [year],
                    "mileage": [mileage],
                    "engine_volume": [volume],
                },
                price_base=Decimal(price),
            )
            for key, title, make, year, mileage, volume, price in (
                ("c1", "Toyota Corolla", "toyota", "2015", "120000", "1.6", "9000"),
                ("c2", "Toyota Camry", "toyota", "2018", "80000", "2.5", "18000"),
                ("c3", "Kia Rio", "kia", "2020", "40000", "1.4", "14000"),
            )
        ],
    )


def test_a_car_page_reports_the_bounds_of_every_numeric_axis(conformance, cars_category):
    from stapel_search.services import search

    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars"})
    ranges = answer["facet_meta"]["ranges"]
    assert ranges["year"] == {"min": 2015, "max": 2020}
    assert ranges["mileage"] == {"min": 40000, "max": 120000}
    assert ranges["engine_volume"] == {"min": 1.4, "max": 2.5}
    # The core column is bounded by the same pass, so one report covers the
    # whole rail rather than the attribute half of it.
    assert ranges["price"] == {"min": 9000, "max": 18000}
    # An axis with no numbers behind it is ABSENT, not zero.
    assert "make" not in ranges
    assert "power" not in ranges
    assert "facet_ranges" not in answer["degraded"]


def test_r_year_filters_the_car_page(conformance, cars_category):
    from stapel_search.services import search

    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars", "r.year": "2015..2018"})
    assert {item["key"] for item in answer["items"]} == {"c1", "c2"}


def test_the_bounds_are_the_domain_a_picker_can_widen_back_to(conformance, cars_category):
    """A slider whose ends are its own selection can only ever narrow."""
    from stapel_search.services import search

    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars", "r.year": "2018..2018"})
    assert [item["key"] for item in answer["items"]] == ["c2"]
    assert answer["facet_meta"]["ranges"]["year"] == {"min": 2015, "max": 2020}


def test_a_range_axis_past_the_facet_budget_still_gets_its_bounds(
    conformance, cars_category, settings
):
    """The budget governs counting, never bounding.

    `_facet_rank` puts a choice above a measurement, so on a wide leaf the
    numeric axes are exactly the ones that fall past `MAX_FACET_FIELDS` —
    and they are exactly the ones a from/to picker is drawn for.
    """
    from stapel_search.services import search

    settings.STAPEL_SEARCH = {**settings.STAPEL_SEARCH, "MAX_FACET_FIELDS": 1}
    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars"})
    assert answer["facet_meta"]["counted"] == ["make"]
    assert "year" in answer["facet_meta"]["skipped"]
    assert answer["facet_meta"]["ranges"]["year"] == {"min": 2015, "max": 2020}


@pytest.fixture
def vocabulary_year_category():
    """The other shape of the same leaf: `year` as a vocabulary of codes."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        _feature("year", "ref_select", optionsRef={"vocabulary": "cars", "level": "Year"}),
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_a_vocabulary_year_leaf_reports_a_range(conformance, vocabulary_year_category):
    from stapel_search.services import index_documents, search

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=key,
                card={"title": key},
                category_id="cars",
                category_path=("cars",),
                features={"year": {"type": "ref_select", "value": [code]}},
            )
            for key, code in (("v1", "2016"), ("v2", "2021"))
        ],
    )
    answer = search({"type": DOC_TYPE, "category": "cars"})
    assert answer["facet_meta"]["ranges"]["year"] == {"min": 2016, "max": 2021}
    # The same value is still a COUNTED term: the catalogue calls it a
    # choice, the buyer calls it a from/to, and both are served.
    assert answer["facets"]["year"] == {"2016": 1, "2021": 1}
    assert {item["key"] for item in search(
        {"type": DOC_TYPE, "category": "cars", "r.year": "2020.."}
    )["items"]} == {"v2"}


# --------------------------------------------------------------------------
# the back-fill for documents indexed before the fix
# --------------------------------------------------------------------------


def test_the_backfill_recovers_the_numbers_of_an_old_index(conformance, cars_category):
    """A stand indexed before 0.14.7 is caught up without a source pull."""
    from django.core.management import call_command

    from stapel_search.models import SearchNumber
    from stapel_search.services import search

    _index_cars()
    # Exactly the state the audit found: documents, terms, and no numbers.
    SearchNumber.objects.all().delete()
    assert search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"] == {
        "price": {"min": 9000, "max": 18000}
    }

    call_command("search_backfill_numbers", "--type", DOC_TYPE)

    ranges = search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"]
    assert ranges["year"] == {"min": 2015, "max": 2020}
    assert ranges["mileage"] == {"min": 40000, "max": 120000}
    assert {item["key"] for item in search(
        {"type": DOC_TYPE, "category": "cars", "r.year": "2015..2018"}
    )["items"]} == {"c1", "c2"}


def test_the_backfill_refuses_to_look_useful_on_an_engine_that_ignores_it(
    conformance, cars_category, settings, capsys
):
    """A side table nothing reads is not a back-fill, it is a number."""
    from django.core.management import call_command

    settings.STAPEL_SEARCH = {
        **settings.STAPEL_SEARCH,
        "BACKEND": "stapel_search.backends.meili.MeilisearchBackend",
    }
    call_command("search_backfill_numbers", "--type", DOC_TYPE, "--dry-run")
    assert "search_rebuild instead" in capsys.readouterr().out


def test_the_backfill_writes_nothing_twice_and_can_be_asked_first(
    conformance, cars_category, capsys
):
    from django.core.management import call_command

    from stapel_search.models import SearchNumber

    _index_cars()
    before = SearchNumber.objects.count()
    call_command("search_backfill_numbers", "--type", DOC_TYPE, "--dry-run")
    assert "would write 0" in capsys.readouterr().out
    call_command("search_backfill_numbers", "--type", DOC_TYPE)
    assert SearchNumber.objects.count() == before
