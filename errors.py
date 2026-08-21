"""i18n error keys of stapel-search.

Only ``error.<status>.<slug>`` keys leave this package — a response never
carries a human-readable string, because the reader's language is not
known here. The ``translations/errors.{ru,es}.json`` catalogues ship in the
first release: shipping keys without catalogues is the stapel-docs lesson,
and there is nothing to gain by repeating it.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_UNKNOWN_DOC_TYPE = "error.400.search_unknown_doc_type"
ERR_400_UNKNOWN_SORT = "error.400.search_unknown_sort"
ERR_400_SORT_NEEDS_CENTER = "error.400.search_sort_needs_center"
ERR_400_BAD_RANGE = "error.400.search_bad_range"
ERR_400_BAD_GEO = "error.400.search_bad_geo"
ERR_400_QUERY_TOO_LONG = "error.400.search_query_too_long"
ERR_400_TOO_MANY_FACETS = "error.400.search_too_many_facets"
ERR_400_TOO_MANY_RANGES = "error.400.search_too_many_ranges"
ERR_400_WINDOW_EXCEEDED = "error.400.search_window_exceeded"
ERR_400_BAD_CURSOR = "error.400.search_bad_cursor"
ERR_403_FORBIDDEN_MANAGE = "error.403.search_forbidden"
ERR_503_BACKEND_UNAVAILABLE = "error.503.search_backend_unavailable"

STAPEL_SEARCH_ERRORS = {
    ERR_400_UNKNOWN_DOC_TYPE: "Unknown search type '{doc_type}'",
    ERR_400_UNKNOWN_SORT: "Unknown sort '{sort}'",
    ERR_400_SORT_NEEDS_CENTER: "Sorting by distance needs lat and lon",
    ERR_400_BAD_RANGE: "Range filter '{slug}' is malformed: {reason}",
    ERR_400_BAD_GEO: "Geographic filter is malformed: {reason}",
    ERR_400_QUERY_TOO_LONG: "The search text is too long",
    ERR_400_TOO_MANY_FACETS: "Too many facet filters (limit {limit})",
    ERR_400_TOO_MANY_RANGES: "Too many range filters (limit {limit})",
    ERR_400_WINDOW_EXCEEDED: (
        "This result page is beyond the maximum window of {window}. Narrow the "
        "search instead of paging deeper."
    ),
    ERR_400_BAD_CURSOR: "The pagination cursor is not valid",
    ERR_403_FORBIDDEN_MANAGE: "You may not manage the search index",
    ERR_503_BACKEND_UNAVAILABLE: "The search engine is unavailable",
}

register_service_errors(STAPEL_SEARCH_ERRORS, owner="stapel_search")


class SearchValidationError(ValueError):
    """A request the query contract refuses, carrying its i18n key."""

    status_code = 400

    def __init__(self, code: str, **params):
        self.code = code
        self.params = params
        super().__init__(code)


class SearchBackendUnavailable(RuntimeError):
    """The configured engine could not answer."""

    status_code = 503
    code = ERR_503_BACKEND_UNAVAILABLE


__all__ = [
    "ERR_400_BAD_CURSOR",
    "ERR_400_BAD_GEO",
    "ERR_400_BAD_RANGE",
    "ERR_400_QUERY_TOO_LONG",
    "ERR_400_SORT_NEEDS_CENTER",
    "ERR_400_TOO_MANY_FACETS",
    "ERR_400_TOO_MANY_RANGES",
    "ERR_400_UNKNOWN_DOC_TYPE",
    "ERR_400_UNKNOWN_SORT",
    "ERR_400_WINDOW_EXCEEDED",
    "ERR_403_FORBIDDEN_MANAGE",
    "ERR_503_BACKEND_UNAVAILABLE",
    "STAPEL_SEARCH_ERRORS",
    "SearchBackendUnavailable",
    "SearchValidationError",
]
