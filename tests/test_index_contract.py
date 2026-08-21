"""Layer 2 of the gate: every declared index field, proven by a round trip.

One test per row of ``INDEX_FIELDS``, named by the ``test`` the row
declares — so a field cannot be added to the contract without a test that
resolves, and a test cannot be deleted without the contract noticing
(``IDX003``, plus :func:`test_every_declared_test_exists` below, which
keeps the package self-checking with no linter installed).

Every test has a NEGATIVE half. Without one, "finds everything" passes:
the legacy index that inspired this file had ``features_search``,
``description_en`` and ``geohash`` written and never read, and a
positive-only assertion would have gone green over all three.

These run against the CONFIGURED backend, so CI executes the whole table
once per engine: naive on SQLite, Postgres when ``STAPEL_SEARCH_TEST_DB``
is set, Meilisearch in its own job.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stapel_search.dto import Cursor, GeoFilter, RangeFilter
from stapel_search.index_schema import INDEX_FIELDS
from stapel_search.testing import CENTER, DOC_TYPE, _document, _feature

pytestmark = pytest.mark.django_db


def _extra(ctx, **kwargs):
    """Index one purpose-built document on top of the shared corpus."""
    document = _document(**kwargs)
    ctx.reindex([document])
    return document


# --- doc_type --------------------------------------------------------------


def test_doc_type_scopes_the_corpus(conformance):
    assert set(conformance.keys(conformance.query())) == {"1", "2", "3", "4"}
    assert conformance.keys(conformance.query(doc_type="other-corpus")) == []


# --- doc_key ---------------------------------------------------------------


def test_doc_key_round_trips_and_delete_removes(conformance):
    from stapel_search.services import remove_documents

    hits = conformance.backend.query(conformance.query()).hits
    assert {hit.key for hit in hits} == {"1", "2", "3", "4"}, "the answer's key IS the source key"

    remove_documents(DOC_TYPE, ["1"])
    assert "1" not in conformance.keys(conformance.query())
    assert "2" in conformance.keys(conformance.query())


# --- visible ---------------------------------------------------------------


def test_visible_is_the_implicit_predicate(conformance):
    """Membership follows the pulled document's status, not the event name.

    Republishing a live listing emits ``listing.updated`` carrying
    ``status: pending`` and emits no ``listing.removed`` at all, so an index
    that read the event name would keep a withdrawn listing searchable.
    """
    assert "1" in conformance.keys(conformance.query())
    _extra(conformance, doc_key="1", title="Apple iPhone 13 Pro", status="pending", seq=99)
    assert "1" not in conformance.keys(conformance.query())


# --- language --------------------------------------------------------------


def test_language_filters_and_picks_the_fts_config(conformance):
    # Explicit language narrows the corpus...
    assert set(conformance.keys(conformance.query(language_filter="ru"))) == {"1", "2", "3", "4"}
    assert conformance.keys(conformance.query(language_filter="de")) == []
    # ...and no explicit language narrows nothing: `lang` describes the
    # QUERY, and an Accept-Language header must not hide a whole catalogue.
    assert set(conformance.keys(conformance.query())) == {"1", "2", "3", "4"}

    # The ru document is found by a Russian-stemmed form of a word that
    # occurs in another grammatical number in its body.
    ru = conformance.keys(
        conformance.query(language="ru", language_filter="ru", text=conformance.text("телефон", "ru"))
    )
    assert "1" in ru


# --- owner_key -------------------------------------------------------------


def test_owner_key_filters_to_one_seller(conformance):
    assert set(conformance.keys(conformance.query(owner_key="u2"))) == {"4"}
    assert "4" not in conformance.keys(conformance.query(owner_key="u1"))


# --- category_path ---------------------------------------------------------


def test_category_prefix_finds_descendants(conformance):
    parent = set(conformance.keys(conformance.query(category_path=("electronics",))))
    assert parent == {"1", "2", "3"}, "a parent category must find its children"
    assert conformance.keys(conformance.query(category_path=("marine", "boats"))) == []


# --- title / body / text_extra --------------------------------------------


def test_title_outranks_body(conformance):
    """The same word ranks higher in a title than in a description."""
    _extra(
        conformance,
        doc_key="t-title",
        title="Zenith",
        body="unrelated prose",
        features={"brand": _feature("string", "zenith")},
    )
    _extra(
        conformance,
        doc_key="t-body",
        title="unrelated heading",
        body="a long description that mentions zenith once",
        features={"brand": _feature("string", "other")},
    )
    hits = conformance.backend.query(
        conformance.query(text=conformance.text("zenith", ""), sort="relevance")
    ).hits
    keys = [hit.key for hit in hits]
    assert set(keys) == {"t-title", "t-body"}
    assert keys[0] == "t-title", "a title match must outrank the same match in a body"


def test_body_only_word_is_found(conformance):
    keys = conformance.keys(
        conformance.query(language="ru", language_filter="ru", text=conformance.text("работы", "ru"))
    )
    assert keys == ["3"], "a word occurring only in the description must find the document"


def test_badge_value_is_searchable(conformance):
    assert conformance.keys(conformance.query(text=conformance.text("128", ""))) == ["1"]
    assert conformance.keys(conformance.query(text=conformance.text("256", ""))) == []


# --- text_vec / text_plain -------------------------------------------------


def test_text_vector_is_the_primary_arm(conformance):
    """A well-spelled query is answered without widening it.

    The trigram arm exists for typos; a search that reaches for fuzz when
    the exact arm already answered would return neighbours nobody asked
    for, so the exact arm has to be enough on its own.
    """
    keys = conformance.keys(conformance.query(text=conformance.text("Samsung", "")))
    assert keys == ["2"]


def test_trigram_arm_tolerates_a_typo(conformance):
    if not conformance.capabilities.typo_tolerance:
        pytest.skip("backend declares typo_tolerance: False (and says so in degraded[])")
    keys = conformance.keys(conformance.query(text=conformance.text("Samsng", "")))
    assert "2" in keys
    # Still not a wildcard: an unrelated string finds nothing.
    assert conformance.keys(conformance.query(text=conformance.text("qqqqzzzz", ""))) == []


# --- facets / facet_terms --------------------------------------------------


def test_facet_filter_excludes_non_matches(conformance):
    assert set(conformance.keys(conformance.query(facets={"brand": ["apple"]}))) == {"1"}
    assert conformance.keys(conformance.query(facets={"brand": ["nothing-like-this"]})) == []


def test_facet_counts_match_the_candidate_set(conformance):
    from stapel_search.dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    counts = conformance.backend.facets(conformance.query(), plan).counts["brand"]
    assert counts["apple"] == 1
    assert counts["samsung"] == 1
    # The draft document carries brand=apple and must not be counted.
    assert counts["apple"] != 2


# --- SearchNumber.value ----------------------------------------------------


def test_range_filter_includes_the_bound(conformance):
    inclusive = set(
        conformance.keys(
            conformance.query(ranges=(RangeFilter(slug="year", lower=Decimal(2015)),))
        )
    )
    assert "1" in inclusive, "2015.. includes 2015"
    assert "2" not in inclusive, "2015.. excludes 2014"


# --- price_base ------------------------------------------------------------


def test_price_sorts_with_nulls_last_both_ways(conformance):
    assert conformance.keys(conformance.query(sort="price_asc")) == ["4", "2", "1", "3"]
    assert conformance.keys(conformance.query(sort="price_desc")) == ["1", "2", "4", "3"]


# --- published_at ----------------------------------------------------------


def test_newest_orders_by_publication(conformance):
    assert conformance.keys(conformance.query(sort="newest")) == ["3", "2", "4", "1"]
    # Tie-break: two documents published at the same instant order by key.
    when = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _extra(conformance, doc_key="tie-b", title="B", published_at=when)
    _extra(conformance, doc_key="tie-a", title="A", published_at=when)
    keys = conformance.keys(conformance.query(sort="newest"))
    assert keys.index("tie-a") < keys.index("tie-b")


# --- popularity ------------------------------------------------------------


def test_popularity_signal_moves_the_score(conformance):
    from stapel_search.services import apply_signal

    q = conformance.query(language="ru", language_filter="ru", text=conformance.text("телефон", "ru"), sort="relevance")
    before = {hit.key: hit.score for hit in conformance.backend.query(q).hits}
    assert before, "the fixture query must match something"

    apply_signal({"doc_type": DOC_TYPE, "doc_key": "1", "popularity": 500}, event_id="pop-1")
    after = {hit.key: hit.score for hit in conformance.backend.query(q).hits}
    assert after["1"] > before["1"]
    # A document with no signal is unchanged: popularity is 0, not a guess.
    assert after["2"] == pytest.approx(before["2"], abs=1e-6)


# --- lat / lon -------------------------------------------------------------


def test_radius_includes_near_excludes_far(conformance):
    wide = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=10)
    assert set(conformance.keys(conformance.query(geo=wide))) == {"1", "2"}
    tight = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=1)
    assert set(conformance.keys(conformance.query(geo=tight))) == {"1"}


def test_bbox_crosses_the_antimeridian(conformance):
    crossing = GeoFilter(min_lat=-10.0, min_lon=179.0, max_lat=10.0, max_lon=-179.0)
    assert set(conformance.keys(conformance.query(geo=crossing))) == {"4"}
    ordinary = GeoFilter(min_lat=-10.0, min_lon=-179.0, max_lat=10.0, max_lon=179.0)
    assert "4" not in conformance.keys(conformance.query(geo=ordinary))


# --- geohash ---------------------------------------------------------------


def test_geohash_prefilter_keeps_border_cells(conformance):
    """A document just across a geohash cell edge is still a candidate.

    The prefilter narrows by the geohash cell that CONTAINS the whole search
    box (a cell is a lat/lon rectangle, so a prefix shared by the box's four
    corners contains it). This test places a document a few hundred metres
    away, across the neighbouring cell boundary, and asserts it is found.
    """
    from stapel_geo import geohash as gh

    from stapel_search.backends import _shared as shared

    # Walk east until the 5-character cell changes: that is a real boundary.
    base = gh.encode(CENTER[0], CENTER[1], precision=5)
    lon = CENTER[1]
    for _ in range(2000):
        lon += 0.0005
        if gh.encode(CENTER[0], lon, precision=5) != base:
            break
    else:  # pragma: no cover - the grid guarantees a boundary within range
        pytest.fail("no geohash cell boundary found nearby")

    _extra(
        conformance,
        doc_key="border",
        title="Just across the cell edge",
        lat=Decimal(str(CENTER[0])),
        lon=Decimal(f"{lon:.6f}"),
        geohash=gh.encode(CENTER[0], lon, precision=12),
    )
    distance = shared.haversine_km(CENTER[0], CENTER[1], CENTER[0], lon)
    radius = max(2.0, distance * 2)
    found = conformance.keys(
        conformance.query(geo=GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=radius))
    )
    assert "border" in found, "the geohash prefilter must not drop a neighbouring cell"


# --- boost -----------------------------------------------------------------


def test_boost_moves_relevance_only(conformance):
    from stapel_search.services import apply_signal

    apply_signal({"doc_type": DOC_TYPE, "doc_key": "3", "boost": 5.0}, event_id="boost-3")

    relevance = conformance.keys(
        conformance.query(language="ru", language_filter="ru", text=conformance.text("ноутбук", "ru"), sort="relevance")
    )
    assert relevance and relevance[0] == "3"

    # The same boost must be inert under an explicit sort. Not a setting:
    # promotion_boost declares `relevance` and nothing else.
    assert conformance.keys(conformance.query(sort="price_asc")) == ["4", "2", "1", "3"]


# --- promoted --------------------------------------------------------------


def test_promoted_flag_is_on_every_item(conformance):
    """DSA Art. 26: the marker is on every item, false included."""
    from stapel_search.services import apply_signal, search

    apply_signal({"doc_type": DOC_TYPE, "doc_key": "2", "promoted": True}, event_id="promo-2")
    for sort in ("newest", "price_asc", "price_desc"):
        response = search({"type": DOC_TYPE, "sort": sort, "limit": "20"})
        assert response["items"], sort
        for item in response["items"]:
            assert "promoted" in item, f"{sort}: the marker cannot be omitted"
        promoted = {item["key"]: item["promoted"] for item in response["items"]}
        assert promoted["2"] is True
        assert promoted["1"] is False


# --- promotion_expires_at --------------------------------------------------


def test_expired_promotion_is_dropped(conformance):
    from stapel_search.models import SearchDocument
    from stapel_search.services import apply_signal
    from stapel_search.tasks import search_expire_signals

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    apply_signal(
        {"doc_type": DOC_TYPE, "doc_key": "1", "promoted": True, "boost": 2.0,
         "expires_at": future},
        event_id="promo-future",
    )
    assert search_expire_signals() == 0, "a live promotion must survive the beat"
    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").promoted is True

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    apply_signal(
        {"doc_type": DOC_TYPE, "doc_key": "1", "promoted": True, "boost": 2.0,
         "expires_at": past},
        event_id="promo-past",
    )
    assert search_expire_signals() == 1
    row = SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1")
    assert row.promoted is False
    assert row.boost == 0.0


# --- card ------------------------------------------------------------------


def test_card_is_returned_without_a_second_query(conformance):
    from stapel_search.services import search

    response = search({"type": DOC_TYPE, "sort": "price_asc", "limit": "20"})
    cards = {item["key"]: item["card"] for item in response["items"]}
    assert cards["1"]["title"] == "Apple iPhone 13 Pro"
    assert cards["1"]["price"] == "500.00"


# --- source_seq ------------------------------------------------------------


def test_stale_sequence_does_not_overwrite(conformance):
    from stapel_search.models import SearchDocument
    from stapel_search.services import index_documents

    fresh = _document(doc_key="1", title="Fresh title", seq=5000)
    index_documents(DOC_TYPE, [fresh])
    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").title == "Fresh title"

    stale = _document(doc_key="1", title="Stale title", seq=10)
    report = index_documents(DOC_TYPE, [stale])
    assert report.skipped_stale == 1
    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").title == "Fresh title"


# --- source_event_id -------------------------------------------------------


def test_redelivery_is_a_noop(conformance):
    from stapel_search.models import SearchDocument
    from stapel_search.services import index_documents

    first = _document(doc_key="1", title="Delivered once", seq=6000, source_event_id="evt-1")
    index_documents(DOC_TYPE, [first])

    # The SAME event id, carrying different content and a higher sequence:
    # at-least-once delivery must not apply it twice.
    replay = _document(doc_key="1", title="Applied twice", seq=6001, source_event_id="evt-1")
    report = index_documents(DOC_TYPE, [replay])
    assert report.skipped_duplicate == 1
    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").title == "Delivered once"


# --- indexed_at / source_updated_at ---------------------------------------


def test_drift_is_detected(conformance, monkeypatch):
    """``drift_check`` reports a document the source has and we do not."""
    from stapel_search import services
    from stapel_search.models import SearchDocument

    rows = [
        {"key": row.doc_key, "seq": row.source_seq}
        for row in SearchDocument.objects.filter(doc_type=DOC_TYPE)
    ]
    rows.append({"key": "999", "seq": 1})

    def fake_call(name, payload):
        assert payload["cursor"] is None
        return {"rows": rows, "cursor": None, "total": len(rows)}

    monkeypatch.setattr("stapel_core.comm.call", fake_call)
    report = services.drift_check(DOC_TYPE)
    assert report.in_sync is False
    assert "999" in report.missing_keys


def test_stale_documents_are_caught_up(conformance, monkeypatch):
    """``search_reindex_stale`` re-pulls what a lost event never delivered."""
    from stapel_search import services
    from stapel_search.models import SearchDocument

    SearchDocument.objects.filter(doc_type=DOC_TYPE, doc_key="1").update(title="Went stale")

    def fake_call(name, payload):
        assert name == "conformance.documents"
        return {
            key: {
                "doc_type": DOC_TYPE,
                "doc_key": key,
                "status": "published",
                "title": "Caught up",
                "seq": 9000,
            }
            for key in payload["keys"]
        }

    monkeypatch.setattr("stapel_core.comm.call", fake_call)
    services.reindex_stale(DOC_TYPE, limit=10)
    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").title == "Caught up"


# --- the meta-gate ---------------------------------------------------------


def test_every_declared_test_exists():
    """Every ``IndexField.test`` resolves to a function in this module.

    The static form of the same rule ``IDX003`` enforces fleet-wide, kept
    here so the package cannot drift even where the linter is not installed.
    A declared field whose proof went missing is a field back on the honour
    system.
    """
    import sys

    module = sys.modules[__name__]
    missing = []
    for field in INDEX_FIELDS:
        path, _, name = field.test.partition("::")
        if path != "tests/test_index_contract.py" or not hasattr(module, name):
            missing.append(f"{field.field} -> {field.test}")
    assert not missing, f"declared tests that do not exist: {missing}"


def test_every_field_has_read_paths_and_a_test():
    for field in INDEX_FIELDS:
        assert field.read_paths, field.field
        assert field.test, field.field
        assert field.proves, field.field


def test_cursor_round_trips():
    from stapel_search.query import decode_cursor, encode_cursor

    cursor = Cursor(sort_value="2026-01-01T00:00:00+00:00", doc_key="42", offset=24)
    assert decode_cursor(encode_cursor(cursor)) == cursor
