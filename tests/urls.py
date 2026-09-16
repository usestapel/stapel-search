"""Bare test mount: the module at the root, no host prefix.

The canonical prefix mount lives in ``codegen_urls.py`` so the contract
emission cannot drift from the recipe ``urls.py`` documents.
"""
from django.urls import include, path

urlpatterns = [
    # The BARE mount, kept: this module is designed to work under any host
    # prefix, and every existing test here addresses it this way.
    path("", include("stapel_search.urls")),
    # …and the mount the CONTRACT is emitted at (codegen_urls.py). Without it
    # not one path in the committed docs/schema.json resolved under this
    # urlconf, so nothing in this repository had ever driven the document it
    # ships — the suite was looking somewhere the contract does not describe.
    # `tests/test_contract_wire.py` drives this one.
    path("search/", include("stapel_search.urls")),
]
