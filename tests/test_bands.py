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
NEARBY_KEYS = {"n0", "n4", "n11"}


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


def _answer(payload):
    """The response minus what a stopwatch decides.

    ``took_ms`` is a measured duration, so asserting two answers are
    identical must not assert they took the same number of milliseconds —
    on SQLite both round to 1 and the equality holds by luck, on Postgres
    one run is 2 and the test fails for a reason that has nothing to do
    with what it is testing.
    """
    return {key: value for key, value in payload.items() if key != "took_ms"}


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


def test_a_row_with_no_coordinates_is_labelled_all_never_dropped():
    from stapel_search.backends import _shared as shared
    from stapel_search.dto import GeoFilter

    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=25.0)
    assert shared.band_of(None, None, near) == "all"
    assert shared.band_of(Decimal(str(CENTER[0])), Decimal(str(CENTER[1])), near) == "nearby"
    assert shared.band_of(Decimal(str(PARIS[0])), Decimal(str(PARIS[1])), near) == "all"
    # No centre at all: banding is inactive, and inactive is not "all".
    assert shared.band_of(None, None, None) == ""


# --------------------------------------------------------------------------
# the deploy flag is off, and off is off
# --------------------------------------------------------------------------


def test_the_flag_off_answers_exactly_what_it_answered_before(api_client, corpus):
    with _settings(GEO_BANDS=False):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1])
    assert "bands" not in body
    assert {item["key"] for item in body["items"]} == ALL_KEYS
    for item in body["items"]:
        assert set(item) == {
            "key",
            "score",
            "promoted",
            "owner_key",
            "distance_km",
            "card",
        }
        assert set(item["card"]) <= {"title"}


def test_geo_mode_is_inert_while_the_flag_is_off(api_client, corpus):
    """The wave keeps today's behaviour until the eval says otherwise.

    `geo_mode=rank` is not a way around the deploy flag: with GEO_BANDS off
    it changes nothing, and `radius_km` keeps CUTTING exactly as it always
    has — which is the half a caller would notice first if the flag leaked.
    """
    with _settings(GEO_BANDS=False):
        ranked = _get(api_client, lat=CENTER[0], lon=CENTER[1], geo_mode="rank")
        cut = _get(
            api_client, lat=CENTER[0], lon=CENTER[1], geo_mode="rank", radius_km=5
        )
        plain = _get(api_client, lat=CENTER[0], lon=CENTER[1])
    assert _answer(ranked) == _answer(plain)
    assert "bands" not in ranked
    assert {item["key"] for item in cut["items"]} == {"n0", "n4"}


def test_the_flag_off_is_the_default(api_client, corpus):
    with _settings():
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1])
    assert "bands" not in body
    assert all("band" not in item for item in body["items"])


# --------------------------------------------------------------------------
# geo_mode=rank: an order, not a gate
# --------------------------------------------------------------------------


def test_nearby_rows_come_first_and_nothing_is_dropped(api_client, corpus):
    with _settings(GEO_BANDS=True):
        banded = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
        plain = _get(api_client, geo_mode="filter", limit=50)

    keys = [item["key"] for item in banded["items"]]
    bands = [item["band"] for item in banded["items"]]
    assert set(keys) == ALL_KEYS
    assert banded["count"] == plain["count"]
    assert bands == ["nearby"] * 3 + ["all"] * 3
    assert set(keys[:3]) == NEARBY_KEYS
    assert banded["bands"] == [
        {"id": "nearby", "count": 3, "count_is_lower_bound": False, "radius_km": 25.0},
        {"id": "all", "count": 3, "count_is_lower_bound": False},
    ]


def test_the_row_without_coordinates_is_in_the_all_band(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    row = next(item for item in body["items"] if item["key"] == "nowhere")
    assert row["band"] == "all"
    assert row["distance_km"] is None


def test_radius_km_moves_a_row_between_bands_without_removing_it(api_client, corpus):
    """Under `rank` the caller's own radius is the edge, and only the edge."""
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], radius_km=5, limit=50)
    by_key = {item["key"]: item["band"] for item in body["items"]}
    assert set(by_key) == ALL_KEYS
    assert by_key["n0"] == "nearby" and by_key["n4"] == "nearby"
    assert by_key["n11"] == "all"
    assert body["bands"][0]["radius_km"] == 5.0


def test_ranking_without_a_centre_is_not_an_error(api_client, corpus):
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
    assert {k for k, band in by_key.items() if band == "nearby"} == NEARBY_KEYS


# --------------------------------------------------------------------------
# one cursor, across the boundary
# --------------------------------------------------------------------------


def test_one_cursor_pages_straight_out_of_the_nearby_band(api_client, corpus):
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
    """Three nearby rows, two per page: page two is one `nearby`, one `all`.

    The band is a heading, and a heading does not end a page. This is the
    load-bearing case of the two-query execution — the `nearby` read comes
    back short and the remainder of the page is filled from `all`'s
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
    assert [item["band"] for item in first["items"]] == ["nearby", "nearby"]
    assert [item["band"] for item in second["items"]] == ["nearby", "all"]
    assert {item["key"] for item in first["items"]} & {
        item["key"] for item in second["items"]
    } == set()


def test_the_two_bands_add_up_to_the_whole_answer(api_client, corpus):
    """``count(nearby) + count(all) == count`` — the whole promise.

    This is the owner's "nothing is ever hidden by distance" in the one
    form a machine can check, and it catches the entire class: a row that
    matches neither band is a row that vanished, whatever the engine did to
    lose it.
    """
    from stapel_search.models import SearchDocument
    from stapel_geo import geohash as gh

    # The row that bites: a geohash INSIDE the nearby cell cover, with the
    # coordinates cleared out from under it — the state a geo service
    # outage leaves behind, since the two are maintained separately.
    SearchDocument.objects.filter(doc_type=DOC_TYPE, doc_key="n11").update(
        lat=None, lon=None, geohash=gh.encode(CENTER[0], CENTER[1], precision=12)
    )
    with _settings(GEO_BANDS=True):
        banded = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
        plain = _get(api_client, geo_mode="filter", limit=50)

    by_band = {row["key"]: row["band"] for row in banded["items"]}
    assert set(by_band) == {item["key"] for item in plain["items"]}
    assert all(band in ("nearby", "all") for band in by_band.values())
    assert by_band["n11"] == "all"
    summary = {row["id"]: row["count"] for row in banded["bands"]}
    assert summary["nearby"] + summary["all"] == plain["count"] == banded["count"]


def test_the_all_band_predicate_survives_a_null_coordinate():
    """``NOT (<nearby>)`` is not the `all` band — ``NOT NULL`` is ``NULL``.

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
    all_sql, _ = PostgresSearchBackend()._band_clause(q, "all")
    nearby_sql, _ = PostgresSearchBackend()._band_clause(q, "nearby")
    assert all_sql.startswith("NOT COALESCE(")
    assert nearby_sql.startswith("COALESCE(")


def test_the_cursor_carries_the_band_it_resumes_in():
    from stapel_search.backends import _shared as shared
    from stapel_search.query import decode_cursor, encode_cursor
    from stapel_search.dto import Cursor

    value = shared.banded_sort_value("all", 2, "2026-01-04T00:00:00+00:00")
    raw = encode_cursor(Cursor(sort_value=value, doc_key="f_cologne", offset=3))
    rank, matches, base = shared.split_sort_value(decode_cursor(raw).sort_value)
    assert (rank, matches, base) == (1, 2, "2026-01-04T00:00:00+00:00")
    # A plain cursor stays plain: `filter` changes nothing about the codec.
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
    nearby = [hit for hit in result.hits if hit.band == "nearby"]
    assert [hit.key for hit in nearby] == ["n0", "n4", "n11"]
    assert [hit.match_count for hit in nearby] == [2, 1, 0]
    # And still nothing was dropped by either the band or the signals.
    assert {hit.key for hit in result.hits} == ALL_KEYS


def test_match_count_is_absent_without_signals(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    assert all("match_count" not in item for item in body["items"])


# --------------------------------------------------------------------------
# the card is coarse, and so is everything measured against it
# --------------------------------------------------------------------------


def test_the_card_carries_an_area_and_the_distance_is_on_the_same_grid(
    api_client, corpus
):
    """0.12.0 asserted the opposite of this — «the card is coarse; the
    distance is not» — and that WAS the leak: an exact distance from a
    caller-chosen centre rebuilds the pin from three requests, so a coarse
    card beside a fine distance is not a coarse answer. Both halves now come
    off one grid (``_shared``, "the public grid"), and the band the card
    feeds is unaffected: «12 км» never needed metres.
    """
    from stapel_search.backends import _shared as shared

    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    row = next(item for item in body["items"] if item["key"] == "n4")
    card = row["card"]
    precision = shared.public_precision()
    assert card["lat"] == shared.snap_to_grid(NEAR_4KM[0], precision)
    assert card["lon"] == shared.snap_to_grid(NEAR_4KM[1], precision)
    assert card["geo_precision_km"] == shared.cell_km(precision)
    assert card["lat"] != NEAR_4KM[0]

    quantum = shared.distance_quantum_km(precision)
    steps = row["distance_km"] / quantum
    assert abs(steps - round(steps)) < 1e-6, row["distance_km"]
    # Still the right neighbourhood: ~4.45km away, reported as the quantum
    # below it, which is what a card rendering «4 км» has always needed.
    assert 3.0 <= row["distance_km"] <= 4.5
    assert row["band"] == "nearby"


def test_a_card_without_coordinates_gains_none(api_client, corpus):
    with _settings(GEO_BANDS=True):
        body = _get(api_client, lat=CENTER[0], lon=CENTER[1], limit=50)
    card = next(item for item in body["items"] if item["key"] == "nowhere")["card"]
    assert "lat" not in card and "lon" not in card


# --------------------------------------------------------------------------
# the parameters
# --------------------------------------------------------------------------


def test_radius_km_lands_on_the_band_under_rank_and_on_the_filter_under_filter(
    corpus,
):
    """One parameter, two readings, and `geo_mode` is the only thing that
    decides which. The carriers stay separate so the two can never merge."""
    from stapel_search.query import parse_query

    centre = {"type": DOC_TYPE, "lat": "49.6", "lon": "6.1"}
    with _settings(GEO_BANDS=True):
        q = parse_query(centre)
        assert q.near is not None and q.near.radius_km == 25.0
        assert q.geo.radius_km is None, "rank must leave the filter unbounded"

        q = parse_query({**centre, "radius_km": "7"})
        assert q.near.radius_km == 7.0
        assert q.geo.radius_km is None

        q = parse_query({**centre, "radius_km": "7", "geo_mode": "filter"})
        assert q.near is None
        assert q.geo.radius_km == 7.0

    with _settings(GEO_BANDS=False):
        q = parse_query({**centre, "radius_km": "7", "geo_mode": "rank"})
        assert q.near is None, "the deploy flag wins over the parameter"
        assert q.geo.radius_km == 7.0


def test_filter_mode_still_excludes(corpus):
    """The hard cut is still reachable — deliberately, by asking for it."""
    from stapel_search.query import parse_query

    with _settings(GEO_BANDS=True):
        q = parse_query(
            {
                "type": DOC_TYPE,
                "lat": CENTER[0],
                "lon": CENTER[1],
                "radius_km": "5",
                "geo_mode": "filter",
            }
        )
    result = corpus.backend.query(q)
    assert {hit.key for hit in result.hits} == {"n0", "n4"}
    assert result.bands == ()


def test_total_is_the_whole_matching_count_never_the_nearby_one(api_client, corpus):
    """«Never 0 because of geo», in one assertion.

    A centre in the middle of the Atlantic with a 1km edge: the `nearby`
    band is empty, and the answer is still the whole catalogue with a
    `count` to match. A `total` that tracked the nearby band would read 0
    here — over six visible cards.
    """
    with _settings(GEO_BANDS=True):
        empty_nearby = _get(api_client, lat=0.0, lon=-30.0, radius_km=1, limit=50)
        plain = _get(api_client, geo_mode="filter", limit=50)

    assert empty_nearby["items"], "geo must never empty an answer"
    assert {item["key"] for item in empty_nearby["items"]} == ALL_KEYS
    assert empty_nearby["count"] == plain["count"] == len(ALL_KEYS)
    assert all(item["band"] == "all" for item in empty_nearby["items"])
    summary = {row["id"]: row["count"] for row in empty_nearby["bands"]}
    assert summary == {"nearby": 0, "all": len(ALL_KEYS)}


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


@pytest.mark.parametrize("bad", ["maybe", "1", "on"])
def test_an_unknown_geo_mode_is_refused_inside_the_feature(bad, corpus):
    from stapel_search.errors import SearchValidationError
    from stapel_search.query import parse_query

    with _settings(GEO_BANDS=True):
        with pytest.raises(SearchValidationError):
            parse_query({"type": DOC_TYPE, "geo_mode": bad})
    # Inert means inert: with the flag off there is nothing it could have
    # changed, so there is nothing to refuse.
    with _settings(GEO_BANDS=False):
        assert parse_query({"type": DOC_TYPE, "geo_mode": bad}).near is None


@pytest.fixture
def mislabelled(db):
    """One row whose TEXT says apple and whose brand facet says nokia.

    This is the shape the fallback exists for: the extraction is defensible
    («apple» really is a brand) and still wrong for this catalogue, so the
    filter empties a page the text search can fill.
    """
    from stapel_search.models import SearchDocument
    from stapel_search.testing import harness

    with harness() as ctx:
        SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
        ctx.reindex(documents=[
            _at("m0", CENTER, day=1,
                title="apple pie recipe book",
                features={"brand": _feature("string", "nokia")}),
        ])
        yield ctx


def test_an_extraction_that_empties_the_page_is_withdrawn(
    api_client, mislabelled, brand_schema
):
    """The one measured win of the 2026-09-03 labelled eval.

    An extracted filter is a GUESS about what a reader meant, and an empty
    page over a catalogue that is not empty is the outcome that proves the
    guess wrong. Falling back to the plain text search bought recall@10
    +0.057, paired bootstrap CI [+0.011, +0.125], P(gain) = 1.00 — more
    than embedding every listing title did, and free.
    """
    with _settings(QUERY_UNDERSTANDING=True):
        # `f.brand=apple` matches nothing here; the WORD is in the title.
        body = _get(api_client, category="electronics/phones", q="apple", limit=50)

    assert body["items"], "the fallback must not leave the reader an empty page"
    assert {item["key"] for item in body["items"]} == {"m0"}
    understanding = body["query_understanding"]
    # The chips come back, but stamped as NOT applied — a chip that is not
    # filtering must never render as one that is.
    assert all(f["applied"] is False for f in understanding["filters"])
    assert "understanding_withdrawn" in body["degraded"]


def test_a_genuinely_empty_catalogue_keeps_its_chips(
    api_client, mislabelled, brand_schema
):
    """Withdrawal is for a wrong guess, not for an honest nothing.

    When the plain text search finds nothing either, the filters were a true
    description of what was searched for and stay applied — otherwise the
    answer would claim a narrowing was undone that changed nothing.
    """
    with _settings(QUERY_UNDERSTANDING=True):
        body = _get(
            api_client, category="electronics/phones", q="zzzznotathing", limit=50
        )
    assert body["items"] == []
    assert "understanding_withdrawn" not in body["degraded"]


# --------------------------------------------------------------------------
# a drawn area beside a centre (0.14.6)
# --------------------------------------------------------------------------


def test_a_drawn_area_on_a_category_page_still_reports_the_distance(api_client, corpus):
    """Д262: the leaf's box cut the answer and took the centre with it.

    Live, the same centre gave a distance on every hit of the unscoped band
    and ``null`` on every hit of the phone leaf, because the leaf applied
    the place as a RECTANGLE and the parser dropped the centre the moment a
    ``bbox`` arrived. The box says which rows; the centre says how far.
    """
    home = _get(api_client, lat=CENTER[0], lon=CENTER[1], radius_km=25, limit=50)
    assert home["items"]
    assert all(item["distance_km"] is not None for item in home["items"])

    leaf = _get(
        api_client,
        category="electronics/phones",
        lat=CENTER[0],
        lon=CENTER[1],
        bbox="49.0,5.5,50.5,7.0",
        limit=50,
    )
    assert leaf["items"]
    assert {item["key"] for item in leaf["items"]} == NEARBY_KEYS
    assert all(item["distance_km"] is not None for item in leaf["items"])


def test_a_box_with_no_centre_still_has_nothing_to_measure_from(api_client, corpus):
    """The other half of the rule, so the fix is not "always emit a number"."""
    body = _get(api_client, bbox="49.0,5.5,50.5,7.0", limit=50)
    assert body["items"]
    assert all(item["distance_km"] is None for item in body["items"])
