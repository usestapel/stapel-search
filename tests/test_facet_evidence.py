"""A plan the RESULT SET justifies, not one the queried category owns.

D175, measured by a UX walker on a live classified stand (2026-09-03) and
then re-measured against that stand's own API:

    surface                       listings   facet groups
    /c/mobilnye-telefony (leaf)         46             12
    /c/telefony          (branch)       46              0
    /c/elektronika       (root)         52              0
    /s?q=iPhone          (text)         14              0

The cause is structural and it is entirely in the PLAN. ``facet_plan``
asks ``categories.features`` for ONE category; that Function resolves a
category's own features plus the ones it inherits from its ANCESTORS. A
branch therefore owns nothing (``telefony`` declares 0 features on the
stand, ``elektronika`` 0), and a text query carries no category at all,
so ``facet_plan(None)`` folds an empty feature list.

Nothing downstream is broken. Asked to count explicitly, the stand's own
server answers both surfaces perfectly:

    ?category=32/149&facets=vendor      -> apple 13, samsung 10, xiaomi 9,
                                          realme 6, google 3, honor 3
    ?q=iPhone&facets=vendor             -> apple 13

which is what makes «Для этого поиска фильтров нет» a lie rather than a
shortfall: the engine had the answer and was never asked for it.

The naive repair — union the subtree's schemas — is worse than the defect.
Measured on the same stand: ``telefony``'s subtree is 27 categories
declaring 83 distinct feature definitions, of which exactly ONE category
holds any listing; ``elektronika``'s is 210 categories and 439 definitions,
of which SEVEN hold a listing and one of those holds 88.5% of them. A plan
drawn from the union spends a 12-slug budget on axes that describe nothing,
which is the «Вес/Длина/Высота (Для Доставки)» shape all over again.

So the plan is drawn from the categories the CANDIDATE SET actually
contains, weighted by how many documents each of them holds.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


def _select(slug, *values, **flags):
    return {
        "id": 0, "slug": slug, "name": slug, "translate": "none",
        "mandatory": False, "show_at_title": False, "show_as_badge": False,
        **flags,
        "config": {
            "type": "select",
            "options": [{"value": value, "label": value} for value in values],
            "allowCustom": True,
        },
    }


#: What each category declares. ``branch`` owns nothing, exactly as
#: ``telefony`` and ``elektronika`` do on the stand.
CATALOGUE = {
    "branch": [],
    "phones": [_select("vendor", "apple", "samsung"), _select("memory", "64", "128")],
    "laptops": [_select("cpu", "intel", "amd"), _select("screen", "13", "15")],
}


@pytest.fixture
def catalogue():
    """``categories.features`` for a two-leaf branch."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def provider(payload):
        category_id = str(payload["category_id"])
        return {
            "category_id": category_id,
            "revision": 1,
            "features": CATALOGUE.get(category_id, []),
        }

    register_function("categories.features", provider)
    yield CATALOGUE
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def tuned(**overrides):
    """`override_settings` REPLACES a dict setting; the harness lives in it."""
    from django.conf import settings
    from django.test import override_settings

    return override_settings(
        STAPEL_SEARCH={**getattr(settings, "STAPEL_SEARCH", {}), **overrides}
    )


@pytest.fixture
def branch_corpus(conformance):
    """Eight phones and one laptop, both leaves under one branch."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    # Only these nine: the shipped conformance corpus files documents under
    # `electronics/phones` too, and this test is about counting.
    conformance.backend.clear(DOC_TYPE)
    SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()

    docs = [
        _document(
            doc_key=f"p{index}",
            title=f"Телефон {index}",
            card={"title": f"Телефон {index}"},
            category_id="phones",
            category_path=("branch", "phones"),
            features={
                "vendor": {"type": "select", "value": ["apple" if index % 2 else "samsung"]},
                "memory": {"type": "select", "value": ["128"]},
            },
        )
        for index in range(8)
    ]
    docs.append(
        _document(
            doc_key="l0",
            title="Ноутбук 1",
            card={"title": "Ноутбук 1"},
            category_id="laptops",
            category_path=("branch", "laptops"),
            features={
                "cpu": {"type": "select", "value": ["intel"]},
                "screen": {"type": "select", "value": ["15"]},
            },
        )
    )
    index_documents(DOC_TYPE, docs)
    return docs


# --------------------------------------------------------------------------
# the cause, pinned
# --------------------------------------------------------------------------


def test_a_branch_owns_no_schema_of_its_own(catalogue):
    """The whole of D175 in one assertion: this is why the panel is empty."""
    from stapel_search.facets import facet_plan

    assert facet_plan("branch").slugs == ()
    assert facet_plan(None).slugs == ()
    assert facet_plan("phones").slugs == ("vendor", "memory")


# --------------------------------------------------------------------------
# a branch category
# --------------------------------------------------------------------------


def test_a_branch_offers_what_its_documents_actually_carry(catalogue, branch_corpus):
    """47 phones sharing a manufacturer is not "no filters for this search"."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "branch"})

    assert answer["count"] == 9
    assert answer["facet_meta"]["plan"] == "evidence"
    assert "vendor" in answer["facets"]
    assert answer["facets"]["vendor"] == {"apple": 4, "samsung": 4}


def test_the_answer_names_the_categories_it_drew_the_plan_from(catalogue, branch_corpus):
    """A panel cannot offer the category as a filter without the counts."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "branch"})

    assert answer["facet_meta"]["categories"] == [
        {"category": "branch/phones", "count": 8},
        {"category": "branch/laptops", "count": 1},
    ]


def test_a_slug_only_a_handful_of_documents_carry_is_withheld(catalogue, branch_corpus):
    """The «Вес/Длина/Высота (Для Доставки)» shape, closed at the source.

    ``cpu`` is a perfectly good axis — for the one laptop. Offered beside
    ``vendor`` on a page of nine, it is an axis that narrows to a single row
    and pushes a real one past the budget.
    """
    from stapel_search.services import search

    with tuned(**{"FACET_MIN_COVERAGE": 0.2}):
        answer = search({"type": DOC_TYPE, "category": "branch"})

    assert set(answer["facet_meta"]["counted"]) == {"vendor", "memory"}
    assert answer["facet_meta"]["withheld"] == [
        {"slug": "cpu", "coverage": 1, "candidates": 9},
        {"slug": "screen", "coverage": 1, "candidates": 9},
    ]
    assert "cpu" not in answer["facets"]


def test_a_withheld_slug_the_reader_has_already_chosen_is_never_taken_away(
    catalogue, branch_corpus
):
    """Withholding a group whose filter is applied leaves no way to undo it."""
    from stapel_search.services import search

    with tuned(**{"FACET_MIN_COVERAGE": 0.2}):
        answer = search({"type": DOC_TYPE, "category": "branch", "f.cpu": "intel"})

    assert "cpu" in answer["facets"]
    assert [row["slug"] for row in answer["facet_meta"]["withheld"]] == ["screen"]


def test_evidence_ranks_by_coverage_and_the_budget_goes_to_the_covered(
    catalogue, branch_corpus
):
    """Same order the two frontend surfaces already sort by (`facetCoverage`)."""
    from stapel_search.services import search

    with tuned(**{"MAX_FACET_FIELDS": 2, "FACET_MIN_COVERAGE": 0}):
        answer = search({"type": DOC_TYPE, "category": "branch"})

    assert answer["facet_meta"]["counted"] == ["vendor", "memory"]
    assert answer["facet_meta"]["skipped"] == ["cpu", "screen"]


def test_one_document_of_prediction_does_not_reorder_a_panel(monkeypatch):
    """The `/c/elektronika` shape, measured on the stand 2026-09-03.

    ``case_condition`` is declared by the 46-listing phones leaf AND by a
    1-listing laptop leaf, so it PREDICTED 47 documents against
    ``color_ref_select``'s 46 and took its budget slot. The counts, once
    taken, were 31 and 44: a category declaring an axis is not the same fact
    as its documents carrying a value for it, and 47-against-46 is not a
    reason to reorder anything. Deciles do not fix it either — 46 and 47 out
    of 52 straddle a decile boundary, which is how this was found.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    from stapel_search.facets import evidence_plan

    colour = {
        "id": 0, "slug": "colour", "name": "colour", "translate": "none",
        "mandatory": False, "show_at_title": False, "show_as_badge": False,
        "config": {
            "type": "ref_select",
            "optionsRef": {"vocabulary": "catalogue", "level": "Colour"},
        },
    }
    wear = _select("wear", "new", "used")
    schemas = {"phones": [colour, wear], "laptops": [wear]}

    def provider(payload):
        cid = str(payload["category_id"])
        return {"category_id": cid, "revision": 1, "features": schemas.get(cid, [])}

    register_function("categories.features", provider)
    try:
        plan = evidence_plan([("phones", 46), ("laptops", 1)])
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)

    # Both cover most of the page, so the band ties and `_facet_rank`
    # decides: a vocabulary-backed choice above a plain select.
    assert plan.slugs == ("colour", "wear")


# --------------------------------------------------------------------------
# a text query
# --------------------------------------------------------------------------


def test_a_text_query_plans_from_the_categories_in_its_result_set(
    catalogue, branch_corpus
):
    """The buyer who arrived through the search box — most of them.

    ``lang`` is declared, as every other Russian-text query in this suite
    declares it, because this test is about the PLAN and must not be hostage
    to dictionary resolution. Without it the answer comes back
    ``language: "en"`` — the corpus is indexed ``ru``, and the default is
    ``en`` (which is what ``search.W007`` exists to warn a deployment
    about). It passed anyway, and only because «Телефон» is its own stem
    under both analyzers: «телефоны» in the same place answered zero facet
    groups. A test that green on that coincidence is a test of the Russian
    stemmer's nominative singular, not of this module.
    """
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "q": "Телефон", "lang": "ru"})
    assert answer["language"] == "ru"

    assert answer["count"] == 8
    assert answer["facet_meta"]["plan"] == "evidence"
    assert answer["facets"]["vendor"] == {"apple": 4, "samsung": 4}
    assert answer["facet_meta"]["categories"] == [
        {"category": "branch/phones", "count": 8}
    ]


# --------------------------------------------------------------------------
# the panel must describe the page the reader is actually on
# --------------------------------------------------------------------------


def _needs_typo_arm(monkeypatch=None):
    """Skip unless the engine can widen a query — the naive walk cannot."""
    from stapel_search.backends import get_backend

    backend = get_backend()
    widens = getattr(backend, "has_trigram", None)
    if widens is None or not widens():
        pytest.skip("engine has no typo arm to widen through")
    return backend


def test_the_plan_is_drawn_from_the_arm_that_FOUND_the_results(
    catalogue, branch_corpus
):
    """Measured on real Postgres 16, and it is D175 through a second door.

    ``query()`` runs the strict arm and widens through trigram when the
    strict arm lands under ``TYPO_FALLBACK_THRESHOLD`` — and says so about
    its own totals: "Counted over the SAME arm the hits came from. Counting
    the exact arm behind a fuzzy page is how `count: 0` ends up printed over
    four visible cards." ``category_counts`` did not follow that rule. On a
    header-less «телефоны» over a ru corpus the strict aggregate answered
    `[]` while the page showed 8 results, so the plan was empty and the
    panel said there were no filters — over eight phones that all carry a
    manufacturer, which is the exact sentence this release exists to delete.
    """
    _needs_typo_arm()
    from stapel_search.services import search

    # No `lang`, deliberately: this is the header-less request the typo arm
    # is what rescues, and the panel has to describe the page it rescued.
    answer = search({"type": DOC_TYPE, "q": "телефоны"})

    assert answer["count"] == 8
    assert answer["facet_meta"]["plan"] == "evidence"
    assert answer["facets"]["vendor"] == {"apple": 4, "samsung": 4}
    assert answer["facet_meta"]["categories"] == [
        {"category": "branch/phones", "count": 8}
    ]


def test_a_widened_page_counts_its_options_over_itself(catalogue, branch_corpus):
    """The same rule, one layer down, and a bug older than this release.

    ``facets()`` hardcoded the strict arm too. On a leaf — where the plan is
    authored and never needed an aggregate — a widened query counted every
    option over a candidate set nobody was looking at, so the panel offered
    «Vendor» and «Memory» with EVERY BUCKET EMPTY above eight results. Two
    controls that cannot narrow anything is not a smaller failure than no
    controls; it is the same failure with more to click.
    """
    _needs_typo_arm()
    from stapel_search.services import search

    # MAX_FACET_FIELDS=1 so the leaf's own two slugs OVERFILL the budget and
    # no aggregate runs: this test is about `facets()` counting over the
    # right arm, and nothing about `evidence_plan` may stand in for it.
    with tuned(**{"MAX_FACET_FIELDS": 1}):
        answer = search({"type": DOC_TYPE, "q": "телефоны", "category": "branch/phones"})

    assert answer["count"] == 8
    assert answer["facet_meta"]["plan"] == "category"
    assert answer["facet_meta"]["counted"] == ["vendor"]
    assert answer["facets"]["vendor"] == {"apple": 4, "samsung": 4}


def test_a_query_whose_strict_arm_answers_is_not_widened(catalogue, branch_corpus):
    """The fallback is a net, not a new default: a page the strict arm found
    is counted by the strict arm, exactly as it was."""
    _needs_typo_arm()
    from stapel_search.backends import get_backend
    from stapel_search.dto import SearchQuery
    from stapel_search.text import normalize_query

    backend = get_backend()
    calls: list[bool] = []
    original = type(backend)._category_groups

    def spy(self, q, *, trigram, limit):
        calls.append(trigram)
        return original(self, q, trigram=trigram, limit=limit)

    type(backend)._category_groups = spy
    try:
        backend.category_counts(
            SearchQuery(
                doc_type=DOC_TYPE, language="ru", text=normalize_query("телефоны", "ru")
            ),
            limit=10,
        )
    finally:
        type(backend)._category_groups = original

    assert calls == [False], "the strict arm answered; nothing should have widened"


# --------------------------------------------------------------------------
# the leaf must not move
# --------------------------------------------------------------------------


def test_a_leaf_whose_own_schema_fills_the_budget_pays_nothing(catalogue, branch_corpus):
    """The authored plan stays authored, and costs no extra aggregate."""
    from stapel_search.services import search

    with tuned(**{"MAX_FACET_FIELDS": 2}):
        answer = search({"type": DOC_TYPE, "category": "branch/phones"})

    assert answer["facet_meta"]["plan"] == "category"
    assert answer["facet_meta"]["counted"] == ["vendor", "memory"]
    assert answer["facet_meta"]["withheld"] == []


def test_an_explicit_facets_list_is_still_the_callers_own(catalogue, branch_corpus):
    """`facets=` means "count these"; evidence does not overrule a caller."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "branch", "facets": "memory"})

    assert answer["facet_meta"]["plan"] == "category"
    assert answer["facet_meta"]["counted"] == ["memory"]


def test_a_slug_one_category_marks_non_public_is_hidden_from_all_of_them():
    """Fail-closed across the fold. A VIN that is a VIN anywhere is a VIN here.

    The evidence plan folds several categories, and `excluded` is never
    un-set, so a leaf that marks `serial` `owner` withholds it from the
    branch page even though a sibling leaf calls it public. The alternative
    — last writer wins — would enumerate an identifier with counts on the
    one surface nobody thought to check, and re-admit its `f.serial=` filter
    as an exact-match oracle.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    from stapel_search.facets import evidence_plan

    public = _select("serial", "a", "b")
    private = {**_select("serial", "a", "b"), "visibility": "owner"}
    schemas = {"open": [public], "strict": [private]}

    def provider(payload):
        cid = str(payload["category_id"])
        return {"category_id": cid, "revision": 1, "features": schemas.get(cid, [])}

    register_function("categories.features", provider)
    try:
        plan = evidence_plan([("open", 40), ("strict", 1)])
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)

    assert plan.slugs == ()
    assert plan.hidden == ("serial",)
    # And an explicit `facets=serial` cannot buy it back.
    assert evidence_plan(
        [("open", 40), ("strict", 1)], requested=("serial",)
    ).slugs == ()


# --------------------------------------------------------------------------
# the switch, and the engine that cannot answer
# --------------------------------------------------------------------------


def test_the_mechanism_is_off_by_a_setting_and_the_answer_is_what_it_was(
    catalogue, branch_corpus
):
    from stapel_search.services import search

    with tuned(**{"FACET_EVIDENCE_CATEGORIES": 0}):
        answer = search({"type": DOC_TYPE, "category": "branch"})

    assert answer["facets"] == {}
    assert answer["facet_meta"]["plan"] == "category"
    assert "facet_plan_evidence" not in answer["degraded"]


def test_an_engine_that_cannot_aggregate_categories_says_so(
    catalogue, branch_corpus, monkeypatch
):
    """Degrading loudly beats an empty panel that claims to be complete."""
    from stapel_search.backends import get_backend
    from stapel_search.services import search

    backend = get_backend()
    monkeypatch.delattr(type(backend), "category_counts", raising=False)

    answer = search({"type": DOC_TYPE, "category": "branch"})

    assert answer["facets"] == {}
    assert "facet_plan_evidence" in answer["degraded"]


# --------------------------------------------------------------------------
# the aggregate itself
# --------------------------------------------------------------------------


def test_the_aggregate_is_taken_over_the_querys_own_candidate_set(
    catalogue, branch_corpus
):
    """Not over the corpus: a category filter and a facet filter both narrow it."""
    from stapel_search.backends import get_backend
    from stapel_search.dto import SearchQuery

    backend = get_backend()

    whole = dict(backend.category_counts(SearchQuery(doc_type=DOC_TYPE), limit=20))
    assert whole == {("branch", "phones"): 8, ("branch", "laptops"): 1}

    scoped = dict(
        backend.category_counts(
            SearchQuery(doc_type=DOC_TYPE, category_path=("branch", "laptops")), limit=20
        )
    )
    assert scoped == {("branch", "laptops"): 1}


def test_the_shipped_coverage_floor_is_most_of_the_page():
    """0.6: a group has to describe MOST of what is on the page.

    The measured 5% floor was the right shape and the wrong number. It
    withheld the six 1.9% slivers of `/c/elektronika` and admitted anything
    a twentieth of a page carried — which, on the unfiltered feed of a mixed
    catalogue, is every axis of every minority in it.
    """
    from stapel_search.conf import search_settings

    assert search_settings.FACET_MIN_COVERAGE == 0.6
    assert search_settings.FACET_EVIDENCE_CATEGORIES == 24


# --------------------------------------------------------------------------
# the unfiltered feed of a mixed catalogue
# --------------------------------------------------------------------------
#
# Founder's case, 2026-09-04: `/query?type=listing` — no category, no `q` —
# over 90 listings of everything offered `memory_size`, `ram_size`,
# `camera_flaws` and `box_sealed` above a desk. Every one of those is a real
# axis of the phones MINORITY, borrowed by the evidence plan and kept by a
# 5% floor. The reader is looking at a page that is mostly not phones.

MIXED = {
    "mobile": [
        _select("condition", "new", "used"),
        _select("memory_size", "64", "128"),
        _select("ram_size", "4", "8"),
    ],
    "desks": [
        _select("condition", "new", "used"),
        _select("material", "wood", "steel"),
    ],
}


@pytest.fixture
def mixed_catalogue():
    """`categories.features` for two unrelated leaves under no shared branch."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def provider(payload):
        category_id = str(payload["category_id"])
        return {
            "category_id": category_id,
            "revision": 1,
            "features": MIXED.get(category_id, []),
        }

    register_function("categories.features", provider)
    yield MIXED
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


@pytest.fixture
def mixed_corpus(conformance):
    """Ninety listings: twenty phones, seventy desks. The founder's ratio."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    conformance.backend.clear(DOC_TYPE)
    SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()

    docs = [
        _document(
            doc_key=f"m{index}",
            title=f"Телефон {index}",
            card={"title": f"Телефон {index}"},
            category_id="mobile",
            category_path=("mobile",),
            features={
                "condition": {"type": "select", "value": ["used"]},
                "memory_size": {"type": "select", "value": ["128"]},
                "ram_size": {"type": "select", "value": ["8"]},
            },
        )
        for index in range(20)
    ] + [
        _document(
            doc_key=f"d{index}",
            title=f"Стол {index}",
            card={"title": f"Стол {index}"},
            category_id="desks",
            category_path=("desks",),
            features={
                "condition": {"type": "select", "value": ["new"]},
                "material": {"type": "select", "value": ["wood"]},
            },
        )
        for index in range(70)
    ]
    index_documents(DOC_TYPE, docs)
    return docs


def test_the_unfiltered_feed_drops_the_minoritys_axes(mixed_catalogue, mixed_corpus):
    """`memory_size` over 90 listings of everything is not a filter for them."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE})

    assert answer["facet_meta"]["candidates"] == 90
    assert set(answer["facet_meta"]["counted"]) == {"condition", "material"}
    assert "memory_size" not in answer["facets"]
    assert "ram_size" not in answer["facets"]
    assert answer["facet_meta"]["withheld"] == [
        {"slug": "memory_size", "coverage": 20, "candidates": 90},
        {"slug": "ram_size", "coverage": 20, "candidates": 90},
    ]


def test_a_group_the_whole_page_carries_survives_the_floor(mixed_catalogue, mixed_corpus):
    """`condition` is declared by both leaves and carried by all ninety rows;
    `material` by the seventy that are most of the page. Neither is borrowed
    from a minority, and the floor is not a rule against many groups."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE})

    assert answer["facets"]["condition"] == {"new": 70, "used": 20}
    assert answer["facets"]["material"] == {"wood": 70}


def test_the_leaf_itself_keeps_every_one_of_them(mixed_catalogue, mixed_corpus):
    """Scoped to the phones leaf, coverage is ~1 and nothing is withheld —
    the drill-down `category_counts` offers for the uncategorised case."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "mobile"})

    assert answer["count"] == 20
    assert set(answer["facets"]) >= {"condition", "memory_size", "ram_size"}
    assert answer["facet_meta"]["withheld"] == []


def test_the_floor_is_configurable(mixed_catalogue, mixed_corpus):
    """A catalogue whose minorities are worth offering lowers it and gets
    exactly the 0.14.2 answer back."""
    from stapel_search.services import search

    with tuned(**{"FACET_MIN_COVERAGE": 0.1}):
        answer = search({"type": DOC_TYPE})

    assert "memory_size" in answer["facets"]
    assert answer["facet_meta"]["withheld"] == []


def test_a_capped_bucket_list_is_never_withheld(mixed_catalogue, mixed_corpus):
    """Coverage is the sum of the buckets ANSWERED, so a group cut at
    MAX_FACET_VALUES reports a floor and not a measurement — and a floor
    cannot establish that a group describes too little."""
    from stapel_search.services import search

    with tuned(**{"MAX_FACET_VALUES": 1}):
        answer = search({"type": DOC_TYPE})

    assert "memory_size" in answer["facets"]
    assert answer["facet_meta"]["withheld"] == []


# --------------------------------------------------------------------------
# the queried category's own axes are not borrowed (0.14.4)
# --------------------------------------------------------------------------


@pytest.fixture
def thin_leaf(conformance):
    """One leaf with three axes, and only a third of its listings filling one.

    Three axes is fewer than `MAX_FACET_FIELDS`, so the plan is widened from
    the aggregate — and the only category the aggregate holds is this leaf
    itself. That is the case in which the exemption used to be erased.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    from stapel_search.models import SearchDocument
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    features = [
        {
            "id": 1, "slug": "make_ref_select", "name": "Марка", "mandatory": True,
            "show_at_title": True, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "ref_select",
                "optionsRef": {"vocabulary": "autocatalog", "level": "Make"},
            },
        },
        _select("condition", "new", "used"),
        _select("wheel", "left", "right"),
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)

    conformance.backend.clear(DOC_TYPE)
    SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=f"c{index}",
                title=f"Авто {index}",
                card={"title": f"Авто {index}"},
                category_id="cars",
                category_path=("cars",),
                features=(
                    {
                        "condition": {"type": "select", "value": ["used"]},
                        "make_ref_select": {"type": "ref_select", "value": ["toyota"]},
                    }
                    if index == 0
                    else {"condition": {"type": "select", "value": ["used"]}}
                ),
            )
            for index in range(3)
        ],
    )
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def test_the_queried_categorys_own_axis_is_never_withheld_for_coverage(thin_leaf):
    """The exemption 0.14.3 states, on the page that erased it.

    A leaf with fewer axes than the budget is WIDENED from an aggregate that
    contains only itself, and `evidence_plan` marks everything it ranked. So
    the leaf's own mandatory make — filled by one listing in three, coverage
    0.33 against a 0.6 floor — was withheld from its own category page, with
    real buckets behind it. A vocabulary-backed group is the one a client
    cannot enumerate from the schema, so withholding it deletes the filter.
    """
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "cars"})

    assert answer["facet_meta"]["plan"] == "evidence"
    assert answer["facet_meta"]["withheld"] == []
    assert answer["facets"]["make_ref_select"] == {"toyota": 1}
    assert answer["facet_labels"]["make_ref_select"]["label"] == "Марка"


def test_a_borrowed_axis_is_still_governed_by_the_floor(mixed_catalogue, mixed_corpus):
    """The exemption is the queried category's, and there is none here: an
    uncategorised feed borrows every axis, so 0.14.3's floor is untouched."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE})

    assert [row["slug"] for row in answer["facet_meta"]["withheld"]] == [
        "memory_size",
        "ram_size",
    ]
