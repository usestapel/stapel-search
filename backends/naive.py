"""The declared, honest, slow backend — and the module's semantic reference.

It exists for one reason: so the Postgres backend never has to degrade.
``stapel-recordings`` and ``stapel-studio`` both fall back to ``icontains``
off Postgres and say so in a comment; for a *search library* that is not
acceptable, because the caller cannot tell the good engine from the bad one
by looking at the answer. Here the choice is explicit — a SQLite demo or a
unit test configures this backend and gets ``typo_tolerance: False`` in
``capabilities()`` and ``degraded: ["typo_tolerance"]`` in every response
that wanted it.

Being pure Python over the module's own table also makes it the reference
semantics: when the Postgres and Meilisearch implementations disagree with
this one, the conformance suite says which is wrong.

Cost is exactly what it looks like — it materializes the candidate set in
Python. That is fine for a demo and a test corpus and is not fine for a
real one, which is why ``capabilities().max_result_window`` and the
candidate cap still apply.
"""
from __future__ import annotations

from ..dto import (
    BackendCapabilities,
    BackendHealth,
    BandSummary,
    FacetPlan,
    FacetResult,
    Hit,
    IndexDocument,
    IndexSettings,
    QueryResult,
    SearchQuery,
)
from . import _shared as shared

#: Read paths of ``docs/index.json`` and the code in THIS module that
#: answers each. ``IDX002`` checks both that the promise is registered and
#: that the named symbol exists — the SUR004 boundary, one level stricter:
#: it proves the promise was not dropped on the floor, not that the branch
#: is right. The round-trip suite proves the branch.
READ_PATH_IMPL = {
    "filter:type": "_candidates",
    "filter:lang": "_candidates",
    "filter:owner": "_candidates",
    "filter:category": "_matches_category",
    "filter:facet": "_matches_facets",
    "filter:range": "_matches_ranges",
    "filter:radius": "_geo_distance",
    "filter:bbox": "_matches_bbox",
    "facets:counts": "facets",
    "q": "_text_score",
    "sort:newest": "_sort_value",
    "sort:price_asc": "_sort_value",
    "sort:price_desc": "_sort_value",
    "sort:distance": "_sort_value",
    "score:popularity": "_score",
    "score:promotion_boost": "_score",
    # No geohash prefilter: this backend already walks the candidate rows,
    # so a coarse cell narrowing would cost a pass and save nothing.
    "geo:prefilter": "capability:python_side_scan",
}


class NaiveSearchBackend:
    """Python-side matching over ``SearchDocument``. Declared, not hidden."""

    name = "naive"

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        from ..conf import search_settings
        from ..registry import get_scorers

        return BackendCapabilities(
            typo_tolerance=False,
            facet_counts=True,
            exact_facet_counts=True,
            exact_total=True,
            geo_native=False,
            synonyms_native=False,
            suggest=True,
            phrase_synonyms=False,
            supported_scorers=frozenset(get_scorers()),
            max_facet_fields=int(search_settings.MAX_FACET_FIELDS),
            max_result_window=int(search_settings.MAX_RESULT_WINDOW),
        )

    def health(self) -> BackendHealth:
        from ..models import SearchDocument

        return BackendHealth(
            name=self.name,
            reachable=True,
            detail="in-process, python-side matching",
            documents=SearchDocument.objects.count(),
        )

    # -- write side ---------------------------------------------------------

    def upsert(self, docs: list[IndexDocument]) -> None:
        """No-op: the table written by the indexer IS this backend's index."""
        return None

    def delete(self, doc_type: str, keys: list[str]) -> None:
        """No-op: the indexer already removed the rows."""
        return None

    def clear(self, doc_type: str | None = None) -> None:
        """No-op: the indexer owns the rows."""
        return None

    def apply_settings(self, doc_type: str, settings: IndexSettings) -> None:
        """No-op: there is no engine-side schema to push."""
        return None

    # -- read side ----------------------------------------------------------

    def _candidates(self, q: SearchQuery):
        """Coarse ORM narrowing, then Python for everything JSON-shaped.

        JSONField containment has no SQLite implementation, so facets and
        the category path are matched in Python here. That is a property of
        this backend, not of the contract: Postgres answers the same
        questions with GIN.
        """
        from ..models import SearchDocument

        qs = SearchDocument.objects.filter(doc_type=q.doc_type, visible=True)
        if q.language_filter:
            qs = qs.filter(language=q.language_filter)
        if q.owner_key:
            qs = qs.filter(owner_key=q.owner_key)
        if q.ranges:
            qs = shared.narrow_by_ranges(qs, q.ranges)
        return qs

    @staticmethod
    def _matches_category(row, q: SearchQuery) -> bool:
        return shared.category_matches(list(row.category_path or []), q.category_path)

    @staticmethod
    def _matches_facets(row, q: SearchQuery) -> bool:
        return shared.facets_match(row.facet_terms or [], q.facets)

    @staticmethod
    def _matches_ranges(row, q: SearchQuery) -> bool:
        """Ranges are already applied in SQL; kept as the named read path."""
        return True

    @staticmethod
    def _matches_bbox(row, q: SearchQuery) -> bool:
        return shared.bbox_matches_for(row.lat, row.lon, q)

    @staticmethod
    def _geo_distance(row, q: SearchQuery):
        """The MEASUREMENT: the reader's own position for this row.

        Anonymous readers measure against the public grid, so the radius cut,
        the band and the reported distance are all functions of the point the
        card publishes and of nothing finer. What is EMITTED is
        ``shared.published_distance_km`` of this — the measurement is not the
        answer, and this backend used to emit the raw float where the other
        two at least rounded it.
        """
        return shared.measured_distance_km(row.lat, row.lon, q)

    @staticmethod
    def _band(row, q: SearchQuery) -> str:
        """The band LABEL. Never a predicate: no caller may `continue` on it."""
        return shared.band_for(row.lat, row.lon, q)

    @staticmethod
    def _match_count(row, q: SearchQuery) -> int:
        return shared.match_count(row.facet_terms, q.signals)

    @staticmethod
    def _text_score(row, q: SearchQuery) -> float | None:
        """Weighted substring matching: A=title, B=extra, C=body.

        No stemming and no typo tolerance — that is the declared shortfall.
        Every declared term group must hit somewhere (AND across terms, OR
        within a group's expansions), which is the same semantics the FTS
        arm gets from ``plainto_tsquery`` + an OR-group per term.
        """
        if q.text is None or q.text.is_empty:
            return 0.0
        title = row.title.casefold()
        extra = (row.text_extra or "").casefold()
        body = (row.text_plain or "").casefold()
        score = 0.0
        from ..text import fold

        for group in q.text.terms:
            best = 0.0
            for term in group:
                needle = fold(term)
                if needle and needle in title:
                    best = max(best, 1.0)
                elif needle and needle in extra:
                    best = max(best, 0.4)
                elif needle and needle in body:
                    best = max(best, 0.2)
            if best == 0.0:
                return None
            score += best
        return score

    def _score(self, row, q: SearchQuery, text_score: float, distance_km) -> float:
        """Registry-driven score. ``promotion_boost`` is structurally
        confined to ``sort=relevance`` by its ``applies_to_sorts``."""
        return shared.combined_score(
            row=row, sort=q.sort, text_score=text_score, distance_km=distance_km
        )

    @staticmethod
    def _sort_value(row, sort: str, score: float, distance_km):
        return shared.sort_value_of(row, sort, score, distance_km)

    def _rows(self, q: SearchQuery) -> list[tuple]:
        """Every matching row as ``(sort_key, sort_value, hit, row)``."""
        out: list[tuple] = []
        for row in self._candidates(q).iterator():
            if not self._matches_category(row, q):
                continue
            if not self._matches_facets(row, q):
                continue
            if not self._matches_bbox(row, q):
                continue
            distance = self._geo_distance(row, q)
            if distance is shared.OUT_OF_RANGE:
                continue
            text_score = self._text_score(row, q)
            if text_score is None:
                continue
            score = self._score(row, q, text_score, distance)
            published = shared.published_distance_km(distance, q)
            value = self._sort_value(row, q.sort, score, published)
            band = self._band(row, q)
            matches = self._match_count(row, q)
            if any(shared.leading_keys(q)):
                value = shared.banded_sort_value(band, matches, value)
            out.append(
                (
                    shared.order_key(q.sort, value, row.doc_key),
                    value,
                    Hit(
                        key=row.doc_key,
                        score=round(score, 6),
                        distance_km=published,
                        sort_value=value,
                        band=band,
                        match_count=matches,
                    ),
                    row,
                )
            )
        out.sort(key=lambda item: item[0])
        return out

    def query(self, q: SearchQuery) -> QueryResult:
        rows = self._rows(q)
        total = len(rows)
        page, has_next, has_prev = shared.paginate(rows, q)
        return QueryResult(
            hits=tuple(item[2] for item in page),
            total=total,
            exact_total=True,
            has_next=has_next,
            has_prev=has_prev,
            degraded=(),
            bands=self._bands(q, rows),
        )

    @staticmethod
    def _bands(q: SearchQuery, rows: list[tuple]) -> tuple[BandSummary, ...]:
        """Exact per-band counts over the SAME rows the answer came from.

        Both bands are always reported, an empty one included: a heading a
        reader can scroll to must exist before the rows under it do, and a
        band that disappears when it empties reads as a filter.
        """
        if q.near is None or not q.near.has_center:
            return ()
        nearby = sum(1 for item in rows if item[2].band == "nearby")
        return (
            BandSummary(
                id="nearby", count=nearby, radius_km=shared.near_radius_km(q.near)
            ),
            BandSummary(id="all", count=len(rows) - nearby),
        )

    def facets(self, q: SearchQuery, plan: FacetPlan) -> FacetResult:
        """Exact drill-down counts, one candidate pass per counted slug."""
        counts: dict[str, dict[str, int]] = {}
        candidates = 0
        for slug in plan.slugs:
            drilled = q.without_facet(slug)
            rows = self._rows(drilled)
            candidates = max(candidates, len(rows))
            bucket: dict[str, int] = {}
            prefix = f"{slug}="
            for _key, _value, _hit, row in rows:
                for term in row.facet_terms or []:
                    if isinstance(term, str) and term.startswith(prefix):
                        bucket[term[len(prefix):]] = bucket.get(term[len(prefix):], 0) + 1
            counts[slug] = bucket
        return FacetResult(counts=counts, approximate=False, candidates=candidates)

    def category_counts(self, q: SearchQuery, *, limit: int) -> list[tuple[tuple[str, ...], int]]:
        """OPTIONAL verb: which categories *q*'s candidate set is made of.

        The evidence the facet plan is drawn from when the queried category
        owns no axes of its own — a branch, or no category at all (D175).
        Grouped over ``_rows``, which is this backend's own candidate set,
        so the count on a category is the count ``&category=…`` will show.
        Being pure Python over the module's own table, this is the reference
        semantics the conformance scenario holds the real engines to.
        """
        counts: dict[tuple[str, ...], int] = {}
        for _key, _value, _hit, row in self._rows(q):
            path = tuple(str(segment) for segment in (row.category_path or []))
            if not path:
                continue
            counts[path] = counts.get(path, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]

    def suggest(
        self, doc_type: str, prefix: str, *, limit: int, scope: SearchQuery | None = None
    ) -> list[str]:
        from ..models import SearchDocument

        folded = (prefix or "").strip()
        if not folded:
            return []
        qs = SearchDocument.objects.filter(
            doc_type=doc_type, visible=True, title__istartswith=folded
        )
        if scope is not None and scope.language_filter:
            qs = qs.filter(language=scope.language_filter)
        titles = qs.order_by("title", "doc_key").values_list("title", flat=True)[: limit * 4]
        seen: list[str] = []
        for title in titles:
            if title not in seen:
                seen.append(title)
            if len(seen) >= limit:
                break
        return seen

    def suggest_categories(
        self, doc_type: str, query: str, *, language: str, limit: int
    ) -> list[tuple[tuple[str, ...], int]]:
        """OPTIONAL verb: the categories whose DOCUMENTS match *query*.

        The goods-driven half of the type-ahead (see ``suggest.py``): when
        no category NAME matches, these pairs — ``(category path, matching
        document count)`` — become the suggestion rows. The predicate is
        this backend's own SERP predicate, on purpose and asserted
        (``test_a_goods_row_count_is_the_serp_count``): the count shown is
        the count the tap will find, because it is computed by the same
        ``_text_score`` walk a ``?q=…&category=…`` page runs.

        Grouped by the document's FULL path, so each count is exact for the
        page that path opens; a category with matching stock deeper down
        simply appears as its own deeper row. Being pure Python over the
        module's own table, this is also the reference semantics the
        conformance scenario holds the real engines to.
        """
        from ..text import normalize_query

        text = normalize_query(query, language)
        if text.is_empty:
            return []
        q = SearchQuery(doc_type=doc_type, language=language, text=text)
        counts: dict[tuple[str, ...], int] = {}
        for row in self._candidates(q).iterator():
            if self._text_score(row, q) is None:
                continue
            path = tuple(str(segment) for segment in (row.category_path or []))
            if not path:
                continue
            counts[path] = counts.get(path, 0) + 1
        # Busiest first; the path itself breaks ties so the answer is stable
        # rather than dictionary-ordered by insertion.
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]


__all__ = ["READ_PATH_IMPL", "NaiveSearchBackend"]
