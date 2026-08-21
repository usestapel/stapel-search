"""Version 1 routes, plus the gate registry the capabilities emitter reads.

``GATE_REGISTRY`` maps a mountable block to the URL patterns it contains.
This module has exactly one block and no feature flags: a search library
whose query endpoint can be switched off is a library nobody can depend on.
The registry exists so ``docs/capabilities.json`` can say which settings
gate which operations — and the honest answer here is "none of them".
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    SearchHealthView,
    SearchQueryView,
    SearchRankingView,
    SearchReindexView,
    SearchSuggestView,
)

urlpatterns = [
    path("query", SearchQueryView.as_view(), name="search-query"),
    path("suggest", SearchSuggestView.as_view(), name="search-suggest"),
    path("ranking", SearchRankingView.as_view(), name="search-ranking"),
    path("health", SearchHealthView.as_view(), name="search-health"),
    path("reindex", SearchReindexView.as_view(), name="search-reindex"),
]


class GateEntry(NamedTuple):
    """One mountable block. Declared here, not imported from stapel-tools:
    runtime code does not depend on the fleet's tooling."""

    name: str
    flags: tuple
    patterns: tuple


GATE_REGISTRY: dict = {
    "search.api": GateEntry("search.api", (), tuple(urlpatterns)),
}
