"""Semantics every backend must agree on, written once.

Anything in here is a decision about *meaning* — what "inside the radius"
is, what a boost does to a score, where NULL prices sort, what the tie-break
is. Duplicating those per engine is how two backends drift while both look
green in isolation, which is the defect class the conformance suite exists
for. Engine-specific *execution* (a GIN predicate, a Meili filter string)
stays in the backend; the meaning lives here.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from ..dto import GeoFilter, RangeFilter, SearchQuery

#: Returned by :func:`geo_distance_km` for a row outside the requested
#: radius — distinct from ``None`` ("no geo filter was asked for").
OUT_OF_RANGE = object()

#: Kilometres per degree of latitude. Good to ~0.3% anywhere, which is
#: three orders of magnitude finer than a radius filter's meaning.
_KM_PER_DEG = 111.32


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------


def category_matches(row_path: list, wanted: tuple[str, ...]) -> bool:
    """A parent category finds its descendants — prefix containment.

    Keyed on the path prefix rather than the bare code because
    ``hierarchical_select`` guarantees uniqueness only among siblings, so the
    same code can legitimately appear under two parents.
    """
    if not wanted:
        return True
    path = [str(p) for p in (row_path or [])]
    if len(wanted) > len(path):
        return False
    return path[: len(wanted)] == [str(w) for w in wanted]


def facet_terms_for(slug: str, values) -> list[str]:
    """``["brand=apple", "brand=samsung"]`` — the OR set for one slug."""
    return [f"{slug}={v}" for v in values]


def bucket_limit(plan, slug: str) -> int:
    """How many buckets one facet group may answer with.

    ONE rule for every engine, because a cap is a semantic and not an
    implementation detail: a panel that shows 200 makes on Postgres and all
    of them on the reference walk is two products.

    Two numbers, and the split is the whole point. An INLINE option set is
    authored by hand and a category that lists 200 of them has a different
    problem; a VOCABULARY level is data — a live make dictionary holds 418
    terms — and 0.14.1's flat ``LIMIT 200`` cut it at 200, silently, ordered
    by count, so the tail of the alphabet simply did not exist in the panel
    and no field in the answer said so. A dictionary control that filters
    the bucket list can only filter what it was sent.

    So ``MAX_FACET_VALUES_VOCABULARY`` governs the slugs the plan knows are
    vocabulary-backed (``optionsRef``) and ``MAX_FACET_VALUES`` governs the
    rest. Both are still caps: an unbounded group is a response whose size
    is set by the corpus.
    """
    from ..conf import search_settings

    if slug in (getattr(plan, "vocabulary_refs", None) or {}):
        return int(search_settings.MAX_FACET_VALUES_VOCABULARY)
    return int(search_settings.MAX_FACET_VALUES)


def top_buckets(counts: dict[str, int], limit: int) -> dict[str, int]:
    """The *limit* biggest buckets, ties broken by the term — the SQL order.

    ``ORDER BY n DESC, term ASC LIMIT k`` in Python, so an engine that counts
    in memory truncates the same way the one that counts in SQL does.
    """
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[:limit])


def facets_match(row_terms, wanted: dict[str, list[str]]) -> bool:
    """AND between slugs, OR within a slug — the only semantics a facet
    panel's user expects without being told.

    Matched against ``facet_terms``, not against ``facets``, and on purpose:
    the term list already carries every prefix of a hierarchical path, so an
    ancestor filter finds a descendant with a plain set intersection — the
    same single indexed operation Postgres performs with
    ``facet_terms_arr && ARRAY[...]``. Two engines agreeing because they ask
    the same question beats two engines agreeing by coincidence.
    """
    terms = {str(t) for t in (row_terms or [])}
    for slug, values in (wanted or {}).items():
        if not terms.intersection(facet_terms_for(slug, values)):
            return False
    return True


def split_ranges(
    ranges: tuple[RangeFilter, ...],
) -> tuple[tuple[tuple[str, RangeFilter], ...], tuple[RangeFilter, ...]]:
    """Split range predicates into the two axes an engine has to serve.

    Returns ``(core, attributes)`` where *core* pairs each reserved slug with
    the document COLUMN it addresses (``index_schema.CORE_RANGE_FIELDS``) and
    *attributes* is everything else, resolved through ``SearchNumber`` as
    before. Every engine calls this rather than pattern-matching a slug of
    its own, because the whole point of the map is that the three backends
    cannot drift about what ``r.price`` means.
    """
    from ..index_schema import CORE_RANGE_FIELDS

    core: list[tuple[str, RangeFilter]] = []
    attributes: list[RangeFilter] = []
    for spec in ranges:
        field = CORE_RANGE_FIELDS.get(spec.slug)
        if field is None:
            attributes.append(spec)
        else:
            core.append((field, spec))
    return tuple(core), tuple(attributes)


def narrow_by_ranges(qs, ranges: tuple[RangeFilter, ...]):
    """Apply every range predicate to *qs*.

    A core-field range is a plain column comparison (and therefore excludes
    a document whose column is NULL — an unpriced listing is not a cheap
    one); an attribute range stays one indexed semi-join over
    ``SearchNumber``.
    """
    from ..models import SearchNumber

    core, attributes = split_ranges(ranges)
    for field, spec in core:
        if spec.lower is not None:
            qs = qs.filter(**{f"{field}__gte": spec.lower})
        if spec.upper is not None:
            qs = qs.filter(**{f"{field}__lte": spec.upper})
    for spec in attributes:
        matching = SearchNumber.objects.filter(slug=spec.slug)
        if spec.lower is not None:
            matching = matching.filter(value__gte=spec.lower)
        if spec.upper is not None:
            matching = matching.filter(value__lte=spec.upper)
        qs = qs.filter(pk__in=matching.values("document_id"))
    return qs


def parse_range(raw: str) -> tuple[Decimal | None, Decimal | None]:
    """``"100..500"`` / ``"100.."`` / ``"..500"`` -> bounds. Raises ValueError."""
    if ".." not in raw:
        raise ValueError("a range needs '..'")
    low_s, _, high_s = raw.partition("..")
    try:
        low = Decimal(low_s) if low_s.strip() else None
        high = Decimal(high_s) if high_s.strip() else None
    except InvalidOperation:
        raise ValueError("range bounds must be numbers") from None
    if low is None and high is None:
        raise ValueError("a range needs at least one bound")
    if low is not None and high is not None and low > high:
        raise ValueError("range lower bound is above the upper bound")
    return low, high


# --------------------------------------------------------------------------
# geo
# --------------------------------------------------------------------------


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True great-circle distance, through ``stapel_geo``'s arithmetic.

    Verdict §18.10: the geohash/haversine math is IMPORTED, not copied —
    ``stapel_geo.geohash`` is pure (pygeohash, no Django), so this is a
    library call, and ``stapel_geo`` is never added to ``INSTALLED_APPS``.
    Encoding at precision 12 pins each point to ~2cm, far below anything a
    radius filter means.
    """
    from stapel_geo import geohash as geohash_utils

    return geohash_utils.distance_km(
        geohash_utils.encode(lat1, lon1, precision=12),
        geohash_utils.encode(lat2, lon2, precision=12),
    )


def radius_bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Latitude/longitude window enclosing the circle. Pole- and wrap-safe."""
    d_lat = radius_km / _KM_PER_DEG
    cos_lat = math.cos(math.radians(max(-89.9, min(89.9, lat))))
    d_lon = 180.0 if cos_lat < 1e-6 else min(180.0, radius_km / (_KM_PER_DEG * cos_lat))
    min_lat = max(-90.0, lat - d_lat)
    max_lat = min(90.0, lat + d_lat)
    min_lon = lon - d_lon
    max_lon = lon + d_lon
    if min_lon < -180.0:
        min_lon += 360.0
    if max_lon > 180.0:
        max_lon -= 360.0
    return min_lat, min_lon, max_lat, max_lon


def bbox_of(geo: GeoFilter, *, slack_km: float = 0.0) -> tuple[float, float, float, float] | None:
    """The rectangle this filter narrows to, radius filters included.

    *slack_km* widens the RADIUS-derived box only. It is what a coarse
    prefilter needs when the exact half measures a snapped position (see
    :func:`grid_slack_km`): the disc is drawn around the caller's centre in
    true coordinates, but membership is decided against a point up to half a
    cell diagonal away, and a prefilter that cannot see that row drops it.
    A rectangle filter needs no slack — :func:`grid_aligned_bbox` has already
    made it a union of whole cells, which contains every point that snaps
    into it.
    """
    if geo is None:
        return None
    if geo.is_bbox:
        return (geo.min_lat, geo.min_lon, geo.max_lat, geo.max_lon)
    if geo.has_center and geo.radius_km:
        return radius_bbox(geo.lat, geo.lon, float(geo.radius_km) + slack_km)
    return None


def bbox_matches(lat, lon, geo: GeoFilter | None) -> bool:
    """Row inside the rectangle. ``min_lon > max_lon`` crosses +/-180.

    The antimeridian contract is borrowed verbatim from
    ``GeoSearchBackend.bbox`` and is the first scenario of the conformance
    suite, because it is the one geo rule two engines are most likely to
    quietly disagree about.
    """
    if geo is None or not geo.is_bbox:
        return True
    flat, flon = _as_float(lat), _as_float(lon)
    if flat is None or flon is None:
        return False
    if not (geo.min_lat <= flat <= geo.max_lat):
        return False
    if geo.crosses_antimeridian:
        return flon >= geo.min_lon or flon <= geo.max_lon
    return geo.min_lon <= flon <= geo.max_lon


def geo_distance_km(lat, lon, geohash: str, geo: GeoFilter | None):
    """Distance to the centre, :data:`OUT_OF_RANGE`, or ``None``.

    ``None`` means there is no distance to report — no centre was given, or
    the row carries no coordinates. A document with no coordinates is
    *excluded* only when a ``radius_km`` actually bounds the query, because
    "within 25km" is a claim and a row that cannot support it must not be in
    the answer. A bare centre bounds nothing: it asks how far, not whether,
    so a row that cannot answer "how far" stays in the answer with no
    distance — which is also what the Postgres backend has always done,
    since ``_where`` adds no predicate for a radius-less centre. The two
    engines disagreeing here was invisible to the conformance suite only
    because its one coordinate-less row is a draft.
    """
    if geo is None or not geo.has_center:
        return None
    flat, flon = _as_float(lat), _as_float(lon)
    if flat is None or flon is None:
        return OUT_OF_RANGE if geo.radius_km is not None else None
    distance = haversine_km(geo.lat, geo.lon, flat, flon)
    if geo.radius_km is not None and distance > geo.radius_km:
        return OUT_OF_RANGE
    return distance


def _cell_grid(precision: int) -> tuple[int, int, float, float]:
    """``(lat_bits, lon_bits, lat_step, lon_step)`` of one cell at *precision*.

    A geohash interleaves bits starting with longitude, five per character,
    so a cell is a plain lat/lon rectangle whose size follows from the bit
    split alone. Deriving the grid rather than probing it is what lets the
    cover be enumerated exactly instead of sampled and hoped over.
    """
    bits = 5 * precision
    lon_bits = (bits + 1) // 2
    lat_bits = bits // 2
    return lat_bits, lon_bits, 180.0 / (2**lat_bits), 360.0 / (2**lon_bits)


def geohash_cells(
    lat: float, lon: float, radius_km: float, *, precision: int, max_cells: int
) -> tuple[str, ...]:
    """The cells at *precision* covering the radius box, or ``()`` if too many.

    The ``nearby`` band's indexed prefilter: each cell is one ``LIKE 'cell%'``
    range scan, and their union provably contains the whole box, so no
    border row can fall out of ``nearby`` by accident. ``()`` is not "no
    cells" — it is "this cover is not worth its OR", and the caller falls
    back to the bounding box, which answers the same question more coarsely.

    The cell strings come from ``stapel_geo.geohash.encode`` (the module's
    one geohash implementation, verdict §18.10) applied to each cell's
    centre; base32 is never assembled here.
    """
    if precision <= 0 or max_cells <= 0 or radius_km <= 0:
        return ()
    from stapel_geo import geohash as geohash_utils

    lat_bits, lon_bits, lat_step, lon_step = _cell_grid(precision)
    lat_count, lon_count = 2**lat_bits, 2**lon_bits
    min_lat, min_lon, max_lat, max_lon = radius_bbox(lat, lon, radius_km)

    def _index(value: float, origin: float, step: float, count: int) -> int:
        return max(0, min(count - 1, int((value - origin) // step)))

    lat_lo = _index(min_lat, -90.0, lat_step, lat_count)
    lat_hi = _index(max_lat, -90.0, lat_step, lat_count)
    lon_lo = _index(min_lon, -180.0, lon_step, lon_count)
    lon_hi = _index(max_lon, -180.0, lon_step, lon_count)

    # A box crossing +/-180 wraps the longitude index range rather than
    # inverting it; the latitude range never wraps, poles being clamped.
    if min_lon > max_lon:
        lon_indexes = list(range(lon_lo, lon_count)) + list(range(0, lon_hi + 1))
    else:
        lon_indexes = list(range(lon_lo, lon_hi + 1))
    lat_indexes = list(range(lat_lo, lat_hi + 1))
    if len(lat_indexes) * len(lon_indexes) > max_cells:
        return ()

    cells = {
        geohash_utils.encode(
            -90.0 + (i + 0.5) * lat_step,
            -180.0 + (j + 0.5) * lon_step,
            precision=precision,
        )
        for i in lat_indexes
        for j in lon_indexes
    }
    return tuple(sorted(cells))


def near_radius_km(near: GeoFilter | None) -> float:
    """The ``nearby`` edge in km — the request's own value, else the default.

    Under ``geo_mode=rank`` this is where the request's ``radius_km`` went:
    the same number the caller always sent, now partitioning the answer
    instead of cutting it.
    """
    from ..conf import search_settings

    if near is not None and near.radius_km:
        return float(near.radius_km)
    return float(search_settings.NEAR_BAND_RADIUS_KM)


def band_of(lat, lon, near: GeoFilter | None) -> str:
    """``"nearby"`` | ``"all"`` | ``""`` — the LABEL every backend must agree on.

    Deliberately the opposite of :func:`geo_distance_km`'s rule for a row
    with no coordinates. That function serves a FILTER, where "within 25km"
    is a claim a coordinate-less row cannot support, so it is excluded. This
    one serves a LABEL over an answer nothing is withheld from: a row that
    cannot prove it is nearby is simply not nearby, and belongs in ``all``
    rather than nowhere.

    ``""`` means banding is inactive (no centre was given), which is a
    normal answer and not an error.
    """
    if near is None or not near.has_center:
        return ""
    flat, flon = _as_float(lat), _as_float(lon)
    if flat is None or flon is None:
        return "all"
    if haversine_km(near.lat, near.lon, flat, flon) <= near_radius_km(near):
        return "nearby"
    return "all"


# --------------------------------------------------------------------------
# the public grid
# --------------------------------------------------------------------------
#
# A public card publishes the seller's coordinates rounded to
# CARD_COORD_PRECISION decimals — one ~1.1km cell instead of a front door.
# Everything below exists so that no OTHER answer this module gives is finer
# than that cell, because a coarse card beside a fine distance is not a
# coarse answer: it is the same pin, one arithmetic step away.
#
# The rule is one sentence: **for an anonymous reader, a row's position is
# read through the grid**. Not "the distance is rounded" — rounding the
# ANSWER closes nothing, because the caller's centre and rectangle are
# continuous. Whatever the emitted number, moving the centre until the answer
# flips traces a circle of known radius around the TRUE point, and three of
# those are the point. Only putting one end of every measurement on a fixed
# grid removes the continuous probe, and the row's own position is the end
# that must be on it.


def public_precision() -> int:
    """Decimal places the public grid rounds to — the card's own setting."""
    from ..conf import search_settings

    return int(search_settings.CARD_COORD_PRECISION)


def cell_km(precision: int) -> float:
    """The side of one grid cell, in km. 1.113 at the default precision 2."""
    return round(_KM_PER_DEG * (10.0**-precision), 3)


def distance_quantum_km(precision: int) -> float:
    """The step every published ``distance_km`` is floored to.

    Derived, never chosen. The grid declares one square cell of side
    :func:`cell_km` indistinguishable, so the largest distance between two
    points it treats as the same place is that cell's DIAGONAL,
    ``side * sqrt(2)`` — 1.574km at precision 2. A quantum any finer would
    separate two points the card does not, and a difference that survives
    repetition is a position.

    Rounded so the published numbers are readable, but never to zero: a
    precision fine enough for that (10**-6 degrees is eleven centimetres) is
    not a public area at all, and the answer there is the exact value, not a
    division by it.
    """
    exact = _KM_PER_DEG * (10.0**-precision) * math.sqrt(2)
    return round(exact, 3) or exact


def grid_slack_km(precision: int) -> float:
    """How far snapping can move a point: half the cell diagonal, 0.787km.

    The number a COARSE prefilter has to be widened by. A prefilter that
    narrows on the stored column while the exact half measures the snapped
    point would drop rows the exact half would keep, and an optimisation that
    removes correct answers is a defect (``_where`` says the same thing about
    the geohash prefix).
    """
    return distance_quantum_km(precision) / 2.0


def is_precise(audience: str | None) -> bool:
    """Whether this reader gets the stored point instead of the grid.

    The audience axis is ``stapel_attributes.visibility``'s — the one that
    already decides who may read a VIN — and not a second notion of who may
    see what. ``anonymous`` is the fail-closed default, so a caller that
    never said who it is gets the grid.
    """
    from stapel_attributes import visibility

    return visibility.normalize_audience(audience) != visibility.ANONYMOUS


def snap_to_grid(value, precision: int) -> float | None:
    """The grid point *value* sits on: ``floor(v * 10**p + 0.5) / 10**p``.

    Stated as one arithmetic rule because Postgres computes it in SQL and
    this module computes it in Python, and two engines disagreeing about
    which cell a listing is in is exactly the seam defect the conformance
    suite exists for. Ties go toward +infinity in both, which ``round()``
    (banker's, in Python) and ``round(numeric)`` (half away from zero, in
    Postgres) would not have agreed about.
    """
    number = _as_decimal(value)
    if number is None:
        return None
    scale = Decimal(1).scaleb(precision)
    stepped = (number * scale + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
    return float(stepped / scale)


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def public_point(lat, lon, q: SearchQuery) -> tuple:
    """The coordinates *q*'s reader is allowed to have measured against."""
    if is_precise(getattr(q, "audience", None)):
        return lat, lon
    precision = public_precision()
    return snap_to_grid(lat, precision), snap_to_grid(lon, precision)


def query_slack_km(q: SearchQuery) -> float:
    """Widening a coarse prefilter needs, for this query. ``0`` when precise."""
    if is_precise(getattr(q, "audience", None)):
        return 0.0
    return grid_slack_km(public_precision())


def measured_distance_km(lat, lon, q: SearchQuery):
    """:func:`geo_distance_km` against the position this reader may measure.

    The MEASUREMENT: it decides the radius cut, the band and the geo decay.
    What is published is :func:`published_distance_km` of this.
    """
    flat, flon = public_point(lat, lon, q)
    return geo_distance_km(flat, flon, "", q.geo)


def published_distance_km(distance, q: SearchQuery):
    """The measurement as the answer may state it — floored to the quantum.

    Floored rather than rounded so the number is never an overstatement of
    proximity, and floored rather than left alone because a deployment whose
    cards carry no coordinates at all (this fleet's) would otherwise let the
    distance alone rebuild the area the card declines to draw.

    It also travels into the keyset cursor, so the ORDER under
    ``sort=distance`` is the order of these numbers and rows inside one
    quantum are separated by ``doc_key`` — a cursor whose anchor is coarser
    than the ordering it resumes would page unstably.
    """
    if distance is None or distance is OUT_OF_RANGE:
        return None
    value = float(distance)
    if is_precise(getattr(q, "audience", None)):
        return round(value, 6)
    quantum = distance_quantum_km(public_precision())
    if quantum <= 0:  # a precision no grid can express; nothing to floor to
        return round(value, 6)
    return round(math.floor(value / quantum) * quantum, 6)


def band_for(lat, lon, q: SearchQuery) -> str:
    """:func:`band_of` against the position this reader may measure."""
    flat, flon = public_point(lat, lon, q)
    return band_of(flat, flon, q.near)


def bbox_matches_for(lat, lon, q: SearchQuery) -> bool:
    """:func:`bbox_matches` against the position this reader may measure."""
    flat, flon = public_point(lat, lon, q)
    return bbox_matches(flat, flon, q.geo)


def grid_aligned_bbox(geo: GeoFilter, precision: int) -> GeoFilter:
    """The caller's rectangle grown OUTWARD to whole grid cells.

    A minimum box SIZE would not close the bisection: a smallest-allowed box
    is still slid continuously, and the edge at which a row enters it is the
    row's coordinate to as many decimals as the attacker cares to ask for.
    What closes it is that the box may only ever be asked about whole cells —
    then "is the row in this box" is a question about the PUBLISHED point,
    the answer stops changing below one cell, and the halving stalls. The
    minimum box falls out of that for free: the smallest expressible box is
    one cell.

    Growing outward rather than snapping to the nearest edge keeps a viewport
    honest — a row a client can see on its map does not vanish because the
    box shrank under it.
    """
    if geo is None or not geo.is_bbox:
        return geo
    step = Decimal(1).scaleb(-precision)
    half = Decimal("0.5")

    def _floor(number: Decimal) -> Decimal:
        return number.to_integral_value(rounding=ROUND_FLOOR)

    def lower(value) -> Decimal:
        """The largest cell boundary at or below *value*."""
        return (_floor(_as_decimal(value) / step - half) + half) * step

    def upper(value) -> Decimal:
        """The smallest cell boundary at or above *value*."""
        return (-_floor(half - _as_decimal(value) / step) + half) * step

    min_lat, max_lat = lower(geo.min_lat), upper(geo.max_lat)
    min_lon, max_lon = lower(geo.min_lon), upper(geo.max_lon)
    # A box whose two edges landed on the same boundary is empty, not one
    # cell; the smallest box this grid can express is a whole cell.
    if max_lat - min_lat < step:
        max_lat = min_lat + step
    if not geo.crosses_antimeridian and max_lon - min_lon < step:
        max_lon = min_lon + step
    return replace(
        geo,
        min_lat=max(-90.0, float(min_lat)),
        max_lat=min(90.0, float(max_lat)),
        min_lon=max(-180.0, float(min_lon)),
        max_lon=min(180.0, float(max_lon)),
    )


def padded_bbox(geo: GeoFilter, slack_km: float) -> tuple[float, float, float, float]:
    """A rectangle grown by *slack_km* on every side, clamped to the planet.

    What an engine that filters on the STORED point needs when membership is
    decided against a snapped one (``backends/meili.py``): the engine half is
    a prefilter and must never drop a row the exact half would keep.
    """
    min_lat, min_lon = float(geo.min_lat), float(geo.min_lon)
    max_lat, max_lon = float(geo.max_lat), float(geo.max_lon)
    if slack_km <= 0:
        return min_lat, min_lon, max_lat, max_lon
    d_lat = slack_km / _KM_PER_DEG
    widest = max(abs(min_lat), abs(max_lat))
    cos_lat = math.cos(math.radians(max(-89.9, min(89.9, widest))))
    d_lon = 180.0 if cos_lat < 1e-6 else min(180.0, slack_km / (_KM_PER_DEG * cos_lat))
    return (
        max(-90.0, min_lat - d_lat),
        max(-180.0, min_lon - d_lon),
        min(90.0, max_lat + d_lat),
        min(180.0, max_lon + d_lon),
    )


def coarse_coordinates(lat, lon, precision: int) -> tuple[float | None, float | None, float]:
    """A card's coordinates as an AREA, plus how wide that area is.

    ``(lat, lon, precision_km)`` with the pair on the public grid, so a card
    draws a neighbourhood and never the seller's pin. It is the SAME grid
    every geo answer is measured on (:func:`snap_to_grid`), which is what
    makes the card and the distance agree about which cell a listing is in
    instead of describing two differently-aligned areas around one point.
    """
    return snap_to_grid(lat, precision), snap_to_grid(lon, precision), cell_km(precision)


def match_count(facet_terms, signals: tuple[tuple[str, str], ...]) -> int:
    """How many of the query's ``(slug, value)`` signals this row satisfies.

    Counted against ``facet_terms`` — the same structure a facet filter
    matches and a facet count unnests — so "the row satisfies the signal"
    means exactly what "the row passes the filter" means. Both sides are
    deduplicated: a signal repeated twice is one thing the query is about.
    """
    if not signals:
        return 0
    terms = {str(term) for term in (facet_terms or [])}
    return len({f"{slug}={value}" for slug, value in signals} & terms)


def geohash_prefix(geo: GeoFilter | None, *, slack_km: float = 0.0) -> str:
    """Coarse indexed prefilter: the geohash cell containing the whole box.

    A geohash cell IS a latitude/longitude rectangle, so if all four corners
    of the search box share a prefix, every point inside the box shares it
    too — the prefilter cannot drop a border document, which is precisely
    what its round-trip test asserts. When the box straddles a top-level
    cell boundary the common prefix is empty and the prefilter simply
    contributes nothing; the lat/lon range still answers correctly.
    """
    box = bbox_of(geo, slack_km=slack_km)
    if box is None:
        return ""
    min_lat, min_lon, max_lat, max_lon = box
    if min_lon > max_lon:  # antimeridian: no useful common cell
        return ""
    from stapel_geo import geohash as geohash_utils

    corners = [
        geohash_utils.encode(min_lat, min_lon, precision=12),
        geohash_utils.encode(min_lat, max_lon, precision=12),
        geohash_utils.encode(max_lat, min_lon, precision=12),
        geohash_utils.encode(max_lat, max_lon, precision=12),
    ]
    prefix: list[str] = []
    for chars in zip(*corners):
        if len(set(chars)) != 1:
            break
        prefix.append(chars[0])
    return "".join(prefix)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def combined_score(*, row, sort: str, text_score: float, distance_km) -> float:
    """``sum(weight_i * f_i)`` over the scorers active for *sort*.

    The module invariant "an explicit sort gets no promotion boost" is not
    checked here — it is structural: ``promotion_boost.applies_to_sorts``
    contains ``relevance`` and nothing else, so under any other sort the
    scorer simply is not in the loop. There is no setting that turns this
    on, and there is no third place to keep in sync.
    """
    from ..registry import get_scorers

    total = 0.0
    for scorer in get_scorers().values():
        if sort not in scorer.applies_to_sorts:
            continue
        total += scorer.weight * _scorer_value(scorer, row, text_score, distance_km)
    return total


def _scorer_value(scorer, row, text_score: float, distance_km) -> float:
    slug = scorer.slug
    if slug == "relevance":
        return float(text_score or 0.0)
    if slug == "promotion_boost":
        # Clamped at the seam, not at the emitter: a signal producer that
        # writes 10**9 must not be able to own the whole result page.
        return max(-1.0, min(5.0, float(row.boost or 0.0)))
    if slug == "popularity":
        popularity = max(0, int(row.popularity or 0))
        return popularity / (popularity + 10.0)
    if slug == "freshness_decay":
        published = getattr(row, "published_at", None)
        if not published:
            return 0.0
        half_life = float(scorer.params.get("half_life_days") or 14)
        age_days = max(0.0, (_now() - published).total_seconds() / 86400.0)
        return 0.5 ** (age_days / half_life) if half_life > 0 else 0.0
    if slug == "geo_decay":
        if distance_km in (None, OUT_OF_RANGE):
            return 0.0
        max_radius = float(scorer.params.get("max_radius_km") or 50)
        if max_radius <= 0:
            return 0.0
        return max(0.0, 1.0 - float(distance_km) / max_radius)
    # A host-registered scorer with no evaluator here contributes nothing and
    # is reported as unsupported through capabilities().supported_scorers.
    return 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# ordering and keyset pagination
# --------------------------------------------------------------------------

#: Sorts whose value descends. The tie-break is ALWAYS ``doc_key`` ascending
#: — without a total order a keyset cursor is not stable.
_DESCENDING = frozenset({"relevance", "newest", "price_desc"})


def sort_value_of(row, sort: str, score: float, distance_km):
    """The value the cursor carries for this sort — JSON-serializable."""
    if sort == "relevance":
        return round(float(score), 6)
    if sort == "newest":
        return row.published_at.isoformat() if row.published_at else None
    if sort in ("price_asc", "price_desc"):
        return None if row.price_base is None else str(row.price_base)
    if sort == "distance":
        return None if distance_km in (None, OUT_OF_RANGE) else round(float(distance_km), 6)
    return None


def _comparable(sort: str, value):
    """A single ascending scalar for *value*, or ``None`` for a null."""
    if value is None:
        return None
    if sort == "newest":
        return datetime.fromisoformat(value).timestamp()
    if sort in ("price_asc", "price_desc"):
        return float(Decimal(value))
    return float(value)


#: Band -> its position in the answer. ``""`` (banding inactive) ranks with
#: ``nearby`` so an inactive band is a no-op rather than a reordering.
BAND_RANKS = {"nearby": 0, "": 0, "all": 1}

#: Tag of a composite ``sort_value``. A plain sort value is a float, a
#: string or ``None``, so a tagged list cannot be mistaken for one.
_COMPOSITE = "b"


def leading_keys(q: SearchQuery) -> tuple[bool, bool]:
    """``(band leads, match_count leads)`` for this query's ORDER BY.

    Both are OFF by default, and with both off the order — and therefore
    every byte of the answer — is what it was before bands existed.
    """
    return (q.near is not None and q.near.has_center, bool(q.signals))


def banded_sort_value(band: str, matches: int, value):
    """The cursor payload when a band or signal strength leads the order.

    The composite travels INSIDE ``sort_value`` rather than beside it,
    because the cursor is one opaque ``(sort_value, doc_key)`` pair a
    frontend round-trips verbatim; the published contract is one
    ``next_anchor`` and one ``items`` list, and it stays that way.

    The band half is not a sort key an engine orders by — Postgres executes
    the two bands as two indexable queries and concatenates them — it is
    the answer to "which band does this cursor resume in, and at what
    anchor". Carrying it here is what lets a single cursor walk out of
    ``nearby`` into ``all``: the boundary is not a page boundary and
    must not become one.
    """
    return [_COMPOSITE, BAND_RANKS.get(band, 1), int(matches), value]


def split_sort_value(value) -> tuple[int | None, int | None, object]:
    """``(band_rank, match_count, base value)``; the first two are ``None``
    for a plain, unbanded cursor."""
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and value[0] == _COMPOSITE
    ):
        return int(value[1]), int(value[2]), value[3]
    return None, None, value


def order_key(sort: str, value, doc_key: str) -> tuple:
    """Total ascending order key: ``(nulls-last flag, value, doc_key)``.

    NULLs sort last in BOTH directions — "no price" is not "the cheapest"
    and it is not "the dearest" either; it is unknown, and unknown belongs
    at the end whichever way the user pointed the arrow.

    A composite *value* (see :func:`banded_sort_value`) prepends the band
    position ascending and the signal match count descending, in that order.
    This is the ORDER of the answer, stated once so both engines produce
    it; how an engine reaches that order is its own business (Postgres runs
    the two bands as two indexed queries and concatenates them, this
    backend sorts one materialized list).
    """
    rank, matches, base = split_sort_value(value)
    scalar = _comparable(sort, base)
    tail = (
        (1, 0.0, doc_key)
        if scalar is None
        else (0, -scalar if sort in _DESCENDING else scalar, doc_key)
    )
    if rank is None:
        return tail
    return (rank, -matches) + tail


def paginate(rows: list[tuple], q: SearchQuery) -> tuple[list[tuple], bool, bool]:
    """Slice *rows* (already ordered, ``order_key`` first) around the cursor."""
    start = 0
    if q.cursor is not None:
        anchor = order_key(q.sort, q.cursor.sort_value, q.cursor.doc_key)
        if q.direction == "prev":
            before = [i for i, item in enumerate(rows) if item[0] < anchor]
            end = before[-1] + 1 if before else 0
            start = max(0, end - q.limit)
            page = rows[start:end]
            return page, True, start > 0
        start = next((i for i, item in enumerate(rows) if item[0] > anchor), len(rows))
    page = rows[start: start + q.limit]
    return page, start + q.limit < len(rows), start > 0


__all__ = [
    "BAND_RANKS",
    "OUT_OF_RANGE",
    "band_for",
    "band_of",
    "banded_sort_value",
    "bbox_matches",
    "bbox_matches_for",
    "bbox_of",
    "category_matches",
    "cell_km",
    "coarse_coordinates",
    "combined_score",
    "distance_quantum_km",
    "facet_terms_for",
    "facets_match",
    "geo_distance_km",
    "geohash_cells",
    "geohash_prefix",
    "grid_aligned_bbox",
    "grid_slack_km",
    "is_precise",
    "leading_keys",
    "haversine_km",
    "match_count",
    "measured_distance_km",
    "narrow_by_ranges",
    "near_radius_km",
    "padded_bbox",
    "public_point",
    "public_precision",
    "published_distance_km",
    "query_slack_km",
    "snap_to_grid",
    "split_ranges",
    "split_sort_value",
    "order_key",
    "paginate",
    "parse_range",
    "radius_bbox",
    "sort_value_of",
]
