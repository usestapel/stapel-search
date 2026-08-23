"""Meilisearch backend — the scale switch, behind the ``[meili]`` extra.

Packaged inside stapel-search rather than as a separate distribution
(verdict §18.6): one conformance suite, one release cycle, and the seam is
provable end to end by flipping ``STAPEL_SEARCH["BACKEND"]`` and running
``search_rebuild`` with no module code changed.

What Meilisearch does natively and Postgres does not: typo tolerance,
exact facet counts at any corpus size, exact totals, ``_geoRadius`` /
``_geoBoundingBox``, and real index-side synonyms. Those differences travel
in ``capabilities()``, so the same response tells a caller which engine
answered and what it could not do.

What is done in Python here, deliberately: **ordering, scoring and keyset
pagination**. Meilisearch ranks by its own rules and cannot evaluate the
scorer registry, and offset paging is not stable when a document is
inserted between two pages. So the engine is asked for the matching set
(bounded by ``MAX_RESULT_WINDOW``, which is a declared refusal anyway) and
the shared ordering code — the same code the naive backend runs — turns it
into a page. Two engines agreeing about order because they execute the same
ordering function is a stronger guarantee than two engines that happen to
agree today.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..dto import (
    BackendCapabilities,
    BackendHealth,
    FacetPlan,
    FacetResult,
    Hit,
    IndexDocument,
    IndexSettings,
    QueryResult,
    SearchQuery,
)
from . import _shared as shared

logger = logging.getLogger(__name__)

#: Read paths of ``docs/index.json`` and the symbol answering each.
#: ``capability:`` values say "this engine answers the same question with a
#: native feature instead of a hand-written clause" — declared, so the
#: difference is reviewable rather than discovered in production.
READ_PATH_IMPL = {
    "filter:type": "_filters",
    "filter:lang": "_filters",
    "filter:owner": "_filters",
    "filter:category": "_filters",
    "filter:facet": "_filters",
    "filter:range": "_filters",
    "filter:radius": "_filters",
    "filter:bbox": "_filters",
    "facets:counts": "facets",
    "q": "_search",
    "sort:newest": "_rank",
    "sort:price_asc": "_rank",
    "sort:price_desc": "_rank",
    "sort:distance": "_rank",
    "score:popularity": "_rank",
    "score:promotion_boost": "_rank",
    # Meilisearch resolves proximity with _geoRadius over its own geo index;
    # there is no geohash column to prefilter, and inventing one would be a
    # second source of truth for the same question.
    "geo:prefilter": "capability:geo_native",
}

_FILTERABLE = (
    "doc_type",
    "visible",
    "language",
    "owner_key",
    "category_prefix",
    "facet_terms",
    "promoted",
    "price_base",
    "published_ts",
    "popularity",
    "boost",
    "numeric",
    "_geo",
)

_SORTABLE = ("price_base", "published_ts", "popularity", "boost", "_geo")


class MeilisearchUnavailable(RuntimeError):
    """The ``[meili]`` extra is not installed, or the server is unreachable."""


def _is_index_not_found(exc: Exception) -> bool:
    """Whether *exc* is Meilisearch saying "no such index"."""
    return getattr(exc, "code", "") == "index_not_found" or "index_not_found" in str(exc)


@dataclass
class _Row:
    """The subset of a Meilisearch document the shared scorer needs.

    A tiny adapter rather than the ORM row: re-reading Postgres to score a
    Meilisearch answer would put the database back on the hot path the
    switch exists to take it off.
    """

    doc_key: str
    boost: float
    popularity: int
    published_at: datetime | None
    price_base: Decimal | None
    facet_terms: list


class MeilisearchBackend:
    """Meilisearch engine behind the same nine verbs."""

    name = "meili"

    def __init__(self) -> None:
        self._client = None

    # -- plumbing -----------------------------------------------------------

    @staticmethod
    def _require_client():
        try:
            import meilisearch  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised by the extra
            raise MeilisearchUnavailable(
                "MeilisearchBackend needs the client: pip install 'stapel-search[meili]'"
            ) from exc
        return meilisearch

    def client(self):
        if self._client is None:
            meilisearch = self._require_client()
            from ..conf import search_settings

            self._client = meilisearch.Client(
                search_settings.MEILI_URL,
                search_settings.MEILI_KEY or None,
                timeout=int(search_settings.MEILI_TIMEOUT),
            )
        return self._client

    @staticmethod
    def index_uid(doc_type: str) -> str:
        return f"stapel_search_{doc_type}"

    def index(self, doc_type: str):
        return self.client().index(self.index_uid(doc_type))

    @staticmethod
    def _document_id(doc_type: str, doc_key: str) -> str:
        """Meilisearch primary keys allow only ``[A-Za-z0-9_-]``."""
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in doc_key)
        return f"{doc_type}__{safe}"

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        from ..conf import search_settings

        return BackendCapabilities(
            typo_tolerance=True,
            facet_counts=True,
            exact_facet_counts=True,
            exact_total=True,
            geo_native=True,
            synonyms_native=True,
            suggest=True,
            phrase_synonyms=True,
            supported_scorers=frozenset(
                {"relevance", "freshness_decay", "geo_decay", "promotion_boost", "popularity"}
            ),
            max_facet_fields=int(search_settings.MAX_FACET_FIELDS),
            max_result_window=int(search_settings.MAX_RESULT_WINDOW),
        )

    def health(self) -> BackendHealth:
        try:
            self.client().health()
        except Exception as exc:  # noqa: BLE001 - any transport failure is "down"
            return BackendHealth(name=self.name, reachable=False, detail=str(exc))
        return BackendHealth(name=self.name, reachable=True, detail="ok")

    # -- write side ---------------------------------------------------------

    def _as_meili_document(self, doc: IndexDocument) -> dict:
        body = {
            "id": self._document_id(doc.doc_type, doc.doc_key),
            "doc_key": doc.doc_key,
            "doc_type": doc.doc_type,
            "visible": doc.visible,
            "language": doc.language,
            "owner_key": doc.owner_key,
            # Every prefix, so an ancestor filter finds a descendant with a
            # plain equality — the same shape facet_terms uses for paths.
            "category_prefix": [
                "/".join(str(p) for p in doc.category_path[: i + 1])
                for i in range(len(doc.category_path))
            ],
            "title": doc.title,
            "text_extra": doc.text_extra,
            "body": doc.body,
            "facet_terms": list(doc.facet_terms),
            "numeric": {slug: float(value) for slug, value in (doc.numbers or {}).items()},
            "price_base": None if doc.price_base is None else float(doc.price_base),
            "published_ts": int(doc.published_at.timestamp()) if doc.published_at else None,
            "popularity": int(doc.popularity or 0),
            "boost": float(doc.boost or 0.0),
            "promoted": bool(doc.promoted),
        }
        if doc.lat is not None and doc.lon is not None:
            body["_geo"] = {"lat": float(doc.lat), "lng": float(doc.lon)}
        return body

    def upsert(self, docs: list[IndexDocument]) -> None:
        if not docs:
            return
        by_type: dict[str, list[dict]] = {}
        for doc in docs:
            by_type.setdefault(doc.doc_type, []).append(self._as_meili_document(doc))
        for doc_type, payload in by_type.items():
            task = self.index(doc_type).add_documents(payload, primary_key="id")
            self._wait(task)

    def delete(self, doc_type: str, keys: list[str]) -> None:
        if not keys:
            return
        task = self.index(doc_type).delete_documents(
            [self._document_id(doc_type, key) for key in keys]
        )
        self._wait(task)

    def clear(self, doc_type: str | None = None) -> None:
        from ..registry import get_sources

        types = [doc_type] if doc_type else list(get_sources())
        for name in types:
            try:
                self._wait(self.index(name).delete_all_documents())
            except Exception as exc:  # noqa: BLE001 - a missing index is already clear
                logger.debug("meili clear(%s): %s", name, exc)

    def apply_settings(self, doc_type: str, settings: IndexSettings) -> None:
        """Push the engine-side schema and the dictionary halves it owns.

        Synonyms go in as Meilisearch's asymmetric map expanded from our
        symmetric groups, and stopwords as ``stopWords`` — from there the
        engine does its own work. That is the whole difference from
        Postgres, where the same groups become query expansion because a
        library cannot write files into ``$SHAREDIR/tsearch_data``.
        """
        index = self.index(doc_type)
        self._wait(index.update_searchable_attributes(list(settings.searchable_fields)))
        self._wait(index.update_filterable_attributes(list(_FILTERABLE)))
        self._wait(index.update_sortable_attributes(list(_SORTABLE)))
        synonyms: dict[str, list[str]] = {}
        for group in settings.synonyms:
            for member in group:
                synonyms.setdefault(member, [])
                synonyms[member].extend(m for m in group if m != member)
        self._wait(index.update_synonyms(synonyms))
        self._wait(index.update_stop_words(list(settings.stopwords)))

    def _wait(self, task) -> None:
        """Block until the async task finishes — indexing must be observable.

        Meilisearch applies writes asynchronously. An indexer that returns
        before the engine applied the write turns "searchable within 5s"
        into a coin flip, and that freshness target is a number this module
        publishes in ``/health``. So every write waits for its task.
        """
        uid = getattr(task, "task_uid", None) or getattr(task, "uid", None)
        if uid is None:
            return
        from ..conf import search_settings

        self.client().wait_for_task(
            uid, timeout_in_ms=max(5000, int(search_settings.MEILI_TIMEOUT) * 1000)
        )

    # -- read side ----------------------------------------------------------

    def _filters(self, q: SearchQuery, *, skip_facet: str | None = None) -> list:
        """Meilisearch filter expression for every predicate of *q*."""
        filters: list = [f'doc_type = "{q.doc_type}"', "visible = true"]
        if q.language_filter:
            filters.append(f'language = "{q.language_filter}"')
        if q.owner_key:
            filters.append(f'owner_key = "{q.owner_key}"')
        if q.category_path:
            joined = "/".join(str(p) for p in q.category_path)
            filters.append(f'category_prefix = "{joined}"')
        for slug, values in (q.facets or {}).items():
            if slug == skip_facet:
                continue
            terms = shared.facet_terms_for(slug, values)
            filters.append([f'facet_terms = "{term}"' for term in terms])
        for spec in q.ranges:
            if spec.lower is not None:
                filters.append(f"numeric.{spec.slug} >= {spec.lower}")
            if spec.upper is not None:
                filters.append(f"numeric.{spec.slug} <= {spec.upper}")
        geo = q.geo
        if geo is not None:
            if geo.has_center and geo.radius_km is not None:
                filters.append(
                    f"_geoRadius({geo.lat}, {geo.lon}, {float(geo.radius_km) * 1000})"
                )
            elif geo.is_bbox:
                filters.append(
                    f"_geoBoundingBox([{geo.max_lat}, {geo.max_lon}], "
                    f"[{geo.min_lat}, {geo.min_lon}])"
                )
        return filters

    def _search(self, q: SearchQuery, *, skip_facet: str | None = None, facet: str | None = None):
        """One engine round trip: matching documents, bounded by the window."""
        from ..conf import search_settings

        window = int(search_settings.MAX_RESULT_WINDOW)
        params = {
            "filter": self._filters(q, skip_facet=skip_facet),
            # AND across terms. Meilisearch defaults to "last", which drops
            # trailing query words until something matches — a helpful
            # default for a search box and the wrong answer for a contract
            # that says terms are AND-ed. A search that widens the MEANING
            # of a query when it finds too little is answering a different
            # question than the one asked.
            "matchingStrategy": "all",
            "limit": window,
            "attributesToRetrieve": [
                "doc_key",
                "boost",
                "popularity",
                "published_ts",
                "price_base",
                "facet_terms",
                "_geo",
            ],
        }
        if facet:
            params["facets"] = ["facet_terms"]
            params["limit"] = 1
        if q.geo is not None and q.geo.has_center:
            params["sort"] = [f"_geoPoint({q.geo.lat}, {q.geo.lon}):asc"]
        text = ""
        if q.text is not None and not q.text.is_empty:
            # The engine owns morphology and typo tolerance; we hand it the
            # dictionary-normalized terms and nothing else (verdict §9).
            text = " ".join(q.text.flat_terms)
        try:
            return self.index(q.doc_type).search(text, params)
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if _is_index_not_found(exc):
                # A corpus that was never indexed has no documents. That is
                # an empty answer, not an error: the caller asked a valid
                # question about a doc_type this engine has not seen.
                return {"hits": [], "estimatedTotalHits": 0}
            raise

    def _rank(self, q: SearchQuery, response) -> list[tuple]:
        """Score and order the engine's answer with the shared semantics."""
        rows: list[tuple] = []
        for raw in response.get("hits", []):
            published = raw.get("published_ts")
            row = _Row(
                doc_key=raw["doc_key"],
                boost=float(raw.get("boost") or 0.0),
                popularity=int(raw.get("popularity") or 0),
                published_at=(
                    datetime.fromtimestamp(published, tz=timezone.utc) if published else None
                ),
                price_base=(
                    None if raw.get("price_base") is None else Decimal(str(raw["price_base"]))
                ),
                facet_terms=list(raw.get("facet_terms") or []),
            )
            geo = raw.get("_geo") or {}
            distance = shared.geo_distance_km(geo.get("lat"), geo.get("lng"), "", q.geo)
            if distance is shared.OUT_OF_RANGE:
                continue
            # Meilisearch already ordered by its own relevance; the position
            # is the only relevance signal it exposes without a version-
            # specific scoring flag, so it becomes the text score.
            position = len(rows)
            text_score = 1.0 / (1.0 + position)
            score = shared.combined_score(
                row=row, sort=q.sort, text_score=text_score, distance_km=distance
            )
            value = shared.sort_value_of(row, q.sort, score, distance)
            rows.append(
                (
                    shared.order_key(q.sort, value, row.doc_key),
                    value,
                    Hit(
                        key=row.doc_key,
                        score=round(score, 6),
                        distance_km=None if distance is None else round(float(distance), 2),
                        sort_value=value,
                    ),
                    row,
                )
            )
        rows.sort(key=lambda item: item[0])
        return rows

    def query(self, q: SearchQuery) -> QueryResult:
        from ..conf import search_settings

        response = self._search(q)
        rows = self._rank(q, response)
        page, has_next, has_prev = shared.paginate(rows, q)
        window = int(search_settings.MAX_RESULT_WINDOW)
        returned = len(response.get("hits", []))
        if returned < window:
            # The engine handed over everything it had, and `_rank` then
            # dropped whatever fell outside the radius: what survived IS the
            # answer, counted, not estimated. `estimatedTotalHits` is not
            # used here on purpose — it is the engine's guess AND it counts
            # the rows the geo pass just removed.
            total, lower_bound = len(rows), False
        else:
            # The window truncated the answer, so every number available is
            # a floor: at least this many match.
            estimated = int(response.get("estimatedTotalHits") or 0)
            total, lower_bound = max(estimated, len(rows)), True
        return QueryResult(
            hits=tuple(item[2] for item in page),
            total=total,
            exact_total=not lower_bound,
            total_is_lower_bound=lower_bound,
            has_next=has_next,
            has_prev=has_prev,
            degraded=(),
        )

    def facets(self, q: SearchQuery, plan: FacetPlan) -> FacetResult:
        """Exact drill-down counts — one engine call per counted slug.

        Meilisearch would return the whole ``facet_terms`` distribution in a
        single call, but drill-down semantics need the counted slug's own
        filter removed, and that is a different candidate set per slug. The
        N+1 shape is the semantics, not the engine.
        """
        counts: dict[str, dict[str, int]] = {}
        candidates = 0
        for slug in plan.slugs:
            drilled = q.without_facet(slug)
            response = self._search(drilled, skip_facet=slug, facet=slug)
            distribution = (response.get("facetDistribution") or {}).get("facet_terms", {})
            prefix = f"{slug}="
            counts[slug] = {
                term[len(prefix):]: int(n)
                for term, n in distribution.items()
                if term.startswith(prefix)
            }
            candidates = max(candidates, int(response.get("estimatedTotalHits") or 0))
        return FacetResult(counts=counts, approximate=False, candidates=candidates)

    def suggest(
        self, doc_type: str, prefix: str, *, limit: int, scope: SearchQuery | None = None
    ) -> list[str]:
        filters = [f'doc_type = "{doc_type}"', "visible = true"]
        if scope is not None and scope.language_filter:
            filters.append(f'language = "{scope.language_filter}"')
        try:
            response = self.index(doc_type).search(
                prefix or "",
                {"filter": filters, "limit": limit * 4, "attributesToRetrieve": ["title"]},
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately
            if _is_index_not_found(exc):
                return []
            raise
        titles: list[str] = []
        for raw in response.get("hits", []):
            title = raw.get("title") or ""
            if title and title not in titles:
                titles.append(title)
            if len(titles) >= limit:
                break
        return titles


__all__ = ["READ_PATH_IMPL", "MeilisearchBackend", "MeilisearchUnavailable"]
