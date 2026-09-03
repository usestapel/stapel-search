"""The public geo grid: a stranger's geo answers may not beat the card.

``stapel-listings`` publishes a listing's coordinates rounded to
``PUBLIC_COORD_PRECISION`` (~1.1km) and blanks the public geohash, because a
private seller's pin is their front door. That promise is only worth what the
SEARCH surface leaves of it: ``/query`` is ``AllowAny``, it takes a centre the
caller chooses and it answers with a distance. Two cheap attacks follow, and
both are performed here against the real code rather than described:

* **Trilateration** — three centres, three distances, one exact point.
* **Bisection** — ``bbox`` excludes rows, so halving the rectangle around a
  row converges on it in a few dozen requests.

Neither is closed by making the ANSWER coarse: the caller's centre and box are
continuous, so any oracle that reads the true point can be probed to arbitrary
precision by moving them. What closes both is that the row's position is read
through the same grid the card publishes — every geo answer becomes a function
of the PUBLISHED point and of nothing finer, and two pins in one cell are
indistinguishable to any number of queries.

The battery below therefore asserts that indistinguishability, and asserts it
by running each attack twice against two pins that share one cell.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from stapel_search.testing import DOC_TYPE, _document

pytestmark = pytest.mark.django_db

QUERY = "/api/v1/query"

#: Two pins inside ONE ~1.1km cell (lat [49.605, 49.615), lon [6.125, 6.135)),
#: 1.2km apart — near opposite corners, so any oracle finer than the cell
#: separates them instantly.
PIN_A = (49.6060, 6.1260)
PIN_B = (49.6149, 6.1349)
#: The point the CARD publishes for both of them.
PUBLISHED = (49.61, 6.13)

#: Three observation posts, roughly 120 degrees apart around the cell.
POSTS = [(49.9000, 6.1300), (49.4500, 5.7000), (49.4500, 6.6000)]


def _row(key, point, **overrides):
    from stapel_geo import geohash as gh

    lat, lon = point
    fields = dict(
        doc_key=key,
        title=f"row {key}",
        body="a row that exists",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        lat=Decimal(str(lat)),
        lon=Decimal(str(lon)),
        geohash=gh.encode(lat, lon, precision=12),
        card={"title": f"row {key}"},
    )
    fields.update(overrides)
    return _document(**fields)


@pytest.fixture
def one_pin(db):
    """A corpus of exactly one row, reloadable per pin under attack."""
    from stapel_search.models import SearchDocument
    from stapel_search.testing import harness

    with harness() as ctx:

        def load(point, **overrides):
            SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
            ctx.reindex(documents=[_row("pin", point, **overrides)])

        yield load


def _get(api_client, **params):
    from stapel_search.backends import reset_backend_cache

    reset_backend_cache()
    response = api_client.get(QUERY, {"type": DOC_TYPE, **params})
    assert response.status_code == 200, response.content
    return response.json()


def _distance(api_client, post, **params) -> float | None:
    body = _get(api_client, lat=post[0], lon=post[1], **params)
    items = {item["key"]: item for item in body["items"]}
    return items["pin"]["distance_km"] if "pin" in items else None


def _present(api_client, **params) -> bool:
    return any(item["key"] == "pin" for item in _get(api_client, **params)["items"])


def _grid():
    from stapel_search.backends import _shared as shared

    precision = shared.public_precision()
    return precision, shared.cell_km(precision), shared.distance_quantum_km(precision)


def _km(a, b) -> float:
    from stapel_search.backends import _shared as shared

    return shared.haversine_km(a[0], a[1], b[0], b[1])


def _trilaterate(observations):
    """Solve three (centre, distance) circles for one point, in a local plane.

    The attacker's own arithmetic, and it is not much: an equirectangular
    projection around the first post is good to well under a metre over these
    tens of kilometres, and three circles reduce to a 2x2 linear system.
    """
    from stapel_search.backends._shared import _KM_PER_DEG

    origin = observations[0][0]
    scale_x = _KM_PER_DEG * math.cos(math.radians(origin[0]))

    def to_xy(point):
        return ((point[1] - origin[1]) * scale_x, (point[0] - origin[0]) * _KM_PER_DEG)

    (x1, y1), r1 = to_xy(observations[0][0]), observations[0][1]
    (x2, y2), r2 = to_xy(observations[1][0]), observations[1][1]
    (x3, y3), r3 = to_xy(observations[2][0]), observations[2][1]
    a1, b1 = 2 * (x2 - x1), 2 * (y2 - y1)
    c1 = r1**2 - r2**2 - x1**2 + x2**2 - y1**2 + y2**2
    a2, b2 = 2 * (x3 - x1), 2 * (y3 - y1)
    c2 = r1**2 - r3**2 - x1**2 + x3**2 - y1**2 + y3**2
    det = a1 * b2 - a2 * b1
    x = (c1 * b2 - c2 * b1) / det
    y = (a1 * c2 - a2 * c1) / det
    return (origin[0] + y / _KM_PER_DEG, origin[1] + x / scale_x)


# --------------------------------------------------------------------------
# the arithmetic the whole module hangs off
# --------------------------------------------------------------------------


def test_the_distance_quantum_is_derived_from_the_coarsening():
    """Not a round-looking number: the coarsening cell's own diagonal.

    The card rounds to ``CARD_COORD_PRECISION`` decimals, so it declares one
    square cell of side ``111.32 * 10**-p`` km — 1.113km at the default 2. The
    largest distance between two points that cell declares identical is its
    DIAGONAL, so a distance quantum any finer would separate two points the
    card does not, and repetition turns that difference into the pin.
    """
    precision, cell, quantum = _grid()
    assert precision == 2
    assert cell == pytest.approx(1.113, abs=0.001)
    assert quantum == pytest.approx(cell * math.sqrt(2), abs=0.002)
    assert quantum > cell


# --------------------------------------------------------------------------
# attack 1: trilateration
# --------------------------------------------------------------------------


def test_trilateration_recovers_the_published_area_and_never_the_pin(
    api_client, one_pin
):
    """Three queries, three distances, one point — and it is the card's point.

    The attack runs twice, against two pins 1.2km apart inside one cell. Both
    runs observe the same three distances and therefore recover the same
    point: a solution that moves with the pin IS the pin.
    """
    _, cell, quantum = _grid()

    recovered, observed = {}, {}
    for name, pin in (("a", PIN_A), ("b", PIN_B)):
        one_pin(pin)
        distances = [_distance(api_client, post) for post in POSTS]
        assert all(d is not None for d in distances)
        observed[name] = distances
        recovered[name] = _trilaterate(list(zip(POSTS, distances)))

    assert _km(PIN_A, PIN_B) > 1.0, "the two pins are far apart at street scale"
    assert observed["a"] == observed["b"], (
        "the three distances moved with the pin, so trilateration recovers it"
    )
    assert _km(recovered["a"], recovered["b"]) < 0.001

    # It does converge — onto the neighbourhood the card already publishes,
    # which is the point: coarse is not the same as useless.
    assert _km(recovered["a"], PUBLISHED) <= quantum

    # And what it recovered is not either pin: the attack learned the area,
    # never the address.
    assert _km(recovered["a"], PIN_A) > cell / 4.0


def test_every_reported_distance_is_a_multiple_of_the_quantum(api_client, one_pin):
    """A number carrying more precision than the position it came from is a
    leak whatever else guards it."""
    _, _, quantum = _grid()
    one_pin(PIN_A)
    for post in POSTS + [(49.6120, 6.1320)]:
        distance = _distance(api_client, post)
        assert distance is not None
        steps = distance / quantum
        assert abs(steps - round(steps)) < 1e-6, (distance, quantum)
        assert distance <= _km(post, PIN_A) + 1e-9, "floored, never rounded up"


def test_the_cursor_does_not_carry_a_finer_distance(api_client, one_pin):
    """``sort=distance`` puts the sort value in the anchor, base64 and all.

    An opaque cursor is not a private one — a client round-trips it verbatim
    and can decode it. It must carry the same quantized number the item does.
    """
    from stapel_search.query import decode_cursor
    from stapel_search.services import index_documents

    _, _, quantum = _grid()
    one_pin(PIN_A)
    # A second row, so the first page has a successor and therefore an anchor.
    index_documents(DOC_TYPE, [_row("other", (49.70, 6.20))])

    body = _get(api_client, lat=POSTS[0][0], lon=POSTS[0][1], sort="distance", limit=1)
    anchor = body["next_anchor"]
    assert anchor
    value = float(decode_cursor(anchor).sort_value)
    steps = value / quantum
    assert abs(steps - round(steps)) < 1e-6, value


# --------------------------------------------------------------------------
# attack 2: bisection
# --------------------------------------------------------------------------


def _bisect_latitude(api_client, *, lo: float, hi: float, lon_lo: float, lon_hi: float):
    """Halve the box until it stops telling the attacker anything.

    A sound bisection keeps the half that contains the row — and only when
    exactly ONE half does. When both halves answer "present", the split has
    learned nothing and the attack has stalled; that stall, and how wide the
    interval still is when it happens, is what this measures.
    """
    requests = 0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        low = _present(api_client, bbox=f"{lo},{lon_lo},{mid},{lon_hi}")
        high = _present(api_client, bbox=f"{mid},{lon_lo},{hi},{lon_hi}")
        requests += 2
        if low == high:  # both, or neither: no information in this split
            break
        lo, hi = (lo, mid) if low else (mid, hi)
    return lo, hi, requests


def test_bbox_bisection_stalls_above_one_cell(api_client, one_pin):
    """The rectangle is the caller's, so it is snapped onto the module's grid.

    Halving converges on a point only while each half is an honest question.
    Once every box is grown outward to whole cells, a split inside one cell
    answers "present" on both sides and the attacker is finished — with the
    interval still at least a cell wide and both pins still inside it.
    """
    precision, _, _ = _grid()
    step = 10.0**-precision

    intervals = {}
    for name, pin in (("a", PIN_A), ("b", PIN_B)):
        one_pin(pin)
        lo, hi, requests = _bisect_latitude(
            api_client, lo=49.55, hi=49.67, lon_lo=6.05, lon_hi=6.25
        )
        assert requests <= 24, "the attack is cheap; that is the premise"
        intervals[name] = (round(lo, 9), round(hi, 9))
        assert (hi - lo) >= step, (
            f"bisection converged to {(hi - lo) / step:.3f} of a cell — the "
            "box localizes the pin below what the card publishes"
        )
        assert lo <= pin[0] <= hi

    assert intervals["a"] == intervals["b"], (
        "the attack ended somewhere different for two pins in one cell, so it "
        "separated them"
    )


def test_a_box_inside_one_cell_cannot_separate_two_pins(api_client, one_pin):
    """The degenerate case the bisection converges to, asked directly."""
    boxes = [
        "49.6050,6.1250,49.6100,6.1300",  # the cell's SW quarter: pin A only
        "49.6100,6.1300,49.6150,6.1350",  # its NE quarter: pin B only
        "49.6059,6.1259,49.6061,6.1261",  # twenty metres around pin A
    ]
    answers = {}
    for name, pin in (("a", PIN_A), ("b", PIN_B)):
        one_pin(pin)
        answers[name] = [_present(api_client, bbox=box) for box in boxes]
    assert answers["a"] == answers["b"], answers


def test_radius_bisection_cannot_separate_two_pins(api_client, one_pin):
    """``radius_km`` EXCLUDES, so it is the same oracle in polar form.

    A binary search on the radius from a fixed centre converges on the true
    distance however coarsely the distance itself is reported — which is why
    the fix has to be at the position, not at the number.
    """
    converged = {}
    for name, pin in (("a", PIN_A), ("b", PIN_B)):
        one_pin(pin)
        lo, hi = 0.0, 60.0
        for _ in range(24):
            mid = (lo + hi) / 2.0
            if _present(api_client, lat=POSTS[0][0], lon=POSTS[0][1], radius_km=mid):
                hi = mid
            else:
                lo = mid
        converged[name] = round(hi, 6)
    assert converged["a"] == converged["b"], converged


def test_the_nearby_band_label_is_not_a_finer_oracle(api_client, one_pin):
    """Under ``geo_mode=rank`` the band is a label on a caller-chosen edge —
    which is still a boolean predicate at any radius the caller likes."""
    from django.test import override_settings

    from stapel_search._codegen_settings import default_backend

    labels = {}
    for name, pin in (("a", PIN_A), ("b", PIN_B)):
        one_pin(pin)
        with override_settings(
            STAPEL_SEARCH={"BACKEND": default_backend(), "GEO_BANDS": True}
        ):
            labels[name] = [
                _get(
                    api_client,
                    lat=POSTS[0][0],
                    lon=POSTS[0][1],
                    radius_km=radius,
                    geo_mode="rank",
                )["items"][0]["band"]
                for radius in (32.0, 32.2, 32.4, 32.6, 32.8, 33.0)
            ]
    assert labels["a"] == labels["b"], labels


# --------------------------------------------------------------------------
# the precise audience still exists
# --------------------------------------------------------------------------


def test_staff_still_get_the_true_distance(api_client, one_pin, staff_user):
    """The grid is an audience rule, not a lobotomy: the resolver that lets an
    owner read their own VIN lets staff read the true distance."""
    one_pin(PIN_A)
    api_client.force_authenticate(user=staff_user)
    assert _distance(api_client, POSTS[0]) == pytest.approx(_km(POSTS[0], PIN_A), abs=0.01)


def test_the_owner_of_the_row_still_gets_the_true_distance(api_client, one_pin, db):
    """``?owner=<me>`` is the seller's own list — their own pin is not a leak."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create(username="seller", email="s@example.com")
    one_pin(PIN_A, owner_key=str(user.pk))
    api_client.force_authenticate(user=user)
    body = _get(api_client, lat=POSTS[0][0], lon=POSTS[0][1], owner=str(user.pk))
    assert body["items"][0]["distance_km"] == pytest.approx(
        _km(POSTS[0], PIN_A), abs=0.01
    )


def test_a_signed_in_stranger_reading_someone_elses_list_gets_the_grid(
    api_client, one_pin, db
):
    """``?owner=<somebody else>`` is a public profile page, not an owner view."""
    from django.contrib.auth import get_user_model

    owner = get_user_model().objects.create(username="seller2", email="s2@example.com")
    nosy = get_user_model().objects.create(username="nosy", email="n@example.com")
    one_pin(PIN_A, owner_key=str(owner.pk))
    api_client.force_authenticate(user=nosy)
    _, _, quantum = _grid()
    distance = _distance(api_client, POSTS[0], owner=str(owner.pk))
    steps = distance / quantum
    assert abs(steps - round(steps)) < 1e-6, distance


# --------------------------------------------------------------------------
# the card, on every path
# --------------------------------------------------------------------------


def test_the_card_never_carries_coordinates_flag_or_no_flag(api_client, one_pin):
    """S2: ``_card_area`` ran only under ``geo_mode=rank``, which needs a flag
    that is off — and where it did run it OVERWROTE ``lat``/``lon`` instead of
    removing what it did not recognise, so a card carrying ``geohash`` or
    ``latitude`` walked straight through."""
    from stapel_geo import geohash as gh

    dirty = {
        "title": "row pin",
        "price": "10.00",
        "lat": float(PIN_A[0]),
        "lon": float(PIN_A[1]),
        "latitude": float(PIN_A[0]),
        "longitude": float(PIN_A[1]),
        "geohash": gh.encode(PIN_A[0], PIN_A[1], precision=12),
        "location": {"lat": float(PIN_A[0]), "lng": float(PIN_A[1])},
    }
    one_pin(PIN_A, card=dirty)

    card = _get(api_client)["items"][0]["card"]
    assert card["title"] == "row pin" and card["price"] == "10.00"
    for gone in ("geohash", "location"):
        assert gone not in card, card
    for key in ("lat", "latitude"):
        assert card[key] == pytest.approx(PUBLISHED[0], abs=1e-9), card
    for key in ("lon", "longitude"):
        assert card[key] == pytest.approx(PUBLISHED[1], abs=1e-9), card
    assert card["geo_precision_km"] == _grid()[1]


def test_a_card_with_no_coordinates_is_untouched(api_client, one_pin):
    """The fleet's own card carries none, and its answer must not move."""
    one_pin(PIN_A, card={"title": "row pin", "price": "10.00"})
    assert _get(api_client)["items"][0]["card"] == {"title": "row pin", "price": "10.00"}


def test_the_dirty_card_reaches_staff_as_stored(api_client, one_pin, staff_user):
    """Redaction is an audience rule here too, not a rewrite of the index."""
    one_pin(PIN_A, card={"title": "row pin", "geohash": "u0v90"})
    api_client.force_authenticate(user=staff_user)
    assert _get(api_client)["items"][0]["card"]["geohash"] == "u0v90"


# --------------------------------------------------------------------------
# the SQL half, without a server
# --------------------------------------------------------------------------


def test_the_postgres_grid_expression_carries_no_stray_placeholders():
    """Every fragment this fix touched is built as ``(sql, params)`` and the
    two are assembled by TEXTUAL order, so a clause added with the wrong
    number of ``%s`` corrupts every parameter after it — silently, into a
    query that still runs. Counting them needs no server."""
    from stapel_attributes import visibility

    from stapel_search.backends.postgres import PostgresSearchBackend
    from stapel_search.dto import GeoFilter, SearchQuery

    backend = PostgresSearchBackend()
    box = GeoFilter(min_lat=49.605, min_lon=6.125, max_lat=49.615, max_lon=6.135)
    circle = GeoFilter(lat=POSTS[0][0], lon=POSTS[0][1], radius_km=25.0)
    for audience in (visibility.ANONYMOUS, visibility.AUDIENCE_STAFF):
        for geo in (box, circle):
            q = SearchQuery(doc_type=DOC_TYPE, geo=geo, near=circle, audience=audience)
            for sql, params in (
                backend._where(q, trigram=False),
                backend._distance_expression(q),
                backend._measured_distance(q),
                backend._score_expression(q),
                backend._near_predicate(q),
            ):
                assert sql.count("%s") == len(params), (audience, geo, sql, params)


def test_the_postgres_snap_is_the_python_snap():
    """One arithmetic rule, spelled twice — so state it twice and compare."""
    from stapel_attributes import visibility

    from stapel_search.backends import _shared as shared
    from stapel_search.backends.postgres import PostgresSearchBackend
    from stapel_search.dto import SearchQuery

    anonymous = SearchQuery(doc_type=DOC_TYPE, audience=visibility.ANONYMOUS)
    staff = SearchQuery(doc_type=DOC_TYPE, audience=visibility.AUDIENCE_STAFF)
    scale = 10 ** shared.public_precision()
    assert PostgresSearchBackend._column("lat", anonymous) == (
        f"(floor(d.lat::double precision * {scale} + 0.5) / {scale})"
    )
    assert PostgresSearchBackend._column("lat", staff) == "d.lat::double precision"
    # ...and the Python side computes exactly that, for both signs and a tie.
    for value in (49.6149, -49.6149, 6.125, -6.125, 0.0, 179.9999):
        assert shared.snap_to_grid(value, shared.public_precision()) == pytest.approx(
            math.floor(value * scale + 0.5) / scale, abs=1e-12
        )


def test_the_shared_helper_is_not_a_second_grid():
    """``coarse_coordinates`` and the distance grid are ONE rule, or the card
    and the distance disagree about which cell a listing is in."""
    from stapel_search.backends import _shared as shared

    precision = shared.public_precision()
    for pin in (PIN_A, PIN_B):
        card_lat, card_lon, precision_km = shared.coarse_coordinates(*pin, precision)
        assert (card_lat, card_lon) == (
            shared.snap_to_grid(pin[0], precision),
            shared.snap_to_grid(pin[1], precision),
        )
        assert precision_km == shared.cell_km(precision)
