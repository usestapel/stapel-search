"""The backend protocol — what an engine must be able to do.

Structural :class:`typing.Protocol`, not an ABC: the choice mirrors
``stapel_geo.search.base.GeoSearchBackend``, and for the same reason —
this is a **single-strategy REPLACE seam** (one key, one engine), where a
duck-typed check is enough and a nominal base class would only add an
import a third-party backend does not need. (The MERGE seams of this
module — sources, facet mappings, scorers, dictionaries — are name
registries with dataclass entries, the other half of the same contrast.)

The rule inherited one level up, verbatim in intent: **backends return keys
and scores; the service layer shapes the response.** A backend never builds
a card, never resolves an option label, never decides what "promoted"
means. That is why ``card`` and ``promoted`` are served from this module's
own table in both topologies and are not part of any engine's job.

Differences between engines are not hidden — they are declared in
:class:`~stapel_search.dto.BackendCapabilities` and echoed to the caller in
``degraded[]``. A backend that quietly does less is the failure this seam
exists to prevent.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..dto import (
    BackendCapabilities,
    BackendHealth,
    FacetPlan,
    FacetResult,
    IndexDocument,
    IndexSettings,
    QueryResult,
    SearchQuery,
)

#: The verbs ``get_backend()`` and ``search.E002`` duck-type against.
VERBS = (
    "capabilities",
    "health",
    "upsert",
    "delete",
    "clear",
    "apply_settings",
    "query",
    "facets",
    "suggest",
)

#: A second OPTIONAL verb, same rule: ``category_counts(q, *, limit) ->
#: list[(category path tuple, count)]`` — which categories the CANDIDATE SET
#: of *q* is made of, busiest first. One grouped aggregate over the same
#: predicate ``facets()`` counts through, so the numbers describe the page
#: the reader is on: the category filter, the facet filters and the geo box
#: all narrow it.
#:
#: It exists because the facet PLAN cannot be drawn from the queried
#: category alone. ``categories.features`` resolves own + ANCESTOR-inherited
#: features, so a branch category owns no axes (its leaves do) and a text
#: query names no category at all — which is why a live stand offered 12
#: facet groups on ``/c/mobilnye-telefony`` and zero on ``/c/telefony``,
#: ``/c/elektronika`` and ``?q=iPhone`` over the same 46 phones (D175). See
#: ``stapel_search.facets.evidence_plan``.
#:
#: Optional for the same reason as ``suggest_categories``: an engine that
#: cannot group has not failed ``search.E002``. The service layer probes
#: with ``getattr``, answers what 0.11.x answered, and reports
#: ``facet_plan_evidence`` in ``degraded[]``.
#:
#: One OPTIONAL verb exists beside the nine and is deliberately NOT in
#: ``VERBS``: ``suggest_categories(doc_type, query, *, language, limit) ->
#: list[(category path tuple, count)]`` — which categories hold documents
#: matching *query*, busiest first, powering the goods-driven half of the
#: type-ahead. Optional because it needs the engine's own text predicate
#: (the reason it crosses the seam at all — see ``suggest.py``) and an
#: engine that has not implemented it yet must not fail ``search.E002``: the
#: service layer probes with ``getattr``, answers what 0.9.0 answered, and
#: reports ``category_listing_suggestions`` in ``degraded[]``. Naive and
#: Postgres implement it; the conformance scenario ``suggest_categories``
#: skips engines without it and holds the rest to the naive reference.

#: A THIRD optional verb, same rule: ``ranges(q, plan) -> {slug: (low,
#: high)}`` — the bounds of every numeric axis over the candidate set, with
#: the range filters removed. A bucket list answers "which values are left";
#: a from/to picker has no values to enumerate and needs two ends, and a
#: client that is told neither draws no picker or draws one over a guess.
#: Optional for the same reason as ``category_counts``: an engine without it
#: has not failed ``search.E002``. The service layer probes with ``getattr``
#: and reports ``facet_ranges`` in ``degraded[]``. Naive and Postgres
#: implement it; the conformance scenario ``range_bounds`` skips engines
#: without it and holds the rest to the naive reference.

#: Value prefix a backend uses in ``READ_PATH_IMPL`` when it answers a
#: declared read path through a NATIVE engine capability rather than
#: through a hand-written clause — e.g. Meilisearch answers ``geo:prefilter``
#: with ``_geoRadius`` and has no geohash column at all. Declared, so the
#: difference is reviewable; ``IDX002`` accepts it and the conformance suite
#: still runs the scenario.
NATIVE_IMPL = "capability:"


@runtime_checkable
class SearchBackend(Protocol):
    """Swappable engine (``STAPEL_SEARCH["BACKEND"]``)."""

    #: Short stable name, echoed in every response as ``backend``.
    name: str

    def capabilities(self) -> BackendCapabilities:
        """What this engine can do — the source of ``degraded[]``."""
        ...

    def health(self) -> BackendHealth:
        """Reachability and document count, for ``/health``."""
        ...

    # --- write side --------------------------------------------------------

    def upsert(self, docs: list[IndexDocument]) -> None:
        """Insert or replace *docs*. Idempotent by ``(doc_type, doc_key)``."""
        ...

    def delete(self, doc_type: str, keys: list[str]) -> None:
        """Remove documents by key. Missing keys are not an error."""
        ...

    def clear(self, doc_type: str | None = None) -> None:
        """Drop everything, or everything of one type."""
        ...

    def apply_settings(self, doc_type: str, settings: IndexSettings) -> None:
        """Push searchable/filterable/sortable attrs, synonyms, stopwords."""
        ...

    # --- read side ---------------------------------------------------------

    def query(self, q: SearchQuery) -> QueryResult:
        """Matching keys, scored and ordered by ``q.sort``."""
        ...

    def facets(self, q: SearchQuery, plan: FacetPlan) -> FacetResult:
        """Remaining-option counts for *plan* over *q*'s candidate set."""
        ...

    def suggest(
        self, doc_type: str, prefix: str, *, limit: int, scope: SearchQuery | None = None
    ) -> list[str]:
        """Title prefixes from the index — never from a query log."""
        ...


def missing_verbs(backend) -> tuple[str, ...]:
    """Verbs *backend* (class or instance) does not implement."""
    return tuple(verb for verb in VERBS if not callable(getattr(backend, verb, None)))


__all__ = [
    "NATIVE_IMPL",
    "VERBS",
    "BackendCapabilities",
    "BackendHealth",
    "FacetPlan",
    "FacetResult",
    "IndexDocument",
    "IndexSettings",
    "QueryResult",
    "SearchBackend",
    "SearchQuery",
    "missing_verbs",
]
