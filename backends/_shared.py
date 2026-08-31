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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

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


def bbox_of(geo: GeoFilter) -> tuple[float, float, float, float] | None:
    """The rectangle this filter narrows to, radius filters included."""
    if geo is None:
        return None
    if geo.is_bbox:
        return (geo.min_lat, geo.min_lon, geo.max_lat, geo.max_lon)
    if geo.has_center and geo.radius_km:
        return radius_bbox(geo.lat, geo.lon, geo.radius_km)
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

    ``None`` means no centre was given — a document with no coordinates is
    *excluded* from a radius query rather than sorted last, because "within
    25km" is a claim, and a row that cannot support it must not be in the
    answer.
    """
    if geo is None or not geo.has_center:
        return None
    flat, flon = _as_float(lat), _as_float(lon)
    if flat is None or flon is None:
        return OUT_OF_RANGE
    distance = haversine_km(geo.lat, geo.lon, flat, flon)
    if geo.radius_km is not None and distance > geo.radius_km:
        return OUT_OF_RANGE
    return distance


def geohash_prefix(geo: GeoFilter | None) -> str:
    """Coarse indexed prefilter: the geohash cell containing the whole box.

    A geohash cell IS a latitude/longitude rectangle, so if all four corners
    of the search box share a prefix, every point inside the box shares it
    too — the prefilter cannot drop a border document, which is precisely
    what its round-trip test asserts. When the box straddles a top-level
    cell boundary the common prefix is empty and the prefilter simply
    contributes nothing; the lat/lon range still answers correctly.
    """
    box = bbox_of(geo)
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


def order_key(sort: str, value, doc_key: str) -> tuple:
    """Total ascending order key: ``(nulls-last flag, value, doc_key)``.

    NULLs sort last in BOTH directions — "no price" is not "the cheapest"
    and it is not "the dearest" either; it is unknown, and unknown belongs
    at the end whichever way the user pointed the arrow.
    """
    scalar = _comparable(sort, value)
    if scalar is None:
        return (1, 0.0, doc_key)
    return (0, -scalar if sort in _DESCENDING else scalar, doc_key)


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
    "OUT_OF_RANGE",
    "bbox_matches",
    "bbox_of",
    "category_matches",
    "combined_score",
    "facet_terms_for",
    "facets_match",
    "geo_distance_km",
    "geohash_prefix",
    "haversine_km",
    "narrow_by_ranges",
    "split_ranges",
    "order_key",
    "paginate",
    "parse_range",
    "radius_bbox",
    "sort_value_of",
]
