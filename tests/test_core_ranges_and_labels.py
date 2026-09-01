"""The SERP axes a listing has without owning an attribute for them.

Three defects found on a client fleet's live classified stand (an end-to-end
run of 2026-08-31) and the mechanisms that close them:

- **C3** ``r.price=10000..30000`` answered ``count: 0`` for every corpus.
  A range filter resolved only against the ``SearchNumber`` side table,
  which is fed only by numeric *attribute* DAOs, so a slug naming a core
  document column found no rows — silently, at HTTP 200. Meanwhile
  ``index_schema.py`` had declared ``filter:range`` on ``price_base``
  since 0.1.0. :data:`CORE_RANGE_FIELDS` is the declaration that makes the
  claim true, and the conformance scenario is what keeps every engine
  honest about it.
- **C6** facet buckets shipped ``{value: count}`` and nothing else, so a
  panel whose host had not threaded the category schema through rendered
  ``b-u`` and ``prodayu-svoe`` at buyers. The labels were already in the
  configs ``facet_plan`` fetches to build the plan; not emitting them was
  the whole defect.
- **C4** every query with text carried ``degraded: ["phrase_synonyms",
  "phrase_synonyms"]`` — duplicated because two layers derived it from the
  same condition, and wrong because query-side synonym expansion *does*
  run on Postgres. Only a MULTI-WORD group member is actually lost.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# C3 — price is a range axis
# --------------------------------------------------------------------------


def test_price_is_declared_as_a_core_range_field():
    """The contract names the slug and the column it addresses."""
    from stapel_search.index_schema import CORE_RANGE_FIELDS, index_schema

    assert CORE_RANGE_FIELDS["price"] == "price_base"
    assert index_schema()["core_range_fields"]["price"] == "price_base"


def test_every_core_range_field_names_a_real_index_field():
    """A reserved slug pointing at a column nothing indexes is a lie."""
    from stapel_search.index_schema import CORE_RANGE_FIELDS, field_names

    known = set(field_names())
    for slug, field in CORE_RANGE_FIELDS.items():
        assert field in known, f"r.{slug} -> {field}, which is not an index field"


def test_a_core_range_slug_is_not_looked_up_in_the_side_table():
    """The split is what stops the semi-join from answering 0 for price."""
    from stapel_search.backends import _shared as shared
    from stapel_search.dto import RangeFilter

    core, attributes = shared.split_ranges(
        (
            RangeFilter(slug="price", lower=Decimal("100")),
            RangeFilter(slug="year", lower=Decimal("2015")),
        )
    )
    assert [(f, s.lower) for f, s in core] == [("price_base", Decimal("100"))]
    assert [s.slug for s in attributes] == ["year"]


def test_price_range_narrows_the_corpus(conformance):
    """The live defect, at the backend seam: 100..400 keeps only the 300."""
    from stapel_search.dto import RangeFilter

    ctx = conformance
    both = ctx.query(
        ranges=(RangeFilter(slug="price", lower=Decimal("100"), upper=Decimal("400")),)
    )
    assert set(ctx.keys(both)) == {"2"}

    upper_only = ctx.query(ranges=(RangeFilter(slug="price", upper=Decimal("100")),))
    assert set(ctx.keys(upper_only)) == {"4"}

    # A document with no price is not "cheap": it is outside every bound.
    priced = ctx.query(ranges=(RangeFilter(slug="price", lower=Decimal("0")),))
    assert "3" not in set(ctx.keys(priced))


def test_price_is_planned_as_a_core_range_axis():
    """The panel learns the axis exists from the plan, not from a hardcode."""
    from stapel_search.facets import facet_plan

    plan = facet_plan(None)
    assert "price" in plan.core_ranges


def test_the_answer_states_the_core_range_axes(conformance):
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE})
    assert answer["facet_meta"]["core_ranges"] == ["price"]


def test_a_price_range_reaches_the_service_from_the_query_string(conformance):
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "r.price": "100..400"})
    assert [item["key"] for item in answer["items"]] == ["2"]


# --------------------------------------------------------------------------
# C3 — the plan spends its budget on what the category flagged as important
# --------------------------------------------------------------------------


@pytest.fixture
def wide_category():
    """A category shaped like the live one: filler first, the axes last."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def feature(slug, type_slug, **flags):
        base = {
            "id": abs(hash(slug)) % 10000,
            "slug": slug,
            "name": slug,
            "mandatory": False,
            "show_at_title": False,
            "show_as_badge": False,
            "translate": "none",
            "config": {"type": type_slug},
        }
        base.update(flags)
        return base

    features = [
        feature("video_url", "string"),
        feature("weight_for_delivery", "int"),
        feature("length_for_delivery", "int"),
        feature("condition", "select", mandatory=True, show_as_badge=True),
        feature("vendor", "ref_select", mandatory=True, show_at_title=True),
        feature("ram_size", "ref_select", mandatory=True),
        feature("wholesale_packing", "select"),
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_the_plan_ranks_the_flags_the_category_already_carries(wide_category, settings):
    """Title beats badge beats mandatory beats the order it was authored in.

    The live symptom this closes: a phone SERP counted parcel weight,
    length, height and width while reporting Colour and RAM as *skipped* —
    the panel spent its whole budget on the delivery block because that
    block happens to be authored first.
    """
    from stapel_search.facets import facet_plan

    settings.STAPEL_SEARCH = {"MAX_FACET_FIELDS": 4}
    plan = facet_plan("c1")
    assert list(plan.slugs) == ["vendor", "condition", "ram_size", "video_url"]
    assert "weight_for_delivery" in plan.skipped


def test_a_feature_can_refuse_to_be_a_facet(wide_category):
    """The opt-out is a property of the feature, never a hand-filter here."""
    from stapel_search.facets import facet_plan

    wide_category[1]["facet"] = False
    plan = facet_plan("c1")
    assert "weight_for_delivery" not in plan.slugs
    assert "weight_for_delivery" not in plan.skipped
    assert "length_for_delivery" in plan.slugs


# --------------------------------------------------------------------------
# C6 — a bucket carries the caption its option already has
# --------------------------------------------------------------------------


@pytest.fixture
def labelled_category():
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "condition", "name": "Condition", "mandatory": True,
            "show_at_title": False, "show_as_badge": True, "translate": "all",
            "config": {
                "type": "select",
                "translatable_options": False,
                "options": [
                    {"value": "novoe", "label": "Новое"},
                    {"value": "b-u", "label": "Б/у"},
                ],
            },
        },
        {
            "id": 2, "slug": "brand", "name": "Brand", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "all",
            "config": {
                "type": "select",
                "options": [
                    {"value": "apple", "label": "b.apple"},
                    {"value": "nokia", "label": "b.nokia"},
                ],
            },
        },
        {
            "id": 3, "slug": "vendor", "name": "Vendor", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "ref_select",
                "optionsRef": {"vocabulary": "phones", "level": "Vendor"},
            },
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_the_plan_carries_the_option_captions(labelled_category):
    from stapel_search.facets import facet_plan

    plan = facet_plan("c1")
    assert plan.option_labels["condition"] == {"novoe": "Новое", "b-u": "Б/у"}
    assert plan.translatable_labels["condition"] is False
    assert plan.translatable_labels["brand"] is True


def test_a_vocabulary_backed_slug_carries_its_ADDRESS_not_its_captions(
    labelled_category,
):
    """`optionsRef` levels live outside the schema — inventing one would lie.

    The plan cannot hold the captions: a level of a real phone catalogue is
    15 844 terms and the plan does not yet know which of them this query will
    count. What it can hold is where to ask.
    """
    from stapel_search.facets import facet_plan

    plan = facet_plan("c1")
    assert "vendor" not in plan.option_labels
    assert plan.vocabulary_refs["vendor"] == ("phones", "Vendor")


@pytest.fixture
def vendor_resolver():
    """A vocabulary resolver over three terms, registered and cleaned up."""
    from stapel_attributes.vocabularies import register_vocabulary_resolver

    TERMS = {"apple": "Apple", "xiaomi": "Xiaomi", "realme": "realme"}

    class _Resolver:
        calls: list[tuple] = []

        def describe(self, vocabulary):
            return None

        def exists(self, vocabulary, level, code):
            return code in TERMS

        def is_child(self, vocabulary, level, code, parent_level, parent_code):
            return False

        def labels(self, vocabulary, level, codes):
            _Resolver.calls.append((vocabulary, level, tuple(codes)))
            return {code: TERMS[code] for code in codes if code in TERMS}

    resolver = _Resolver()
    register_vocabulary_resolver(resolver)
    yield resolver
    register_vocabulary_resolver(None)
    _Resolver.calls.clear()


def _index_vendor_docs():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=title,
                card={"title": title},
                # The category the labelled schema describes: a facet count is
                # taken over the candidate set, and a document filed elsewhere
                # is not in it.
                category_id="c1",
                category_path=("c1",),
                features={"vendor": {"type": "ref_select", "value": [code]}},
            )
            for key, title, code in (
                ("81", "Apple iPhone 13", "apple"),
                ("82", "Xiaomi Redmi Note 12", "xiaomi"),
                ("83", "realme C55", "realme"),
                ("84", "A phone from a vendor the catalogue lost", "ghost-vendor"),
            )
        ],
    )


def test_a_vocabulary_backed_facet_ships_captions_for_what_it_counted(
    conformance, labelled_category, vendor_resolver
):
    """The other half of the same panel.

    0.4.0 gave the inline selects captions and left the ref slugs bare, so one
    panel read «Состояние: Б/у» directly above «Производитель: apple» — the
    two halves disagreeing about whether a facet is readable.
    """
    from stapel_search.services import search

    _index_vendor_docs()
    answer = search({"type": DOC_TYPE, "category": "c1", "facets": "vendor"})
    assert answer["facet_labels"]["vendor"] == {
        "translatable": False,
        "values": {"apple": "Apple", "xiaomi": "Xiaomi", "realme": "realme"},
    }


def test_only_the_counted_codes_are_resolved_and_in_one_call(
    conformance, labelled_category, vendor_resolver
):
    """The reason this runs after the count, not in the plan: a level can hold
    tens of thousands of terms and a query produces at most a page of them."""
    from stapel_search.services import search

    _index_vendor_docs()
    search({"type": DOC_TYPE, "q": "iphone", "category": "c1", "facets": "vendor"})
    assert len(vendor_resolver.calls) == 1, "one batched call per slug, not one per code"
    vocabulary, level, codes = vendor_resolver.calls[0]
    assert (vocabulary, level) == ("phones", "Vendor")
    assert set(codes) == {"apple"}, "only the code this query counted"


def test_a_code_the_vocabulary_cannot_resolve_is_absent_rather_than_echoed(
    conformance, labelled_category, vendor_resolver
):
    """`{"ghost-vendor": "ghost-vendor"}` would make a map that resolved
    nothing indistinguishable from one that resolved everything to its own
    name. A reader falls back to the code on its own."""
    from stapel_search.services import search

    _index_vendor_docs()
    answer = search({"type": DOC_TYPE, "category": "c1", "facets": "vendor"})
    assert "ghost-vendor" not in answer["facet_labels"]["vendor"]["values"]
    assert answer["facets"]["vendor"]["ghost-vendor"] == 1, "still counted, just uncaptioned"


def test_no_resolver_registered_answers_exactly_as_before(
    conformance, labelled_category
):
    """A caption is an improvement on a code, never a precondition for
    answering. No `vendor_resolver` fixture here, on purpose."""
    from stapel_search.services import search

    _index_vendor_docs()
    answer = search({"type": DOC_TYPE, "category": "c1", "facets": "vendor"})
    assert "vendor" not in answer["facet_labels"]
    assert answer["facets"]["vendor"]["apple"] == 1


def test_the_answer_ships_the_captions_beside_the_counts(
    conformance, labelled_category
):
    """The live defect: «Состояние: b-u» because nothing shipped «Б/у»."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "c1"})
    assert answer["facet_labels"]["condition"] == {
        "translatable": False,
        "values": {"novoe": "Новое", "b-u": "Б/у"},
    }
    assert answer["facet_labels"]["brand"]["translatable"] is True


# --------------------------------------------------------------------------
# C4 — the notice a buyer sees is about the ANSWER, not about the engine
# --------------------------------------------------------------------------


def test_a_degradation_is_never_reported_twice(conformance):
    """Two layers deriving the same shortfall printed it twice, per query."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "q": "телефон", "lang": "ru"})
    assert len(answer["degraded"]) == len(set(answer["degraded"]))


def test_a_plain_query_loses_no_synonym_and_says_so(conformance):
    """`iphone` expands to `айфон` on Postgres. Nothing was not substituted."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "q": "iphone", "lang": "ru"})
    assert "phrase_synonyms" not in answer["degraded"]


def test_a_multiword_synonym_no_longer_raises_a_syntax_error(conformance):
    """The latent 500: «бывший в употреблении» spliced into a tsquery string.

    The shipped ``ru`` dictionary has carried that phrase since 0.1.0. It
    reached no engine only because nothing on the stand resolved a language,
    so the very fix for the language hole would have turned this group into
    ``ProgrammingError: syntax error in tsquery`` for every buyer typing
    «бу».
    """
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "q": "бу", "lang": "ru"})
    assert answer["backend"]  # it answered at all, which is the assertion


def test_a_multiword_synonym_matches_as_a_phrase(conformance):
    from stapel_search.services import index_documents
    from stapel_search.services import search
    from stapel_search.testing import _document

    if conformance.backend.name != "postgres":  # pragma: no cover
        pytest.skip("phrase adjacency is asserted on the engine that claims it")
    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key="90",
                title="Телефон",
                body="аппарат бывший в употреблении, всё работает",
                card={"title": "Телефон"},
            )
        ],
    )
    keys = [item["key"] for item in search({"type": DOC_TYPE, "q": "бу", "lang": "ru"})["items"]]
    assert "90" in keys


def test_the_engine_that_can_phrase_reports_no_synonym_shortfall(conformance):
    from stapel_search.services import search

    if not conformance.capabilities.phrase_synonyms:  # pragma: no cover
        pytest.skip("this engine does not claim phrase synonyms")
    answer = search({"type": DOC_TYPE, "q": "бу", "lang": "ru"})
    assert "phrase_synonyms" not in answer["degraded"]


def test_the_normalizer_names_the_expansions_a_plain_tsquery_cannot_hold():
    from stapel_search.text import normalize_query

    assert normalize_query("iphone", "ru").multiword_expansions == ()
    assert "бывший в употреблении" in normalize_query("бу", "ru").multiword_expansions


# --------------------------------------------------------------------------
# C4 — the dictionary that silently did not apply
# --------------------------------------------------------------------------


def test_the_answer_states_the_language_it_analyzed_with(conformance):
    """The live root cause: no `lang`, no `Accept-Language`, and the whole
    ru dictionary silently did not apply — `айфон` found 2 where `iphone`
    found 15. Nothing in the answer said which dictionary had been used."""
    from stapel_search.services import search

    assert search({"type": DOC_TYPE, "q": "iphone"})["language"] == "en"
    assert search({"type": DOC_TYPE, "q": "iphone", "lang": "ru"})["language"] == "ru"
    assert (
        search({"type": DOC_TYPE, "q": "iphone"}, accept_language="ru-RU")["language"]
        == "ru"
    )


@pytest.mark.parametrize(
    "typed,brand",
    [
        ("эпл", "apple"),
        ("ксиоми", "xiaomi"),
        ("реалми", "realme"),
        ("хонор", "honor"),
        ("оппо", "oppo"),
        ("виво", "vivo"),
        ("поко", "poco"),
        ("techno", "tecno"),
    ],
)
def test_a_russian_buyer_reaches_a_latin_brand(typed, brand):
    """Phonetic spellings no transliterator produces: `эпл` is not `epl`."""
    from stapel_search.text import normalize_query

    expansions = normalize_query(typed, "ru").terms[0]
    assert brand in expansions, f"{typed!r} must reach {brand!r}"


def test_the_shipped_dictionaries_still_lint():
    from stapel_search.text import lint_dictionary

    assert lint_dictionary("ru") == []
    assert lint_dictionary("en") == []


def test_a_default_language_without_a_dictionary_is_a_deploy_warning(settings):
    """W007 is the deploy-time half of the same defect the answer reports."""
    from stapel_search.checks import check_default_language_has_a_dictionary

    settings.STAPEL_SEARCH = {"DEFAULT_LANGUAGE": "de"}
    ids = [w.id for w in check_default_language_has_a_dictionary(None)]
    assert ids == ["stapel_search.W007"]

    settings.STAPEL_SEARCH = {"DEFAULT_LANGUAGE": "ru"}
    assert check_default_language_has_a_dictionary(None) == []


# --------------------------------------------------------------------------
# C4(b) — the cross-script reach, asserted against a CORPUS on every engine
#
# The tests above assert the normalizer's output. That is the mechanism, but
# it is not the defect: the defect was measured as a COUNT on a live board
# («айфон» 2 where `iphone` found 15), and a normalizer that expands
# correctly into an engine that ignores the expansion answers 2 just the
# same. So the same four brands are asserted again here through
# `services.search`, under the `conformance` fixture, which runs every
# scenario on each configured engine in turn — the seam that made the
# expansion invisible is exactly the seam this fixture exists to watch.
# --------------------------------------------------------------------------


#: The brands the live corpus carries that the shipped corpus does not.
_BRAND_DOCS = (
    ("91", "Xiaomi Redmi Note 12", "xiaomi"),
    ("92", "realme C55 128 ГБ", "realme"),
    ("93", "Honor X9b", "honor"),
)


def _index_brand_docs():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(doc_key=key, title=title, card={"title": title})
            for key, title, _brand in _BRAND_DOCS
        ],
    )


@pytest.mark.parametrize(
    "typed,expected_key",
    [
        ("айфон", "1"),  # Apple iPhone 13 Pro — the headline measurement
        ("эпл", "1"),
        ("ксиоми", "91"),
        ("реалми", "92"),
        ("хонор", "93"),
        ("самсунг", "2"),
    ],
)
def test_a_russian_buyer_reaches_the_latin_corpus(conformance, typed, expected_key):
    """The live defect as a count, not as a token list.

    Every one of these returned 0 or 2 on the stand while its Latin spelling
    returned 7-15. `эпл` is the one no letter table can reach —
    `transliterate("эпл")` is `epl` — which is why the group is curated data
    and not an algorithm.
    """
    from stapel_search.services import search

    _index_brand_docs()
    keys = [item["key"] for item in search({"type": DOC_TYPE, "q": typed, "lang": "ru"})["items"]]
    assert expected_key in keys, f"{typed!r} did not reach document {expected_key}"


def test_the_latin_and_the_russian_spelling_answer_the_same_corpus(conformance):
    """The asymmetry itself, since it is the asymmetry a buyer felt."""
    from stapel_search.services import search

    _index_brand_docs()
    for latin, russian in (("iphone", "айфон"), ("xiaomi", "ксиоми"), ("realme", "реалми")):
        one = {i["key"] for i in search({"type": DOC_TYPE, "q": latin, "lang": "ru"})["items"]}
        other = {i["key"] for i in search({"type": DOC_TYPE, "q": russian, "lang": "ru"})["items"]}
        assert one == other, f"{latin!r} and {russian!r} disagree: {one} vs {other}"


def test_a_word_no_group_claims_expands_to_itself_and_its_script_twin_only():
    """The negative. An expansion layer that reaches too far is worse than
    one that reaches too little: it is unfalsifiable from the outside, and a
    buyer cannot tell a synonym from a bug.
    """
    from stapel_search.text import load_dictionary, normalize_query

    expansions = normalize_query("пылесос", "ru").terms[0]
    assert expansions == ("пылесос", "pylesos")

    every_member = {m for group in load_dictionary("ru").equivalents for m in group}
    assert not set(expansions) & every_member


def test_an_unrelated_query_does_not_drag_a_brand_in(conformance):
    """The corpus half of the same negative: no hit is manufactured."""
    from stapel_search.services import search

    _index_brand_docs()
    # Cyrillic, phonetically near two curated groups ("эпл", "хонор"), in no
    # group itself, and in no document.
    assert search({"type": DOC_TYPE, "q": "эпоксидка", "lang": "ru"})["items"] == []
    assert search({"type": DOC_TYPE, "q": "хоровод", "lang": "ru"})["items"] == []
