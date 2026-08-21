"""Parsing and validating a query — the whole HTTP contract, in one place.

Everything a caller can express lives here as parsing rules, so the views
stay thin and the same contract is reachable from the comm Function
(``search.query``) without going through HTTP. Every refusal is an
``error.400.search_*`` key, never a bare 500 and never a silently ignored
parameter: a filter the server dropped without saying so is the most
expensive kind of wrong answer, because the page looks right.

The pagination envelope is deliberately identical to
``stapel_core.django.api.pagination.AnchorPagination``
(``items/next_anchor/prev_anchor/has_next/has_prev/count``) with the same
``limit``/``direction`` parameter names. What differs is the cursor's
contents: ``AnchorPagination`` anchors on ONE model field, and relevance is
not a column in any engine, so the anchor here is an opaque
``(sort_value, doc_key)`` pair. A frontend paging hook needs no special
case for search — that was the requirement, and it is met by keeping the
envelope, not by pretending relevance is a column.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Mapping

from .dto import Cursor, GeoFilter, RangeFilter, SearchQuery
from .errors import (
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

FACET_PREFIX = "f."
RANGE_PREFIX = "r."


# --------------------------------------------------------------------------
# cursor codec
# --------------------------------------------------------------------------


def encode_cursor(cursor: Cursor) -> str:
    payload = json.dumps(
        {"v": cursor.sort_value, "k": cursor.doc_key, "o": cursor.offset},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> Cursor:
    if not raw:
        raise SearchValidationError(ERR_400_BAD_CURSOR)
    padded = raw + "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return Cursor(
            sort_value=payload["v"], doc_key=str(payload["k"]), offset=int(payload.get("o") or 0)
        )
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise SearchValidationError(ERR_400_BAD_CURSOR) from None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _multi(params: Mapping[str, Any], key: str) -> list[str]:
    """Repeated query parameters, whichever mapping flavour we were given."""
    getlist = getattr(params, "getlist", None)
    if getlist is not None:
        return [v for v in getlist(key) if v not in (None, "")]
    value = params.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [str(value)]


def _float(params: Mapping[str, Any], key: str) -> float | None:
    raw = params.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise SearchValidationError(ERR_400_BAD_GEO, reason=f"{key} is not a number") from None


def parse_geo(params: Mapping[str, Any]) -> GeoFilter | None:
    """``lat``/``lon``/``radius_km`` or ``bbox=minLat,minLon,maxLat,maxLon``."""
    bbox_raw = params.get("bbox")
    if bbox_raw:
        parts = [p.strip() for p in str(bbox_raw).split(",")]
        if len(parts) != 4:
            raise SearchValidationError(
                ERR_400_BAD_GEO, reason="bbox needs minLat,minLon,maxLat,maxLon"
            )
        try:
            min_lat, min_lon, max_lat, max_lon = (float(p) for p in parts)
        except ValueError:
            raise SearchValidationError(ERR_400_BAD_GEO, reason="bbox values must be numbers") from None
        if min_lat > max_lat:
            raise SearchValidationError(
                ERR_400_BAD_GEO, reason="bbox min latitude is above the max"
            )
        # min_lon > max_lon is NOT an error: it means the box crosses +/-180,
        # the contract borrowed verbatim from GeoSearchBackend.bbox.
        return GeoFilter(
            min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon
        )

    lat = _float(params, "lat")
    lon = _float(params, "lon")
    radius = _float(params, "radius_km")
    if lat is None and lon is None:
        return None
    if lat is None or lon is None:
        raise SearchValidationError(ERR_400_BAD_GEO, reason="lat and lon must be given together")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise SearchValidationError(ERR_400_BAD_GEO, reason="coordinates out of range")
    if radius is not None and radius <= 0:
        raise SearchValidationError(ERR_400_BAD_GEO, reason="radius_km must be positive")
    return GeoFilter(lat=lat, lon=lon, radius_km=radius)


def parse_query(params: Mapping[str, Any], *, accept_language: str = "") -> SearchQuery:
    """Turn request parameters into a validated :class:`SearchQuery`."""
    from .backends import _shared as shared
    from .conf import search_settings
    from .registry import get_sources
    from .scoring import scorer_slugs_for
    from .text import normalize_query

    doc_type = str(params.get("type") or "").strip()
    if not doc_type or doc_type not in get_sources():
        raise SearchValidationError(ERR_400_UNKNOWN_DOC_TYPE, doc_type=doc_type)

    # `lang` does two different jobs, and conflating them empties catalogues:
    # it always picks the analyzer/dictionary, and it narrows the corpus ONLY
    # when the caller typed it. Accept-Language is a hint about the reader,
    # not an instruction to hide every listing written in another language.
    explicit_language = str(params.get("lang") or "").strip()
    language = (
        explicit_language
        or (accept_language or "").split(",")[0].split("-")[0].strip()
        or str(search_settings.DEFAULT_LANGUAGE or "")
    )

    raw_text = str(params.get("q") or "").strip()
    if len(raw_text) > int(search_settings.MAX_QUERY_CHARS):
        raise SearchValidationError(ERR_400_QUERY_TOO_LONG)
    text = normalize_query(raw_text, language) if raw_text else None

    category_raw = str(params.get("category") or "").strip().strip("/")
    category_path = tuple(part for part in category_raw.split("/") if part) if category_raw else ()

    facets: dict[str, list[str]] = {}
    ranges: list[RangeFilter] = []
    for key in list(params.keys()):
        if key.startswith(FACET_PREFIX):
            slug = key[len(FACET_PREFIX):]
            values = _multi(params, key)
            if slug and values:
                facets[slug] = values
        elif key.startswith(RANGE_PREFIX):
            slug = key[len(RANGE_PREFIX):]
            raw = params.get(key)
            if not slug or raw in (None, ""):
                continue
            try:
                lower, upper = shared.parse_range(str(raw))
            except ValueError as exc:
                raise SearchValidationError(ERR_400_BAD_RANGE, slug=slug, reason=str(exc)) from None
            ranges.append(RangeFilter(slug=slug, lower=lower, upper=upper))

    max_facets = int(search_settings.MAX_FACET_FIELDS)
    if len(facets) > max_facets:
        raise SearchValidationError(ERR_400_TOO_MANY_FACETS, limit=max_facets)
    max_ranges = int(search_settings.MAX_RANGE_FILTERS)
    if len(ranges) > max_ranges:
        raise SearchValidationError(ERR_400_TOO_MANY_RANGES, limit=max_ranges)

    geo = parse_geo(params)

    sorts = tuple(search_settings.SORTS or ())
    sort = str(params.get("sort") or "").strip()
    if not sort:
        sort = "relevance" if text is not None else "newest"
    if sort not in sorts:
        raise SearchValidationError(ERR_400_UNKNOWN_SORT, sort=sort)
    if sort == "distance" and (geo is None or not geo.has_center):
        raise SearchValidationError(ERR_400_SORT_NEEDS_CENTER)

    limit = _positive_int(params.get("limit"), int(search_settings.DEFAULT_PAGE_SIZE))
    limit = max(1, min(limit, int(search_settings.MAX_PAGE_SIZE)))

    direction = str(params.get("direction") or "next").strip()
    if direction not in ("next", "prev"):
        direction = "next"

    cursor_raw = str(params.get("anchor") or "").strip()
    cursor = decode_cursor(cursor_raw) if cursor_raw else None
    window = int(search_settings.MAX_RESULT_WINDOW)
    if cursor is not None and cursor.offset + limit > window:
        # Refused, not truncated: Meilisearch has its own hard window, and
        # one explicit shared limit is what makes the engines interchangeable
        # instead of "interchangeable until page 40".
        raise SearchValidationError(ERR_400_WINDOW_EXCEEDED, window=window)

    return SearchQuery(
        doc_type=doc_type,
        language=language,
        language_filter=explicit_language,
        text=text,
        category_path=category_path,
        owner_key=str(params.get("owner") or "").strip(),
        facets=facets,
        ranges=tuple(ranges),
        geo=geo,
        sort=sort,
        limit=limit,
        cursor=cursor,
        direction=direction,
        scorers=scorer_slugs_for(sort),
    )


def parse_facet_selection(params: Mapping[str, Any]) -> tuple[str, ...] | None:
    """``facets=on|off|<slug>,<slug>`` -> requested slugs, or ``None``/``()``.

    ``None`` means "the category's plan"; ``()`` means "count nothing", which
    is what an infinite scroll asks for on page two.
    """
    raw = params.get("facets")
    if raw in (None, ""):
        return None
    value = str(raw).strip().lower()
    if value == "on":
        return None
    if value == "off":
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def _positive_int(raw: Any, default: int) -> int:
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


__all__ = [
    "FACET_PREFIX",
    "RANGE_PREFIX",
    "decode_cursor",
    "encode_cursor",
    "parse_facet_selection",
    "parse_geo",
    "parse_query",
]
