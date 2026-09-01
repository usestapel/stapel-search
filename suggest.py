"""Category suggestions: the type-ahead that offers *places*, not strings.

A classified's search box is a navigation control before it is a query
control. Typing «шорты» has three right answers and none of them is a
string: «Одежда › Мужская одежда › Шорты», «Одежда › Женская одежда ›
Шорты», «Детям › Детская одежда › Шорты». The word is the same in all
three; the only thing that tells them apart is the ancestor path, and the
only thing that tells the buyer which one to pick is how many live listings
are behind it.

So this module does three things and refuses a fourth.

**1. It does not match names itself.** Names, ancestry and the
retired/test/soft-deleted state of a node belong to stapel-categories, and
they are asked for by comm name (``categories.suggest``) exactly as
``categories.path`` already is — no import, no second copy of the tree, and
no opinion here about what "excluded" means. This module owns the query
language and hands over already-normalized terms; that module owns the
catalogue and hands back nodes.

**2. The count is the SERP's count, not a number that resembles it.** The
whole value of the row is that «Мужская одежда › Шорты · 128» predicts what
the next page shows. So the count is taken over the same index, with the
same two predicates a SERP starts from (``doc_type`` and ``visible``) and
the same category rule the SERP's ``category=`` filter applies — a path
PREFIX, so a parent counts its descendants. ``tests/test_suggest.py::
test_suggest_count_equals_the_serp_count`` is the gate: it asks this
module for the count and the query endpoint for the same category, and
fails if they disagree. Code that merely looks like the SERP's would pass
review; only that assertion proves it.

**3. Counting is one aggregate, ever.** ``GROUP BY category_path`` over the
index, once, then a prefix rollup in Python — never one count per
suggestion. The result is cached per document type, because the same map
answers every query in the debounce window: a buyer typing «шорты» sends
three requests, and they must not send three aggregates.

Why the aggregate reads the index TABLE rather than going through the
backend seam: ``SearchDocument`` is this module's own materialized table in
*both* topologies (``models.py:1-20``) — a Meilisearch deployment mirrors
into the engine, it does not move the table — and ``category_path`` is
written here by ``services._with_category_path`` under every backend. The
same reasoning already puts ``card`` and ``promoted`` on this side of the
seam and ``health()``'s document count on this side of it. Adding a
``category_counts`` verb to the protocol would oblige four engines to
reimplement a group-by over a column this module maintains itself, and the
first engine to get it subtly wrong would be invisible.

**Scale, measured rather than asserted.** 100k rows, 5% withdrawn, 2850
distinct category paths, Postgres 16::

    HashAggregate (actual time=34.906..35.093 rows=2850)
      Group Key: category_path
      ->  Seq Scan on search_document (actual rows=95000)
            Filter: (visible AND doc_type = 'listing')
    Execution Time: 35.208 ms

A sequential scan is the *right* plan here and not a missing index: 95% of
the table satisfies the predicate, so an index scan would read the same
pages plus the index. What keeps 35ms off the request path is that it runs
once per ``SUGGEST_COUNT_CACHE_TTL`` per document type and is shared by
every concurrent suggest — a buyer typing «шорты» sends several requests
and they see one aggregate between them.

Past the point where that is no longer enough, the next step is not a
longer TTL but a materialized per-category counter maintained by the
indexer. :func:`category_counts` is the single named read, so that is a
change of one function body and of nothing that calls it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Cache key prefix for the per-doc_type category count map.
_COUNT_CACHE_PREFIX = "stapel_search:catcount:"

#: How a category name matched the query, best first — the vocabulary
#: ``categories.suggest`` reports in each row's ``match``. Its ORDER is the
#: ranking key below; an unknown value sorts last, so a provider that grows a
#: fifth kind degrades to "worst" rather than crashing a dropdown.
MATCH_QUALITY: tuple[str, ...] = ("exact", "prefix", "word", "substring")


def _match_rank(match: Any) -> int:
    try:
        return MATCH_QUALITY.index(str(match))
    except ValueError:
        return len(MATCH_QUALITY)


def counts_cache_key(doc_type: str) -> str:
    return f"{_COUNT_CACHE_PREFIX}{doc_type}"


def invalidate_counts(doc_type: str) -> None:
    """Drop the cached count map for *doc_type*.

    Called from the indexer after a batch lands. The TTL alone would be
    correct but slow to notice: a stand that has just been seeded should not
    show a dropdown full of zeros for a minute, and a delete should not keep
    selling a category that has emptied.
    """
    from django.core.cache import cache

    cache.delete(counts_cache_key(doc_type))


def category_counts(doc_type: str) -> dict[tuple[str, ...], int]:
    """``{category path prefix: live listings under it}`` for *doc_type*.

    ONE aggregate over the index, grouped by ``category_path``, then rolled
    up over every prefix of every observed path so that a parent's entry is
    the sum of its descendants' — the same meaning the SERP's
    ``category=a/b`` prefix filter has (``backends/postgres.py:432-435``,
    ``backends/_shared.py:32``).

    The predicate is ``doc_type`` + ``visible`` and nothing else: those are
    the two clauses every backend's candidate set starts from, and they are
    the whole of what a bare category page filters by.
    """
    from django.core.cache import cache

    from .conf import search_settings

    key = counts_cache_key(doc_type)
    cached = cache.get(key)
    if cached is None:
        from django.db.models import Count

        from .models import SearchDocument

        rows = (
            SearchDocument.objects.filter(doc_type=doc_type, visible=True)
            .values("category_path")
            .annotate(n=Count("id"))
            .order_by()
        )
        cached = [
            ([str(segment) for segment in (row["category_path"] or [])], int(row["n"]))
            for row in rows
        ]
        cache.set(key, cached, int(search_settings.SUGGEST_COUNT_CACHE_TTL))

    rollup: dict[tuple[str, ...], int] = {}
    for path, n in cached:
        for depth in range(1, len(path) + 1):
            prefix = tuple(path[:depth])
            rollup[prefix] = rollup.get(prefix, 0) + n
    return rollup


def query_terms(q: str, language: str) -> tuple[str, ...]:
    """The terms to match category names against, cross-script included.

    This is the 0.6.0 query-normalization layer, unchanged and not copied:
    :func:`stapel_search.text.normalize_query` folds, applies the language's
    rewrites, drops its stopwords, expands its curated synonym groups and
    transliterates whatever no group claimed. Suggestions get «shorty» ->
    «шорты» and «айфон» -> «iPhone» because they ask the same function the
    SERP asks, in the same module, with the same dictionaries — a suggestion
    layer with its own normalizer would be a search box that finds one thing
    while typing and another after Enter.

    The result is FLAT: a category name is matched against every variant of
    every word, OR-ed. Suggestions are a "did you mean a place" question,
    not the SERP's conjunction — «зимняя обувь» should still offer «Обувь».
    """
    from .text import normalize_query

    normalized = normalize_query(q, language)
    out: list[str] = []
    for group in normalized.terms:
        for variant in group:
            if variant and variant not in out:
                out.append(variant)
    return tuple(out)


def suggest_categories(
    doc_type: str, q: str, *, language: str, limit: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ranked category suggestions for *q*, plus any degradations.

    Ranking is **stocked before empty, then match quality, then live listing
    count descending, then depth ascending, then name**, and every one of
    those five is load-bearing on a real catalogue.

    *Stocked before empty* keeps 0.7.0's product decision where it was right:
    a dropdown is a prediction of what the buyer will find, and a place with
    listings belongs above a place without. What it is NOT is a filter. A
    3036-leaf catalogue with 100 listings in it is 2924 empty leaves, and
    dropping them left «шорты» and «квартира» with no panel at all — a
    catalogue you cannot navigate because almost none of it is stocked yet.
    An empty row is offered, and it says «0» rather than pretending.

    *Match quality* second, and this is the fix 0.7.0 did not have: it sorted
    an all-zero result set by depth and then by NAME, so «Личные вещи ›
    Мужская одежда › **Шорты**» — the node the buyer typed, letter for
    letter — came third behind two «Брюки и шорты», because Б precedes Ш.
    ``categories.suggest`` grades every hit ``exact`` / ``prefix`` / ``word``
    / ``substring`` (stapel-categories 0.10) and the grade is the only signal
    that survives an empty corpus. It is also what keeps a transliterated
    fragment in its place: «iphone» normalizes to «ифон», which is a
    mid-word substring of «Сифоны» and nothing else on the board — a plumbing
    trap that must never outrank a word-boundary hit.

    Count, then depth, then name break what remains, in that order: among
    equally-well-matched places the busier one is the better prediction, the
    broader one is the safer landing, and the name makes the answer stable
    rather than incidentally ordered by whatever the tree read returned.
    """
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings
    from .facets import path_degradation

    terms = query_terms(q, language)
    if not terms:
        return [], []

    name = search_settings.CATEGORY_SUGGEST_FUNCTION
    try:
        answer = call(
            name,
            {
                "terms": list(terms),
                "limit": int(search_settings.SUGGEST_CATEGORY_CANDIDATES),
            },
        )
    except (CommError, LookupError, KeyError, TypeError) as exc:
        # A dropdown without categories is a smaller product, not a broken
        # endpoint: the terms half still answers. The shortfall travels to
        # the caller the way every other one in this module does.
        logger.warning("%s unavailable: %s", name, exc)
        return [], ["category_suggestions"]

    candidates = (answer or {}).get("categories") or []
    if not candidates:
        return [], []

    counts = category_counts(doc_type)
    degraded: list[str] = []
    # Ancestry that never arrived means every stored path is one segment
    # long, so a candidate's root->leaf prefix matches nothing and every
    # count would read 0. Saying so beats printing a catalogue of zeros.
    if path_degradation():
        degraded.append("category_rollup")

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        path_ids = [str(segment) for segment in (candidate.get("path_ids") or [])]
        if not path_ids:
            continue
        rows.append(
            {
                "id": candidate.get("id"),
                "slug": candidate.get("slug") or "",
                "name": candidate.get("name") or "",
                "path": list(candidate.get("path") or []),
                # Ready to paste into `?category=` — the query parser splits
                # this parameter on "/" (`query.py:166-167`). Serving the
                # joined string rather than only the segments means a
                # frontend cannot invent a different join and silently miss.
                "category": "/".join(path_ids),
                "count": counts.get(tuple(path_ids), 0),
                "depth": int(candidate.get("depth") or len(path_ids)),
                "match": candidate.get("match") or "substring",
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["count"] > 0 else 1,
            _match_rank(row["match"]),
            -row["count"],
            row["depth"],
            row["name"],
        )
    )
    return rows[:limit], degraded


__all__ = [
    "MATCH_QUALITY",
    "category_counts",
    "counts_cache_key",
    "invalidate_counts",
    "query_terms",
    "suggest_categories",
]
