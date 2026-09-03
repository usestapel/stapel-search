"""HTTP surface: three public verbs and two operator verbs.

``query`` / ``suggest`` / ``ranking`` are ``AllowAny`` with an explicit
``stapel_anonymous_access = ANONYMOUS_ALLOWED`` declaration — the core
adoption check treats silence as the red state, so the permission is
stated, not defaulted into. They are bounded by throttle scopes whose rates
come from this module's own settings namespace: a library does not own the
project's ``DEFAULT_THROTTLE_RATES``.

``ranking`` exists in v1 on purpose (verdict §18.8). It costs almost
nothing because it is rendered from the scorer registry that does the
ranking, and adding it later would mean retrospectively explaining a
ranking that had already been shipping.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsNotAnonymousUser,
)

from .errors import (
    ERR_403_FORBIDDEN_MANAGE,
    SearchBackendUnavailable,
    SearchValidationError,
)
from .serializers import (
    HealthResponseSerializer,
    RankingResponseSerializer,
    ReindexRequestSerializer,
    ReindexResponseSerializer,
    SearchResponseSerializer,
    SuggestResponseSerializer,
)

_QUERY_PARAMS = [
    OpenApiParameter("type", str, required=True, description="Registered doc_type. One type per query — federated search across types is not in v1."),
    OpenApiParameter("q", str, description="Free text. Dictionary-normalized here; morphology belongs to the engine."),
    OpenApiParameter("lang", str, description="Language of the query: selects the analyzer AND narrows the corpus. Omit it and only the analyzer is chosen (from Accept-Language) — a header must not hide a catalogue."),
    OpenApiParameter("category", str, description="root/leaf path; a prefix filter, so a parent finds its descendants."),
    OpenApiParameter("owner", str, description="Opaque owner key — the seller's own listings."),
    OpenApiParameter("f.<slug>", str, description="Facet filter. Repeat for OR within a slug; different slugs AND together."),
    OpenApiParameter("r.<slug>", str, description="Range filter, `from..to`; either end may be omitted."),
    OpenApiParameter("lat", float, description="Centre latitude (with lon)."),
    OpenApiParameter("lon", float, description="Centre longitude (with lat)."),
    OpenApiParameter("radius_km", float, description="Distance around the centre, in km. Its MEANING is `geo_mode`'s: under `rank` it is the edge of the `nearby` band and excludes nothing; under `filter` it is a hard cut. Defaults to NEAR_BAND_RADIUS_KM under `rank`."),
    OpenApiParameter("bbox", str, description="minLat,minLon,maxLat,maxLon. minLon > maxLon means the box crosses +/-180."),
    OpenApiParameter("geo_mode", str, description="rank | filter. What `radius_km` means. `rank` PARTITIONS: the answer comes back as `nearby` (inside the radius) then `all` (every remaining row), nothing is withheld, and `count` stays the whole matching total — a query can never come back empty because of distance. `filter` is the historical hard cut, for a caller that genuinely wants only what is within N km. `rank` is the default inside the feature; while STAPEL_SEARCH['GEO_BANDS'] is off this parameter is inert and `radius_km` filters as it always has."),
    OpenApiParameter("qu", str, description="auto | off. Whether the query's own words may become filters. `auto` (the default while QUERY_UNDERSTANDING is on) extracts and reports what it extracted under `query_understanding`; each filter carries the literal `param` to replay. Send `qu=off` with those replayed params afterwards — extraction is stateless, so without it a removed chip comes straight back."),
    OpenApiParameter("sort", str, description="relevance | newest | price_asc | price_desc | distance. An explicit sort never receives a promotional boost."),
    OpenApiParameter("facets", str, description="on | off | comma-separated slugs. Default is the category's plan."),
    OpenApiParameter("limit", int),
    OpenApiParameter("anchor", str, description="Opaque keyset cursor from a previous answer."),
    OpenApiParameter("direction", str, description="next | prev."),
]


class _SettingsRateThrottle(ScopedRateThrottle):
    """Scoped throttle whose rate comes from ``STAPEL_SEARCH``, not the project."""

    settings_key = ""

    def get_rate(self):
        from .conf import search_settings

        return getattr(search_settings, self.settings_key)


class QueryThrottle(_SettingsRateThrottle):
    settings_key = "QUERY_THROTTLE"


class SuggestThrottle(_SettingsRateThrottle):
    settings_key = "SUGGEST_THROTTLE"


def _handle(fn):
    """Run *fn*, turning the module's two error families into responses."""
    try:
        return StapelResponse(fn())
    except SearchValidationError as exc:
        return StapelErrorResponse(400, exc.code, exc.params or None)
    except SearchBackendUnavailable:
        from .errors import ERR_503_BACKEND_UNAVAILABLE

        return StapelErrorResponse(503, ERR_503_BACKEND_UNAVAILABLE)


@extend_schema(
    parameters=_QUERY_PARAMS,
    responses={200: SearchResponseSerializer},
    summary="Search one registered document type",
)
class SearchQueryView(APIView):
    """``GET /search/api/v1/query`` — the whole read contract."""

    permission_classes = [permissions.AllowAny]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    throttle_classes = [QueryThrottle]
    throttle_scope = "search-query"

    def get(self, request):
        from .services import search

        return _handle(
            lambda: search(
                request.query_params,
                accept_language=request.headers.get("Accept-Language", ""),
            )
        )


def _suggest_etag(data) -> str:
    """A weak validator over the whole answer.

    Derived from the payload rather than from a version counter because the
    answer is a JOIN of two things that move independently — the category
    tree and the live listing counts — and there is no single number that
    advances when either does. Hashing what was actually served cannot get
    that wrong, and the answer is small.

    Nothing time-varying is in the body (no ``took_ms``), which is what
    makes the hash stable enough to be worth sending: an ETag that changes
    on every request is a header that costs bytes and saves none.
    """
    import hashlib
    import json

    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return 'W/"%s"' % hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@extend_schema(
    parameters=[
        OpenApiParameter(
            "type",
            str,
            description="Registered doc_type. Optional when exactly one type is "
            "registered — a type-ahead should not have to name the only corpus "
            "there is.",
        ),
        OpenApiParameter("q", str, description="What the buyer has typed so far."),
        OpenApiParameter(
            "lang",
            str,
            description="Language of the query: picks the dictionary, so «shorty» "
            "reaches «шорты». Falls back to Accept-Language, then DEFAULT_LANGUAGE.",
        ),
        OpenApiParameter("limit", int, description="Rows per half. Capped by MAX_SUGGEST_LIMIT."),
    ],
    responses={200: SuggestResponseSerializer},
    summary="Type-ahead: category paths with live counts, plus title prefixes",
)
class SearchSuggestView(APIView):
    """``GET /search/api/v1/suggest`` — what to offer under the search box.

    ``categories`` is the primary half: each row is a destination with its
    full ancestor path and the number of listings a buyer would actually
    see there, ranked by that number. ``terms`` is the title-prefix half.

    Neither comes from a query log: no query log is kept, which is a privacy
    decision before it is a product one, and on day one there would be
    nothing in it anyway.

    The answer is public, identical for every reader and requested on every
    keystroke, so it carries ``Cache-Control: public`` and an ``ETag``. This
    is the module's first conditional read — ``query`` has none, because a
    SERP answer embeds ``took_ms`` and a cursor and would revalidate to a
    miss every time. Here the payload is deliberately free of anything that
    varies with the clock.
    """

    permission_classes = [permissions.AllowAny]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    throttle_classes = [SuggestThrottle]
    throttle_scope = "search-suggest"

    def get(self, request):
        from rest_framework.response import Response

        from .conf import search_settings
        from .services import suggest

        response = _handle(
            lambda: suggest(
                request.query_params,
                accept_language=request.headers.get("Accept-Language", ""),
            )
        )
        if response.status_code != 200:
            return response

        etag = _suggest_etag(response.data)
        cache_control = f"public, max-age={int(search_settings.SUGGEST_CACHE_SECONDS)}"
        if request.headers.get("If-None-Match") == etag:
            response = Response(status=304)
        response["ETag"] = etag
        response["Cache-Control"] = cache_control
        return response


@extend_schema(
    parameters=[OpenApiParameter("type", str)],
    responses={200: RankingResponseSerializer},
    summary="Ranking disclosure (P2B Art. 5), generated from the scorer registry",
)
class SearchRankingView(APIView):
    """``GET /search/api/v1/ranking`` — the P2B Art. 5 disclosure.

    Generated from the scorer registry and annotated with what the
    configured engine can actually evaluate, so it cannot claim a parameter
    is in effect when the backend cannot compute it.
    """

    permission_classes = [permissions.AllowAny]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    throttle_classes = [QueryThrottle]
    throttle_scope = "search-query"

    def get(self, request):
        from .backends import get_backend
        from .scoring import ranking_disclosure

        def run():
            backend = get_backend()
            try:
                supported = backend.capabilities().supported_scorers
                name = getattr(backend, "name", "unknown")
            except Exception:  # noqa: BLE001 - a dead engine still owes a disclosure
                supported, name = None, getattr(backend, "name", "unknown")
            return ranking_disclosure(
                str(request.query_params.get("type") or ""),
                backend_name=name,
                supported=supported,
            )

        return _handle(run)


@extend_schema(
    responses={200: HealthResponseSerializer},
    summary="Engine reachability, capabilities and index lag",
)
class SearchHealthView(APIView):
    """``GET /search/api/v1/health`` — backend, capabilities, index lag."""

    permission_classes = [IsNotAnonymousUser]
    stapel_anonymous_access = ANONYMOUS_DENIED

    def get(self, request):
        from .authz import can_manage
        from .services import health

        if not can_manage(request):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_MANAGE)
        return _handle(health)


@extend_schema(
    request=ReindexRequestSerializer,
    responses={200: ReindexResponseSerializer},
    summary="Re-pull specific keys, or rebuild a whole document type",
)
class SearchReindexView(APIView):
    """``POST /search/api/v1/reindex`` — targeted re-pull or a full rebuild."""

    permission_classes = [IsNotAnonymousUser]
    stapel_anonymous_access = ANONYMOUS_DENIED

    def post(self, request):
        from .authz import can_manage
        from .errors import ERR_400_UNKNOWN_DOC_TYPE
        from .registry import get_sources
        from .services import ingest, rebuild

        if not can_manage(request):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_MANAGE)

        data = request.data or {}
        doc_type = str(data.get("doc_type") or "")
        if doc_type not in get_sources():
            return StapelErrorResponse(
                400, ERR_400_UNKNOWN_DOC_TYPE, {"doc_type": doc_type}
            )
        keys = data.get("keys") or []

        def run():
            report = ingest(doc_type, keys) if keys else rebuild(doc_type)
            return {
                "doc_type": doc_type,
                "indexed": report.indexed,
                "removed": report.removed,
                "skipped_stale": report.skipped_stale,
                "skipped_duplicate": report.skipped_duplicate,
            }

        return _handle(run)


__all__ = [
    "QueryThrottle",
    "SearchHealthView",
    "SearchQueryView",
    "SearchRankingView",
    "SearchReindexView",
    "SearchSuggestView",
    "SuggestThrottle",
]
