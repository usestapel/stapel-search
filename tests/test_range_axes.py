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
    # Every entry is DRAWABLE: two ends, a caption, and where the axis sits
    # in the one panel it shares with the bucket lists (0.16.0). `price` is
    # the core range and opens the panel; the schema's own axes follow in
    # the schema's order (`make` is authored first and is not a number).
    assert ranges["year"] == {
        "min": 2015, "max": 2020,
        "label": "year", "label_translatable": False, "order": 2,
    }
    assert ranges["mileage"]["min"] == 40000 and ranges["mileage"]["max"] == 120000
    assert ranges["engine_volume"]["min"] == 1.4
    assert ranges["engine_volume"]["max"] == 2.5
    # The core column is bounded by the same pass, so one report covers the
    # whole rail rather than the attribute half of it — and it carries this
    # library's own caption, because no category authored the price axis.
    assert ranges["price"] == {
        "min": 9000, "max": 18000,
        "label": "search.range.price", "label_translatable": True, "order": 0,
    }
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
    year = answer["facet_meta"]["ranges"]["year"]
    assert (year["min"], year["max"]) == (2015, 2020)


def test_a_range_axis_past_the_facet_budget_is_not_offered_either(
    conformance, cars_category, settings
):
    """The budget is how wide a PANEL may be, and a picker is as wide as a list.

    Until 0.16.0 `range_candidates` was uncapped, on the reasoning that a
    bound costs one grouped aggregate for every axis at once so the budget
    that governs counting need not govern it. That is a statement about the
    server's cost. The reader's cost is the rail: a phones leaf shipped six
    wholesale measurements past a `MAX_FACET_FIELDS` of twelve, and the axes
    a client did not ask for are the ones somebody has to scroll past.

    The core range is exempt by construction — it is not in the plan's
    ordered list at all, it addresses a column every document has, and it is
    announced unconditionally.
    """
    from stapel_search.services import search

    settings.STAPEL_SEARCH = {**settings.STAPEL_SEARCH, "MAX_FACET_FIELDS": 1}
    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars"})
    assert answer["facet_meta"]["counted"] == ["make"]
    assert "year" in answer["facet_meta"]["skipped"]
    assert "year" not in answer["facet_meta"]["ranges"]
    assert answer["facet_meta"]["ranges"]["price"]["order"] == 0


def test_the_budget_ranks_a_measurement_beside_the_choices(
    conformance, cars_category, settings
):
    """Two slots on a cars leaf buy the make AND the year, in schema order.

    The point of one budget over one ordered list: the plan is cut in the
    category's own order (mandatory first, then as authored), so a
    measurement the schema puts second is not pushed below every choice.
    `order` is what lets the client put it back where the schema had it,
    since the two halves arrive in two different keys.
    """
    from stapel_search.services import search

    settings.STAPEL_SEARCH = {**settings.STAPEL_SEARCH, "MAX_FACET_FIELDS": 2}
    _index_cars()
    answer = search({"type": DOC_TYPE, "category": "cars"})

    assert answer["facet_meta"]["counted"] == ["make", "year"]
    assert answer["facet_labels"]["make"]["order"] == 1
    assert answer["facet_meta"]["ranges"]["year"]["order"] == 2
    assert "mileage" not in answer["facet_meta"]["ranges"]


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
    year = answer["facet_meta"]["ranges"]["year"]
    assert (year["min"], year["max"]) == (2016, 2021)
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
    assert list(
        search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"]
    ) == ["price"]

    call_command("search_backfill_numbers", "--type", DOC_TYPE)

    ranges = search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"]
    assert (ranges["year"]["min"], ranges["year"]["max"]) == (2015, 2020)
    assert (ranges["mileage"]["min"], ranges["mileage"]["max"]) == (40000, 120000)
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


# --------------------------------------------------------------------------
# 0.16.0 — a range is an axis a reader can NAME, or it is not offered
# --------------------------------------------------------------------------
#
# The live symptom (2026-09-04, a classified stand): the chip row over a cars
# leaf read `doors`, `kilometrage`, `engine_volume`. Every one of those axes
# had a name in the category that authored it, and the answer shipped two
# numbers and nothing else — so every client that had no leaf schema in hand,
# which is every client on a branch page or a text query, printed the storage
# slug at a buyer. The same page also shipped six wholesale measurements the
# facet-group coverage rule would have removed on sight.


def _tuned(**overrides):
    from django.conf import settings
    from django.test import override_settings

    return override_settings(
        STAPEL_SEARCH={**getattr(settings, "STAPEL_SEARCH", {}), **overrides}
    )


def _register(features):
    from stapel_core.comm import register_function

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)


@pytest.fixture
def named_cars_category():
    """A cars leaf whose measurements are named and carry units."""
    from stapel_core.comm.registry import function_registry

    features = [
        _feature("make", "ref_select", optionsRef={"vocabulary": "cars", "level": "Make"}),
        _feature("year", "int"),
        _feature("mileage", "int", postfix="км"),
        _feature("weight", "convertible_unit", unitType="weight", unit_m="kg"),
    ]
    features[0]["name"] = "Марка"
    features[1]["name"] = "Год выпуска"
    features[2]["name"] = "Пробег"
    features[3]["name"] = "Вес"
    _register(features)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def _index_named_cars():
    from stapel_search.services import index_documents

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
                    "make": {"type": "ref_select", "value": ["toyota"]},
                    "year": {"type": "int", "value": int(year)},
                    "mileage": {"type": "int", "value": int(km)},
                    "weight": {"type": "convertible_unit", "value": weight},
                },
                price_base=Decimal("10000"),
            )
            for key, year, km, weight in (
                ("n1", "2015", "120000", 1400),
                ("n2", "2018", "80000", 1550),
            )
        ],
    )


def test_every_range_carries_the_caption_its_feature_already_had(
    conformance, named_cars_category
):
    """Resolved from the SAME source the facet group's heading comes from.

    A group and a range are two ways of narrowing one authored feature, and
    nothing about the axis being numeric changes who named it. So this is
    `plan.group_labels` — the category's own `FeatureDef.name` — and not a
    second naming path that can disagree with the one above the buckets.
    """
    from stapel_search.services import search

    _index_named_cars()
    ranges = search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"]

    assert ranges["year"]["label"] == "Год выпуска"
    assert ranges["mileage"]["label"] == "Пробег"
    # `translate: none` on these definitions, so the caption is literal text
    # and a client must not run it through a catalogue.
    assert ranges["year"]["label_translatable"] is False


def test_a_measurement_says_what_it_is_measured_in(conformance, named_cars_category):
    """«40 000 … 120 000» of what? The unit is half of a numeric caption."""
    from stapel_search.services import search

    _index_named_cars()
    ranges = search({"type": DOC_TYPE, "category": "cars"})["facet_meta"]["ranges"]

    assert ranges["mileage"]["unit"] == "км"
    # A `convertible_unit` is STORED in the family's base unit, so the base
    # unit is what the bounds are in — naming `unit_m` would label a number
    # in kilograms as something else the day a family's base is not the
    # metric input unit. The key shape is the catalogue's own.
    assert ranges["weight"]["unit"] == "feature.unit.kg.name"
    # An axis whose definition names no unit omits the KEY rather than
    # sending "", so a client branches on the value and not on the shape.
    assert "unit" not in ranges["year"]
    # A price's unit is the corpus's base currency — a property of each
    # document, not of the axis.
    assert "unit" not in ranges["price"]


def test_an_unnameable_range_is_withheld_and_says_so(conformance):
    """The `doors` / `kilometrage` chip row, closed at the source.

    A definition that carries no name yields NOTHING from the fold — it is
    not turned into a caption invented out of the slug — so this is the one
    case where the answer has bounds and nothing to write above them. It is
    withheld rather than shipped bare: an axis a reader cannot name is a
    control whose meaning has to be guessed from the numbers inside it.
    """
    from stapel_core.comm.registry import function_registry
    from stapel_search.services import index_documents, search

    nameless = _feature("doors", "int")
    nameless["name"] = ""
    named = _feature("year", "int")
    named["name"] = "Год выпуска"
    _register([named, nameless])
    try:
        index_documents(
            DOC_TYPE,
            [
                _document(
                    doc_key=key,
                    title=key,
                    card={"title": key},
                    category_id="cars",
                    category_path=("cars",),
                    features={},
                    features_search={"year": [year], "doors": [doors]},
                    price_base=Decimal("10000"),
                )
                for key, year, doors in (("d1", "2015", "4"), ("d2", "2018", "5"))
            ],
        )
        answer = search({"type": DOC_TYPE, "category": "cars"})
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)

    assert "year" in answer["facet_meta"]["ranges"]
    assert "doors" not in answer["facet_meta"]["ranges"]
    assert {
        "slug": "doors",
        "axis": "range",
        "reason": "unlabelled",
    } in answer["facet_meta"]["withheld"]


def test_a_range_over_a_handful_of_the_page_is_withheld_for_coverage(conformance):
    """The six wholesale axes, measured by the rule the groups already use.

    «Вес (Для Доставки), кг» over three of fifty-two listings is a slider
    that narrows nothing and takes exactly as much of the rail as a real one.
    The floor is `FACET_MIN_COVERAGE` and the numerator is the documents that
    carry a number on the axis, which is why the engine has to report it.
    """
    from stapel_core.comm.registry import function_registry
    from stapel_search.services import index_documents, search

    common = _feature("year", "int")
    common["name"] = "Год выпуска"
    sparse = _feature("weight_for_delivery", "int", postfix="кг")
    sparse["name"] = "Вес (Для Доставки)"
    _register([common, sparse])
    try:
        docs = [
            _document(
                doc_key=f"w{index}",
                title=f"w{index}",
                card={"title": f"w{index}"},
                category_id="cars",
                category_path=("cars",),
                features={},
                features_search=(
                    {"year": ["2015"], "weight_for_delivery": ["12"]}
                    if index == 0
                    else {"year": ["2016"]}
                ),
                price_base=Decimal("10000"),
            )
            for index in range(10)
        ]
        index_documents(DOC_TYPE, docs)
        with _tuned(FACET_MIN_COVERAGE=0.6):
            answer = search({"type": DOC_TYPE, "category": "cars"})
        with _tuned(FACET_MIN_COVERAGE=0):
            unfloored = search({"type": DOC_TYPE, "category": "cars"})
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)

    assert "year" in answer["facet_meta"]["ranges"]
    assert "weight_for_delivery" not in answer["facet_meta"]["ranges"]
    assert {
        "slug": "weight_for_delivery",
        "axis": "range",
        "reason": "coverage",
        "coverage": 1,
        "candidates": 10,
    } in answer["facet_meta"]["withheld"]
    # The floor is a setting, and at 0 the axis comes back — a catalogue
    # whose minorities are worth offering says so.
    assert "weight_for_delivery" in unfloored["facet_meta"]["ranges"]


def test_a_range_the_reader_has_already_filtered_on_is_never_withheld(conformance):
    """Withholding it leaves the filter applied with no control to undo it."""
    from stapel_core.comm.registry import function_registry
    from stapel_search.services import index_documents, search

    common = _feature("year", "int")
    common["name"] = "Год выпуска"
    sparse = _feature("weight_for_delivery", "int")
    sparse["name"] = "Вес (Для Доставки)"
    _register([common, sparse])
    try:
        index_documents(
            DOC_TYPE,
            [
                _document(
                    doc_key=f"f{index}",
                    title=f"f{index}",
                    card={"title": f"f{index}"},
                    category_id="cars",
                    category_path=("cars",),
                    features={},
                    features_search=(
                        {"year": ["2015"], "weight_for_delivery": ["12"]}
                        if index == 0
                        else {"year": ["2016"]}
                    ),
                    price_base=Decimal("10000"),
                )
                for index in range(10)
            ],
        )
        with _tuned(FACET_MIN_COVERAGE=0.6):
            answer = search(
                {
                    "type": DOC_TYPE,
                    "category": "cars",
                    "r.weight_for_delivery": "0..100",
                }
            )
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)

    assert "weight_for_delivery" in answer["facet_meta"]["ranges"]
    assert [
        row for row in answer["facet_meta"]["withheld"] if row["axis"] == "range"
    ] == []
