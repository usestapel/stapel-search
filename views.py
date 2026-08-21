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
    OpenApiParameter("radius_km", float, description="Radius around the centre."),
    OpenApiParameter("bbox", str, description="minLat,minLon,maxLat,maxLon. minLon > maxLon means the box crosses +/-180."),
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


@extend_schema(
    parameters=[
        OpenApiParameter("type", str, required=True),
        OpenApiParameter("q", str, description="Title prefix."),
        OpenApiParameter("limit", int),
    ],
    responses={200: SuggestResponseSerializer},
    summary="Title prefixes from the index",
)
class SearchSuggestView(APIView):
    """``GET /search/api/v1/suggest`` — title prefixes out of the index.

    Not out of a query log: no query log is kept, which is a privacy
    decision before it is a product one, and on day one there would be
    nothing in it anyway.
    """

    permission_classes = [permissions.AllowAny]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    throttle_classes = [SuggestThrottle]
    throttle_scope = "search-suggest"

    def get(self, request):
        from .services import suggest

        return _handle(lambda: suggest(request.query_params))


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
