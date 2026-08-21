"""Bare test mount: the module at the root, no host prefix.

The canonical prefix mount lives in ``codegen_urls.py`` so the contract
emission cannot drift from the recipe ``urls.py`` documents.
"""
from django.urls import include, path

urlpatterns = [
    path("", include("stapel_search.urls")),
]
