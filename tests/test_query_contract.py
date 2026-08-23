"""The query contract: what is accepted, what is refused, and by which key.

A parameter the server drops without saying so is the most expensive kind
of wrong answer, because the page looks right. Every refusal here carries
an ``error.400.search_*`` key.
"""
from __future__ import annotations

import pytest

from stapel_search.errors import (
    ERR_400_BAD_CURSOR,
    ERR_400_BAD_GEO,
    ERR_400_BAD_RANGE,
    ERR_400_QUERY_TOO_LONG,
    ERR_400_SORT_NEEDS_CENTER,
    ERR_400_TOO_MANY_FACETS,
    ERR_400_TOO_MANY_RANGES,
    ERR_400_UNKNOWN_DOC_TYPE,
    ERR_400_UNKNOWN_SORT,
    ERR_400_WINDOW_EXCEEDED,
    SearchValidationError,
)
from stapel_search.query import decode_cursor, encode_cursor, parse_query
from stapel_search.registry import register_source
from stapel_search.testing import CONFORMANCE_SOURCE, DOC_TYPE

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _source():
    register_source(CONFORMANCE_SOURCE)


def _parse(**params):
    return parse_query({"type": DOC_TYPE, **params})


def _refusal(**params):
    with pytest.raises(SearchValidationError) as excinfo:
        _parse(**params)
    return excinfo.value


def test_an_unregistered_type_is_refused_by_name():
    with pytest.raises(SearchValidationError) as excinfo:
        parse_query({"type": "nope"})
    assert excinfo.value.code == ERR_400_UNKNOWN_DOC_TYPE
    assert excinfo.value.params["doc_type"] == "nope"


def test_a_missing_type_is_refused_too():
    with pytest.raises(SearchValidationError) as excinfo:
        parse_query({})
    assert excinfo.value.code == ERR_400_UNKNOWN_DOC_TYPE


def test_default_sort_follows_whether_text_was_given():
    assert _parse().sort == "newest"
    assert _parse(q="phone").sort == "relevance"


def test_popular_is_not_in_the_default_vocabulary():
    """v1 has no popularity emitter, so the sort is not offered (verdict §18.3)."""
    assert _refusal(sort="popular").code == ERR_400_UNKNOWN_SORT


def test_distance_without_a_centre_is_refused():
    assert _refusal(sort="distance").code == ERR_400_SORT_NEEDS_CENTER
    assert _parse(sort="distance", lat="49.6", lon="6.1").sort == "distance"


def test_facet_and_range_parameters_are_parsed_by_prefix():
    q = _parse(**{"f.brand": "apple", "r.year": "2015..2020"})
    assert q.facets == {"brand": ["apple"]}
    assert q.ranges[0].slug == "year"
    assert str(q.ranges[0].lower) == "2015"
    assert str(q.ranges[0].upper) == "2020"


def test_open_ended_ranges():
    assert _parse(**{"r.year": "2015.."}).ranges[0].upper is None
    assert _parse(**{"r.year": "..2015"}).ranges[0].lower is None


@pytest.mark.parametrize("raw", ["2015", "abc..def", "..", "2020..2015"])
def test_a_malformed_range_names_its_slug(raw):
    error = _refusal(**{"r.year": raw})
    assert error.code == ERR_400_BAD_RANGE
    assert error.params["slug"] == "year"


def test_too_many_facets_and_ranges_are_refused_not_truncated():
    from django.test import override_settings

    with override_settings(STAPEL_SEARCH={"MAX_FACET_FIELDS": 2}):
        params = {f"f.s{i}": "v" for i in range(3)}
        assert _refusal(**params).code == ERR_400_TOO_MANY_FACETS
    with override_settings(STAPEL_SEARCH={"MAX_RANGE_FILTERS": 1}):
        params = {f"r.s{i}": "1..2" for i in range(2)}
        assert _refusal(**params).code == ERR_400_TOO_MANY_RANGES


def test_a_long_query_is_refused():
    assert _refusal(q="x" * 500).code == ERR_400_QUERY_TOO_LONG


def test_bbox_parsing_including_the_antimeridian():
    q = _parse(bbox="-10,179,10,-179")
    assert q.geo.crosses_antimeridian is True
    assert _refusal(bbox="1,2,3").code == ERR_400_BAD_GEO
    assert _refusal(bbox="10,2,-10,3").code == ERR_400_BAD_GEO


def test_coordinates_are_validated():
    assert _refusal(lat="49.6").code == ERR_400_BAD_GEO
    assert _refusal(lat="999", lon="0").code == ERR_400_BAD_GEO
    assert _refusal(lat="49.6", lon="6.1", radius_km="-5").code == ERR_400_BAD_GEO


def test_language_narrows_only_when_it_was_typed():
    """Accept-Language picks the analyzer; it must not hide a catalogue."""
    from_header = parse_query({"type": DOC_TYPE}, accept_language="ru-RU,ru;q=0.9")
    assert from_header.language == "ru"
    assert from_header.language_filter == ""

    explicit = parse_query({"type": DOC_TYPE, "lang": "ru"})
    assert explicit.language_filter == "ru"


def test_page_size_is_clamped_not_refused():
    from django.test import override_settings

    with override_settings(STAPEL_SEARCH={"MAX_PAGE_SIZE": 50}):
        assert _parse(limit="9999").limit == 50
        assert _parse(limit="0").limit == 1
        assert _parse(limit="not-a-number").limit == 24


def test_the_cursor_round_trips_and_a_broken_one_is_refused():
    from stapel_search.dto import Cursor

    cursor = Cursor(sort_value=8.21, doc_key="1234", offset=48)
    assert decode_cursor(encode_cursor(cursor)) == cursor
    assert _refusal(anchor="not-base64!!").code == ERR_400_BAD_CURSOR


def test_deep_pagination_is_refused_not_truncated():
    """One explicit shared window is what makes the engines interchangeable."""
    from django.test import override_settings

    from stapel_search.dto import Cursor

    deep = encode_cursor(Cursor(sort_value=1, doc_key="1", offset=1000))
    with override_settings(STAPEL_SEARCH={"MAX_RESULT_WINDOW": 1000}):
        error = _refusal(anchor=deep, limit="24")
        assert error.code == ERR_400_WINDOW_EXCEEDED
        assert error.params["window"] == 1000


def test_facet_selection_vocabulary():
    from stapel_search.query import parse_facet_selection

    assert parse_facet_selection({}) is None
    assert parse_facet_selection({"facets": "on"}) is None
    assert parse_facet_selection({"facets": "off"}) == ()
    assert parse_facet_selection({"facets": "brand, color"}) == ("brand", "color")


def test_the_response_envelope_matches_anchor_pagination(conformance):
    """Same keys and same parameter names as ``AnchorPagination``.

    The cursor's CONTENTS differ (relevance is not a model field), but a
    frontend paging hook must not need a special case for search.
    """
    from stapel_search.services import search

    response = search({"type": DOC_TYPE, "limit": "2"})
    for key in ("items", "next_anchor", "prev_anchor", "has_next", "has_prev", "count"):
        assert key in response, key
    assert response["has_next"] is True
    assert response["next_anchor"]

    second = search({"type": DOC_TYPE, "limit": "2", "anchor": response["next_anchor"]})
    assert second["has_prev"] is True
    first_keys = {item["key"] for item in response["items"]}
    second_keys = {item["key"] for item in second["items"]}
    assert not first_keys & second_keys


def test_degraded_reports_what_the_engine_could_not_do(conformance):
    from stapel_search.services import search

    response = search({"type": DOC_TYPE, "q": "samsung"})
    capabilities = conformance.capabilities
    if not capabilities.typo_tolerance:
        assert "typo_tolerance" in response["degraded"]
    # `exact_total` is degraded per ANSWER, not per engine: an engine with no
    # guaranteed exact total still counts a small candidate set exactly.
    assert ("exact_total" in response["degraded"]) is not response["exact_total"]
    assert response["backend"] == conformance.backend.name


# --------------------------------------------------------------------------
# the count contract: never 0 beside items
# --------------------------------------------------------------------------


class _FakeBackend:
    """An engine that answers exactly what a test needs it to answer."""

    name = "fake"

    def __init__(self, result):
        self._result = result

    def capabilities(self):
        from stapel_search.dto import BackendCapabilities

        return BackendCapabilities(
            typo_tolerance=True,
            facet_counts=False,
            phrase_synonyms=True,
            # The engine makes no exactness guarantee; the ANSWER still may.
            exact_total=False,
            supported_scorers=frozenset(
                {"relevance", "freshness_decay", "geo_decay", "promotion_boost", "popularity"}
            ),
        )

    def query(self, q):
        return self._result

    def facets(self, q, plan):  # pragma: no cover - facet_counts is False
        raise AssertionError("facets must not be asked of an engine that cannot count")


def _answer(monkeypatch, **result_kwargs):
    from stapel_search import backends
    from stapel_search.dto import Hit, QueryResult
    from stapel_search.services import search

    hits = tuple(
        Hit(key=str(n), score=1.0 / n, sort_value=1.0 / n)
        for n in range(1, result_kwargs.pop("hit_count", 2) + 1)
    )
    result = QueryResult(hits=hits, **result_kwargs)
    monkeypatch.setattr(backends, "get_backend", lambda: _FakeBackend(result))
    return search({"type": DOC_TYPE, "q": "samsung", "sort": "relevance"})


def test_a_zero_count_beside_items_is_replaced_by_what_the_page_proves(monkeypatch, db):
    """The live defect: «Примерно 0 объявлений» printed over visible cards."""
    response = _answer(monkeypatch, total=0, exact_total=False, hit_count=3)

    assert len(response["items"]) == 3
    assert response["count"] == 3
    assert response["count_is_lower_bound"] is True
    assert response["exact_total"] is False
    assert "exact_total" in response["degraded"]


def test_the_lower_bound_counts_the_page_after_this_one(monkeypatch, db):
    """``has_next`` proves one more row exists, so the floor includes it."""
    response = _answer(monkeypatch, total=0, has_next=True, hit_count=2)

    assert response["count"] == 3
    assert response["count_is_lower_bound"] is True


def test_a_cursor_offset_counts_the_pages_already_read(monkeypatch, db):
    from stapel_search import backends
    from stapel_search.dto import Cursor, Hit, QueryResult
    from stapel_search.query import encode_cursor
    from stapel_search.services import search

    result = QueryResult(hits=(Hit(key="9", score=0.5, sort_value=0.5),), total=0)
    monkeypatch.setattr(backends, "get_backend", lambda: _FakeBackend(result))
    anchor = encode_cursor(Cursor(sort_value=0.9, doc_key="8", offset=40))

    response = search({"type": DOC_TYPE, "q": "samsung", "anchor": anchor})
    assert response["count"] == 41
    assert response["count_is_lower_bound"] is True


def test_an_unknown_count_is_null_not_zero(monkeypatch, db):
    """A backend that cannot count says so; ``null`` renders as no count."""
    response = _answer(monkeypatch, total=None, hit_count=0)

    assert response["items"] == []
    assert response["count"] is None
    assert response["count_is_lower_bound"] is False
    assert "exact_total" in response["degraded"]


def test_an_unknown_count_still_reports_the_rows_it_returned(monkeypatch, db):
    response = _answer(monkeypatch, total=None, hit_count=2)

    assert response["count"] == 2
    assert response["count_is_lower_bound"] is True


def test_a_backends_lower_bound_survives_the_floor(monkeypatch, db):
    response = _answer(monkeypatch, total=1001, total_is_lower_bound=True, hit_count=2)

    assert response["count"] == 1001
    assert response["count_is_lower_bound"] is True
    assert response["exact_total"] is False


def test_an_exact_answer_is_not_reported_as_degraded(monkeypatch, db):
    """Exactness is a property of the answer, not of the engine class."""
    response = _answer(monkeypatch, total=17, exact_total=True, hit_count=2)

    assert response["count"] == 17
    assert response["count_is_lower_bound"] is False
    assert response["exact_total"] is True
    assert "exact_total" not in response["degraded"]


def test_suggest_refuses_an_unknown_type():
    from stapel_search.services import suggest

    with pytest.raises(SearchValidationError) as excinfo:
        suggest({"type": "nope"})
    assert excinfo.value.code == ERR_400_UNKNOWN_DOC_TYPE
