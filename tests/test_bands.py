"""Two-band geo ranking: a LABEL over the answer, never a filter under it.

The defect this suite exists to prevent is a band that quietly becomes a
radius: an answer that came back empty "because nothing is nearby" when the
catalogue was full. Every scenario below therefore asserts the same thing
twice — the order changed, and the population did not.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from stapel_search.testing import CENTER, DOC_TYPE, _document, _feature

pytestmark = pytest.mark.django_db

QUERY = "/api/v1/query"

#: ~4.4km north of CENTER, ~11km north, and two cities far outside any band.
NEAR_4KM = (49.6516, 6.1319)
NEAR_11KM = (49.7106, 6.1319)
COLOGNE = (50.9375, 6.9603)
PARIS = (48.8566, 2.3522)


def _at(key, point, *, day, **overrides):
    from stapel_geo import geohash as gh

    lat, lon = point
    base = dict(
        doc_key=key,
        title=f"row {key}",
        body="a row that exists",
        published_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        source_updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        lat=Decimal(str(lat)),
        lon=Decimal(str(lon)),
        geohash=gh.encode(lat, lon, precision=12),
        card={"title": f"row {key}"},
    )
    base.update(overrides)
    return _document(**base)


def _corpus():
    """Three rows inside 25km, two far outside, one with no coordinates."""
    return [
        _at("n0", CENTER, day=1, features={"brand": _feature("string", "apple"),
                                           "color": _feature("select", ["red"])}),
        _at("n4", NEAR_4KM, day=2, features={"brand": _feature("string", "apple")}),
        _at("n11", NEAR_11KM, day=3, features={"brand": _feature("string", "nokia")}),
        _at("f_cologne", COLOGNE, day=4),
        _at("f_paris", PARIS, day=5),
        _document(
            doc_key="nowhere",
            title="row nowhere",
            body="a row that exists",
            published_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
            source_updated_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
            lat=None,
            lon=None,
            geohash="",
            card={"title": "row nowhere"},
        ),
    ]


ALL_KEYS = {"n0", "n4", "n11", "f_cologne", "f_paris", "nowhere"}
NEAR_KEYS = {"n0", "n4", "n11"}


@pytest.fixture
def corpus(db):
    """The band corpus, loaded against the configured backend."""
    from stapel_search.models import SearchDocument
    from stapel_search.testing import harness

    with harness() as ctx:
        SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
        ctx.reindex(documents=_corpus())
        yield ctx


def _settings(**extra):
    from django.test import override_settings

    from stapel_search._codegen_settings import default_backend

    config = {"BACKEND": default_backend()}
    config.update(extra)
    return override_settings(STAPEL_SEARCH=config)


def _get(api_client, **params):
    from stapel_search.backends import reset_backend_cache

    reset_backend_cache()
    response = api_client.get(QUERY, {"type": DOC_TYPE, **params})
    assert response.status_code == 200, response.content
    return response.json()


# --------------------------------------------------------------------------
# the covering cell set
# --------------------------------------------------------------------------


def test_the_cell_cover_contains_every_point_of_the_radius_box():
    """A cover that can drop a border row is a filter wearing a label."""
    from stapel_geo import geohash as gh

    from stapel_search.backends import _shared as shared

    cells = shared.geohash_cells(
        CENTER[0], CENTER[1], 25.0, precision=4, max_cells=64
    )
    assert cells and cells == tuple(sorted(cells))
    assert all(len(cell) == 4 for cell in cells)

    min_lat, min_lon, max_lat, max_lon = shared.radius_bbox(CENTER[0], CENTER[1], 25.0)
    for i in range(11):
        for j in range(11):
            lat = min_lat + (max_lat - min_lat) * i / 10.0
            lon = min_lon + (max_lon - min_lon) * j / 10.0
            assert gh.encode(lat, lon, precision=4) in cells


def test_a_cover_wider_than_the_cap_answers_with_nothing():
    """Empty means "use the bbox", which is the caller's documented fallback."""
    from stapel_search.backends import _shared as shared

    assert shared.geohash_cells(CENTER[0], CENTER[1], 25.0, precision=4, max_cells=1) == ()
    assert shared.geohash_cells(CENTER[0], CENTER[1], 2000.0, precision=6, max_cells=64) == ()


def test_a_row_with_no_coordinates_is_labelled_far_never_dropped():
    from stapel_search.backends import _shared as shared
    from stapel_search.dto import GeoFilter

    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=25.0)
    assert shared.band_of(None, None, near) == "far"
    assert shared.band_of(Decimal(str(CENTER[0])), Decimal(str(CENTER[1])), near) == "near"
    assert shared.band_of(Decimal(str(PARIS[0])), Decimal(str(PARIS[1])), near) == "far"
    # No centre at all: banding is inactive, and inactive is not "far".
    assert shared.band_of(None, None, None) == ""


# --------------------------------------------------------------------------
# off is off
# --------------------------------------------------------------------------


def test_bands_off_answers_exactly_what_it_answered_before(api_client, corpus):
    with _settings(GEO_BANDS=False):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1])
    assert "bands" not in body
    assert {item["key"] for item in body["items"]} == ALL_KEYS
    for item in body["items"]:
        assert set(item) == {"key", "score", "promoted", "distance_km", "card"}
        assert set(item["card"]) <= {"title"}


def test_bands_off_by_default_even_with_a_centre(api_client, corpus):
    with _settings():
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1])
    assert "bands" not in body
    assert all("band" not in item for item in body["items"])


# --------------------------------------------------------------------------
# on: an order, not a filter
# --------------------------------------------------------------------------


def test_near_rows_come_first_and_nothing_is_dropped(api_client, corpus):
    with _settings(GEO_BANDS=True):
        banded = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
        plain = _get(api_client, bands="off", limit=50)

    keys = [item["key"] for item in banded["items"]]
    bands = [item["band"] for item in banded["items"]]
    assert set(keys) == ALL_KEYS
    assert banded["count"] == plain["count"]
    assert bands == ["near"] * 3 + ["far"] * 3
    assert set(keys[:3]) == NEAR_KEYS
    assert banded["bands"] == [
        {"key": "near", "count": 3, "count_is_lower_bound": False, "radius_km": 25.0},
        {"key": "far", "count": 3, "count_is_lower_bound": False, "radius_km": None},
    ]


def test_the_row_without_coordinates_is_in_the_far_band(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    row = next(item for item in body["items"] if item["key"] == "nowhere")
    assert row["band"] == "far"
    assert row["distance_km"] is None


def test_a_tighter_band_moves_a_row_without_removing_it(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(
            api_client, lat=CENTER[0], lon=CENTER[1], near_radius_km=5, limit=50
        )
    by_key = {item["key"]: item["band"] for item in body["items"]}
    assert set(by_key) == ALL_KEYS
    assert by_key["n0"] == "near" and by_key["n4"] == "near"
    assert by_key["n11"] == "far"


def test_bands_on_without_a_centre_is_not_an_error(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, limit=50)
    assert {item["key"] for item in body["items"]} == ALL_KEYS
    assert body["bands"] == []
    assert all(item["band"] == "" for item in body["items"])


def test_a_cover_above_the_cell_cap_still_bands_correctly(api_client, corpus):
    """The bbox fallback is a different plan for the same answer."""
    with _settings(GEO_BANDS=True, NEAR_BAND_MAX_CELLS=1):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    by_key = {item["key"]: item["band"] for item in body["items"]}
    assert set(by_key) == ALL_KEYS
    assert {k for k, band in by_key.items() if band == "near"} == NEAR_KEYS


# --------------------------------------------------------------------------
# one cursor, across the boundary
# --------------------------------------------------------------------------


def test_one_cursor_pages_straight_out_of_the_near_band(api_client, corpus):
    with _settings(GEO_BANDS=True):
        first = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
        expected = [item["key"] for item in first["items"]]

        seen: list[str] = []
        anchor = None
        for _ in range(10):
            params = {"lat": CENTER[0], "lon": CENTER[1], "limit": 2}
            if anchor:
                params["anchor"] = anchor
            page = _get(api_client, **params)
            seen.extend(item["key"] for item in page["items"])
            if not page["has_next"]:
                break
            anchor = page["next_anchor"]

    assert seen == expected, "one cursor must neither repeat nor skip a row"
    assert len(seen) == len(set(seen))


def test_a_page_straddles_the_band_boundary(api_client, corpus):
    """Three near rows, two per page: page two is one near plus one far.

    The band is a heading, and a heading does not end a page. This is the
    load-bearing case of the two-query execution — the near read comes back
    short and the remainder of the page is filled from the far band's
    beginning.
    """
    with _settings(GEO_BANDS=True):
        first = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=2)
        second = _get(
            api_client,
            lat=CENTER[0],
            lon=CENTER[1],
            limit=2,
            anchor=first["next_anchor"],
        )
    assert [item["band"] for item in first["items"]] == ["near", "near"]
    assert [item["band"] for item in second["items"]] == ["near", "far"]
    assert {item["key"] for item in first["items"]} & {
        item["key"] for item in second["items"]
    } == set()


def test_the_two_bands_add_up_to_the_unbanded_answer(api_client, corpus):
    """``count(near) + count(far) == count(unbanded)`` — the whole promise.

    This is the owner's "nothing is ever hidden by distance" in the one
    form a machine can check, and it catches the entire class: a row that
    matches neither band is a row that vanished, whatever the engine did to
    lose it.
    """
    from stapel_search.models import SearchDocument
    from stapel_geo import geohash as gh

    # The row that bites: a geohash INSIDE the near cell cover, with the
    # coordinates cleared out from under it — the state a geo service
    # outage leaves behind, since the two are maintained separately.
    SearchDocument.objects.filter(doc_type=DOC_TYPE, doc_key="n11").update(
        lat=None, lon=None, geohash=gh.encode(CENTER[0], CENTER[1], precision=12)
    )
    with _settings(GEO_BANDS=True):
        banded = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
        plain = _get(api_client, bands="off", limit=50)

    by_band = {row["key"]: row["band"] for row in banded["items"]}
    assert set(by_band) == {item["key"] for item in plain["items"]}
    assert all(band in ("near", "far") for band in by_band.values())
    assert by_band["n11"] == "far"
    summary = {row["key"]: row["count"] for row in banded["bands"]}
    assert summary["near"] + summary["far"] == plain["count"]


def test_the_far_band_predicate_survives_a_null_coordinate():
    """``NOT (<near>)`` is not the far band — ``NOT NULL`` is ``NULL``.

    A row with no coordinates makes the haversine NULL, and a plain
    negation would drop it from both bands: present in neither query, gone
    from the answer, with nothing saying so. The predicate must collapse the
    unknown to "not nearby" BEFORE negating, which is the same label
    ``band_of`` gives that row.
    """
    from stapel_search.backends.postgres import PostgresSearchBackend
    from stapel_search.dto import GeoFilter, SearchQuery

    q = SearchQuery(
        doc_type=DOC_TYPE,
        near=GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=25.0),
    )
    far_sql, _ = PostgresSearchBackend()._band_clause(q, "far")
    near_sql, _ = PostgresSearchBackend()._band_clause(q, "near")
    assert far_sql.startswith("NOT COALESCE(")
    assert near_sql.startswith("COALESCE(")


def test_the_cursor_carries_the_band_it_resumes_in():
    from stapel_search.backends import _shared as shared
    from stapel_search.query import decode_cursor, encode_cursor
    from stapel_search.dto import Cursor

    value = shared.banded_sort_value("far", 2, "2026-01-04T00:00:00+00:00")
    raw = encode_cursor(Cursor(sort_value=value, doc_key="f_cologne", offset=3))
    rank, matches, base = shared.split_sort_value(decode_cursor(raw).sort_value)
    assert (rank, matches, base) == (1, 2, "2026-01-04T00:00:00+00:00")
    # A plain cursor stays plain: bands off changes nothing about the codec.
    assert shared.split_sort_value(3.5) == (None, None, 3.5)


# --------------------------------------------------------------------------
# match_count
# --------------------------------------------------------------------------


def test_match_count_orders_within_a_band(corpus):
    from stapel_search.dto import GeoFilter, SearchQuery

    q = SearchQuery(
        doc_type=DOC_TYPE,
        sort="newest",
        limit=20,
        near=GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=25.0),
        signals=(("brand", "apple"), ("color", "red")),
    )
    result = corpus.backend.query(q)
    near = [hit for hit in result.hits if hit.band == "near"]
    assert [hit.key for hit in near] == ["n0", "n4", "n11"]
    assert [hit.match_count for hit in near] == [2, 1, 0]
    # And still nothing was dropped by either the band or the signals.
    assert {hit.key for hit in result.hits} == ALL_KEYS


def test_match_count_is_absent_without_signals(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    assert all("match_count" not in item for item in body["items"])


# --------------------------------------------------------------------------
# the card is coarse; the distance is not
# --------------------------------------------------------------------------


def test_the_card_carries_an_area_and_the_hit_carries_the_exact_distance(
    api_client, corpus
):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    row = next(item for item in body["items"] if item["key"] == "n4")
    card = row["card"]
    assert card["lat"] == round(NEAR_4KM[0], 2)
    assert card["lon"] == round(NEAR_4KM[1], 2)
    assert card["geo_precision_km"] > 0
    # The true coordinate is NOT in the card, and the distance is still exact.
    assert card["lat"] != NEAR_4KM[0]
    assert 4.0 < row["distance_km"] < 5.0


def test_a_card_without_coordinates_gains_none(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    card = next(item for item in body["items"] if item["key"] == "nowhere")["card"]
    assert "lat" not in card and "lon" not in card


# --------------------------------------------------------------------------
# the parameters
# --------------------------------------------------------------------------


def test_the_band_parameters_parse(corpus):
    from stapel_search.query import parse_query

    with _settings(GEO_BANDS=True):
        q = parse_query({"type": DOC_TYPE, "lat": "49.6", "lon": "6.1"})
        assert q.near is not None and q.near.radius_km == 25.0
        q = parse_query(
            {"type": DOC_TYPE, "lat": "49.6", "lon": "6.1", "near_radius_km": "7"}
        )
        assert q.near.radius_km == 7.0
        assert parse_query({"type": DOC_TYPE, "lat": "49.6", "lon": "6.1",
                            "bands": "off"}).near is None
    with _settings(GEO_BANDS=False):
        assert parse_query({"type": DOC_TYPE, "lat": "49.6", "lon": "6.1"}).near is None
        assert parse_query({"type": DOC_TYPE, "lat": "49.6", "lon": "6.1",
                            "bands": "on"}).near is not None


def test_the_band_never_narrows_while_radius_km_still_does(corpus):
    """The two live side by side: one labels, the other excludes."""
    from stapel_search.query import parse_query

    with _settings(GEO_BANDS=True):
        q = parse_query(
            {"type": DOC_TYPE, "lat": CENTER[0], "lon": CENTER[1], "radius_km": "5"}
        )
    result = corpus.backend.query(q)
    assert {hit.key for hit in result.hits} == {"n0", "n4"}
    assert all(hit.band == "near" for hit in result.hits)


# --------------------------------------------------------------------------
# query understanding, as the read path wires it
#
# `understanding.extract` has its own suite; these cover the WIRING that
# lives in the files this change owns — the `qu` switch, the merge into
# SearchQuery, and the `query_understanding` block. They sit here rather
# than in a file of their own because `signals` and `match_count` are the
# same seam the bands use.
# --------------------------------------------------------------------------


@pytest.fixture
def brand_schema():
    """A ``categories.features`` whose `brand` is a CLOSED option set."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    features = [
        {
            "id": 1, "slug": "brand", "name": "Brand", "mandatory": False,
            "show_at_title": False, "show_as_badge": False, "translate": "none",
            "config": {
                "type": "select",
                "options": [{"value": "apple", "label": "b.apple"},
                            {"value": "nokia", "label": "b.nokia"}],
            },
        },
    ]

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": features}

    register_function("categories.features", provider)
    yield features
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


UNDERSTOOD = {"type": DOC_TYPE, "category": "electronics/phones", "q": "apple",
              "limit": 50}


def test_a_query_word_becomes_a_filter_and_says_so(api_client, corpus, brand_schema):
    with _settings(QUERY_UNDERSTANDING=True):
        body = _get(api_client, **{k: v for k, v in UNDERSTOOD.items() if k != "type"})
    assert {item["key"] for item in body["items"]} == {"n0", "n4"}
    understanding = body["query_understanding"]
    assert [(f["slug"], f["value"], f["applied"]) for f in understanding["filters"]] == [
        ("brand", "apple", True)
    ]
    assert understanding["filters"][0]["param"] == "f.brand=apple"
    # Every word became a filter, so the residual is empty and the answer is
    # the chips' set — `count` counts exactly what the chips describe.
    assert understanding["residual"] == ""
    assert body["count"] == 2
    # And the signals rank the rows even where they did not filter.
    assert all(item["match_count"] == 1 for item in body["items"])


def test_qu_off_leaves_the_words_alone(api_client, corpus, brand_schema):
    with _settings(QUERY_UNDERSTANDING=True):
        body = _get(api_client, qu="off", **{k: v for k, v in UNDERSTOOD.items()
                                             if k != "type"})
    assert "query_understanding" not in body
    assert all("match_count" not in item for item in body["items"])
    # `apple` is now only text, and no document's text says it.
    assert body["items"] == []


def test_the_flag_off_adds_no_key_at_all(api_client, corpus, brand_schema):
    with _settings(QUERY_UNDERSTANDING=False):
        body = _get(api_client, **{k: v for k, v in UNDERSTOOD.items() if k != "type"})
    assert "query_understanding" not in body
    assert all("match_count" not in item for item in body["items"])


def test_an_explicit_facet_beats_an_extracted_one(api_client, corpus, brand_schema):
    """The person clicked. A word in the box does not overrule a click."""
    params = {k: v for k, v in UNDERSTOOD.items() if k != "type"}
    with _settings(QUERY_UNDERSTANDING=True):
        body = _get(api_client, **params, **{"f.brand": "nokia"})
    assert {item["key"] for item in body["items"]} == {"n11"}
    extracted = body["query_understanding"]["filters"][0]
    # Reported honestly as NOT applied: a chip that narrowed nothing must
    # not render as one that did.
    assert extracted["applied"] is False


@pytest.mark.parametrize("bad", ["maybe", "1"])
def test_an_unknown_bands_value_is_refused(bad, corpus):
    from stapel_search.errors import SearchValidationError
    from stapel_search.query import parse_query

    with pytest.raises(SearchValidationError):
        parse_query({"type": DOC_TYPE, "bands": bad})
