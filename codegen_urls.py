"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

Mounts the module root at ``search/``; ``urls.py`` bakes in ``api/v1/``, so
the emitted paths are ``/search/api/v1/...`` — exactly the mount recipe
``urls.py`` documents for hosts. Declared separately from the test urlconf
so the emission mount can never silently drift from the documented one.
"""
from django.urls import include, path

urlpatterns = [
    path("search/", include("stapel_search.urls")),
]
