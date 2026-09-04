"""The panel's headings, the bare category id, and the bucket cap.

Three defects of one browse page, found on a live classified stand
(2026-09-04) and closed together in 0.14.2:

- a facet group shipped its buckets and no NAME, so a panel with no schema
  of its own printed the raw slug above them. On a car board the make group
  was present, counted and unreadable — the reader could not tell which
  filter was the make;
- ``category=166`` answered ``count: 0`` with an empty panel while
  ``category=141/151/166`` answered the same node's listings. Every link
  built from a node ID rather than from a rendered path landed on an empty
  page at HTTP 200;
- a vocabulary-backed group was cut at 200 buckets by a hardcoded SQL
  ``LIMIT``. A make dictionary holds 418 terms, so 218 of them did not
  exist as far as the answer was concerned, and no field said so.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


@pytest.fixture
def named_category():
    """A schema whose three axes differ in what they say about their names."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        # A translation KEY: `translate: all` means the catalogue resolves it.
        {
            "id": 1, "slug": "condition", "name": "feature.condition",
            "mandatory": True, "show_at_title": False, "show_as_badge": True,
            "translate": "all",
            "config": {
                "type": "select",
                "options": [{"value": "novoe", "label": "o.new"}],
            },
        },
        # Literal text: an imported catalogue names a make group in the
        # board's own language and marks it untranslatable.
        {
            "id": 2, "slug": "make", "name": "Марка", "mandatory": False,
            "show_at_title": True, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "ref_select",
                "optionsRef": {"vocabulary": "autocatalog", "level": "Make"},
            },
        },
        # A definition with no name at all — the case the answer must be able
        # to STATE rather than paper over.
        {
            "id": 3, "slug": "colour", "name": "", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "all",
            "config": {
                "type": "select",
                "options": [{"value": "red", "label": "o.red"}],
            },
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


# --------------------------------------------------------------------------
# the group carries its own name
# --------------------------------------------------------------------------


def test_the_plan_carries_the_definition_name_and_how_to_read_it(named_category):
    from stapel_search.facets import facet_plan

    plan = facet_plan("c1")
    assert plan.group_labels["condition"] == ("feature.condition", True)
    assert plan.group_labels["make"] == ("Марка", False)
    assert "colour" not in plan.group_labels, "a nameless definition invents none"


def test_every_counted_group_ships_a_label(conformance, named_category):
    """The live defect: buckets with no heading but the slug."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "c1"})
    for slug in answer["facets"]:
        assert "label" in answer["facet_labels"][slug], slug
    assert answer["facet_labels"]["make"]["label"] == "Марка"
    assert answer["facet_labels"]["make"]["label_translatable"] is False
    assert answer["facet_labels"]["condition"]["label"] == "feature.condition"
    assert answer["facet_labels"]["condition"]["label_translatable"] is True


def test_a_ref_select_group_names_its_vocabulary(conformance, named_category):
    """A client with no leaf schema of its own — a branch page, a text
    query — can only tell a `ref_select` axis from an inline `select` by
    reading the answer. `make` points at a vocabulary and must say so;
    `condition`, an inline `select`, must say it does not."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "c1"})
    assert answer["facet_labels"]["make"]["vocabulary"] == "autocatalog"
    assert answer["facet_labels"]["make"]["level"] == "Make"
    assert answer["facet_labels"]["condition"]["vocabulary"] is None
    assert "level" not in answer["facet_labels"]["condition"]


def test_a_group_with_no_definition_says_so_rather_than_inventing_a_name(
    conformance, named_category
):
    """`facets=<slug>` counts a slug no category declares. The answer owes
    the panel the truth about it — `null`, not a title made out of the slug,
    which is the fallback a client must be able to detect and refuse."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "c1", "facets": "colour,undeclared_slug"})
    assert answer["facet_labels"]["undeclared_slug"]["label"] is None
    assert answer["facet_labels"]["undeclared_slug"]["label_translatable"] is False
    # Declared, but the definition itself carries no name: the same answer,
    # and for the same reason.
    assert answer["facet_labels"]["colour"]["label"] is None


# --------------------------------------------------------------------------
# a bare category id is the node, not a root
# --------------------------------------------------------------------------


@pytest.fixture
def tree():
    """A `categories.path` provider over one three-level branch."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    PATHS = {"141": ["141"], "151": ["141", "151"], "166": ["141", "151", "166"]}

    def provider(payload):
        ids = [str(i) for i in payload.get("category_ids") or []]
        return {i: PATHS[i] for i in ids if i in PATHS}

    register_function("categories.path", provider)
    yield PATHS
    function_registry._providers.pop("categories.path", None)
    function_registry._schemas.pop("categories.path", None)


def _index_leaf_docs():
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=key,
                title=title,
                card={"title": title},
                category_id="166",
                category_path=("141", "151", "166"),
            )
            for key, title in (("101", "Toyota Camry"), ("102", "Lada Vesta"))
        ],
    )


def test_a_bare_category_id_answers_what_its_path_answers(conformance, tree):
    """The live defect: `category=166` -> `count: 0, facets: {}` at HTTP 200,
    beside `category=141/151/166` -> the same node's listings."""
    from stapel_search.services import search

    _index_leaf_docs()
    by_path = search({"type": DOC_TYPE, "category": "141/151/166"})
    by_id = search({"type": DOC_TYPE, "category": "166"})
    assert [i["key"] for i in by_id["items"]] == [i["key"] for i in by_path["items"]]
    assert by_id["count"] == by_path["count"] == 2


def test_a_path_still_filters_by_prefix(conformance, tree):
    """Resolution is only for the one-segment case; an ancestor path keeps
    finding its descendants, which is what a branch page is."""
    from stapel_search.services import search

    _index_leaf_docs()
    assert search({"type": DOC_TYPE, "category": "141/151"})["count"] == 2
    assert search({"type": DOC_TYPE, "category": "141"})["count"] == 2


def test_an_id_the_catalogue_does_not_have_is_a_400(conformance, tree):
    """`count: 0` cannot be told apart from an empty category, so a typo in a
    link and a catalogue that lost a branch looked identical."""
    from stapel_search.errors import ERR_400_UNKNOWN_CATEGORY, SearchValidationError
    from stapel_search.services import search

    with pytest.raises(SearchValidationError) as raised:
        search({"type": DOC_TYPE, "category": "999999"})
    assert raised.value.code == ERR_400_UNKNOWN_CATEGORY
    assert raised.value.params["category"] == "999999"


def test_the_unknown_id_reaches_the_client_as_a_400(conformance, tree, api_client):
    response = api_client.get("/api/v1/query", {"type": DOC_TYPE, "category": "999999"})
    assert response.status_code == 400
    assert "search_unknown_category" in response.content.decode()


def test_an_unreachable_provider_is_not_the_callers_fault(conformance):
    """No `tree` fixture: nothing answers `categories.path`. An outage
    upstream may degrade the rollup; it may not turn a valid request into a
    400 and it may not empty the page."""
    from stapel_search.services import search

    answer = search({"type": DOC_TYPE, "category": "phones"})
    assert "category_rollup" in answer["degraded"]
    assert answer["count"] is not None


# --------------------------------------------------------------------------
# the bucket cap
# --------------------------------------------------------------------------


@pytest.fixture
def wide_dictionary_category():
    """One vocabulary-backed axis and one inline one, over the same corpus."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "make", "name": "Марка", "mandatory": False,
            "show_at_title": True, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "ref_select",
                "optionsRef": {"vocabulary": "autocatalog", "level": "Make"},
            },
        },
        {
            "id": 2, "slug": "trim", "name": "Trim", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {"type": "select", "allowCustom": True, "options": [
                {"value": "base", "label": "Base"},
            ]},
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


def _index_many_values(count: int):
    """*count* documents, each carrying its own make and its own trim."""
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=f"m{n}",
                title=f"Car {n}",
                card={"title": f"Car {n}"},
                category_id="c1",
                category_path=("c1",),
                features={
                    "make": {"type": "ref_select", "value": [f"make-{n:03d}"]},
                    "trim": {"type": "select", "value": f"trim-{n:03d}"},
                },
            )
            for n in range(count)
        ],
    )


def test_a_dictionary_group_is_capped_higher_than_an_inline_one(
    conformance, wide_dictionary_category, settings
):
    """The two caps, measured on one corpus at the same moment.

    Small numbers, because the RULE is what is under test and a 418-row
    corpus in a unit test measures the fixture. The number that has to cover
    a real dictionary is asserted separately, as the default it is.
    """
    from stapel_search.services import search

    settings.STAPEL_SEARCH = {
        **settings.STAPEL_SEARCH,
        "MAX_FACET_VALUES": 3,
        "MAX_FACET_VALUES_VOCABULARY": 9,
    }
    _index_many_values(12)
    answer = search({"type": DOC_TYPE, "category": "c1"})
    assert len(answer["facets"]["make"]) == 9, "the vocabulary-backed axis"
    assert len(answer["facets"]["trim"]) == 3, "the inline one"


def test_the_shipped_vocabulary_cap_holds_a_real_make_dictionary():
    """418 is the size of the deployed autocatalog make level. A cap below it
    hides the tail of the alphabet from a panel that filters what it was
    sent, which is the whole of the client-side dictionary control."""
    from stapel_search.conf import search_settings

    assert int(search_settings.MAX_FACET_VALUES_VOCABULARY) >= 418


def test_the_cap_keeps_the_biggest_buckets(conformance, wide_dictionary_category, settings):
    """A cap that dropped the commonest values would be worse than none."""
    from stapel_search.services import search
    from stapel_search.services import index_documents
    from stapel_search.testing import _document

    settings.STAPEL_SEARCH = {
        **settings.STAPEL_SEARCH,
        "MAX_FACET_VALUES": 2,
        "MAX_FACET_VALUES_VOCABULARY": 2,
    }
    _index_many_values(6)
    index_documents(
        DOC_TYPE,
        [
            _document(
                doc_key=f"pop{n}",
                title="Popular",
                card={"title": "Popular"},
                category_id="c1",
                category_path=("c1",),
                features={"make": {"type": "ref_select", "value": ["make-000"]}},
            )
            for n in range(3)
        ],
    )
    counts = search({"type": DOC_TYPE, "category": "c1"})["facets"]["make"]
    assert len(counts) == 2
    assert counts["make-000"] == 4, "the biggest bucket survives the cut"
