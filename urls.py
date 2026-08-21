"""Public URL surface of stapel-search.

Mount recipe for a host (api-versioning.md §2: the ``api/v1`` segment is
baked in HERE, so a host mounts the module root and gets the canonical
prefix)::

    path("search/", include("stapel_search.urls")),
    # -> /search/api/v1/query, /search/api/v1/suggest, ...
"""
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("stapel_search.urls_v1")),
]
