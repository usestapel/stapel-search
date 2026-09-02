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

The GOODS-DRIVEN half (0.9.1) sits on the other side of that same line, and
deliberately: when no category NAME matches the query, the dropdown asks
which categories hold matching DOCUMENTS, and "matching" is the engine's
own text predicate — tsquery arms on Postgres, an analyzer on Meilisearch.
That cannot live on this side without reimplementing every engine's
matching in Python, which is the seam defect in the opposite direction. So
it is an OPTIONAL backend verb (``suggest_categories``), naive and Postgres
implement it, and an engine without it degrades to the 0.9.0 answer with
``category_listing_suggestions`` in ``degraded[]`` — declared, like every
other engine difference.

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

#: How a suggestion earned its row, best first. The first four name grades
#: are the vocabulary ``categories.suggest`` reports in each row's ``match``
#: (stapel-categories 0.10); ``listings`` is the one grade minted HERE — a
#: goods-driven row, offered because documents matching the query live under
#: that category even though no category NAME says the word. It sits between
#: ``word`` and ``substring`` on purpose: a name that contains the query as
#: a word is a promise about the whole category and outranks a corpus
#: co-occurrence, but the goods themselves are stronger evidence than a
#: fragment buried mid-word in an unrelated name («ифон» ⊂ «Сифоны»). The
#: ORDER is the ranking key below; an unknown value sorts last, so a
#: provider that grows a new kind degrades to "worst" rather than crashing
#: a dropdown.
MATCH_QUALITY: tuple[str, ...] = ("exact", "prefix", "word", "listings", "substring")

#: The grade a goods-driven row carries.
LISTINGS_MATCH = "listings"

#: The STRONG name grades: the query is the name, starts it, or starts a
#: word inside it. This is the class boundary of the ranking — see
#: :func:`suggest_categories` — and the trigger for the goods-driven
#: fallback: only when the name matcher produced nothing in this class are
#: the documents themselves asked where the query leads.
STRONG_NAME_MATCHES = frozenset({"exact", "prefix", "word"})

#: The ranking's first key: strong name grades and goods-driven rows sort
#: as one class, above ``substring`` and every unknown grade. A mid-word
#: accident never leads real evidence, stocked or not.
_FIRST_CLASS = STRONG_NAME_MATCHES | {LISTINGS_MATCH}


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


def _loose(term: str) -> str:
    """*term* with combining marks stripped — the loose-typing normal form.

    Only for the sibling-containment test in :func:`query_terms`. It is
    deliberately NOT :func:`stapel_search.text.fold`, which KEEPS Cyrillic
    diacritics because й and и are different letters to a reader. Here the
    question is the opposite one: a group's recall fragments are spelled
    the way a hurried keyboard spells («ифон» is «айфон» minus the breve),
    and the test has to see them that way to recognize them as fragments.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", term)
    return unicodedata.normalize(
        "NFC", "".join(char for char in decomposed if not unicodedata.combining(char))
    )


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

    NOT flat, though, is the whole group. A curated group carries members
    that exist purely for SERP recall — «ифон» in the iphone group is there
    so a buyer who types it loosely still finds the phones — and against
    full document text under AND-of-groups they are harmless. Against
    category NAMES they are poison: the matcher grades a substring hit, and
    «ифон» is a mid-word substring of exactly one thing on a real board —
    «Сифоны», the plumbing traps, which a live stand offered as the ONLY
    suggestion for «айфон». So a variant that is buried MID-WORD inside a
    sibling of its own group is dropped here, on this path only.

    Two refinements, both load-bearing on the shipped ru groups:

    - The containment test strips combining marks first. «ифон» is not a
      LITERAL substring of «айфон» — й and и are different letters — it is
      the fragment of its loosely-typed form, which is exactly why the
      group carries it. A test that respects the breve misses the one
      fragment the rule exists for.
    - A variant that is a PREFIX of its sibling is a stem, not a fragment
      («самсунг» ⊂ «самсунга», «авто» ⊂ «автомобиль»), and it survives:
      the shipped groups inflect almost every brand, so a literal "drop
      every substring" rule would have deleted «самсунг» and «айфон»
      themselves and an exact-named category would stop matching its own
      name.

    The dictionary file is untouched; the SERP keeps every member.
    """
    from .text import normalize_query

    normalized = normalize_query(q, language)
    out: list[str] = []
    for group in normalized.terms:
        members = [variant for variant in group if variant]
        loose = {variant: _loose(variant) for variant in members}
        for variant in members:
            buried_mid_word = any(
                variant != other
                and loose[variant] in loose[other]
                and not loose[other].startswith(loose[variant])
                for other in members
            )
            if buried_mid_word:
                continue
            if variant not in out:
                out.append(variant)
    return tuple(out)


def _listing_rows(
    backend: Any, doc_type: str, q: str, *, language: str, limit: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Goods-driven rows: the categories whose DOCUMENTS match *q*.

    The semantic half of the type-ahead. Category names carry nouns, not
    brands: no board names a node «Samsung» or «Камри», so «samsung» found
    nothing by name while the SERP found real listings — a dropdown
    answering "that does not exist" about goods the very next page shows.
    When the name matcher has no strong answer, the backend that runs the
    SERP is asked which categories hold matching documents, through an
    OPTIONAL verb (``suggest_categories``); an engine without it answers
    exactly what 0.9.0 answered, and the shortfall travels to the caller as
    ``category_listing_suggestions`` — declared, like every other engine
    difference, never absorbed.

    Each pair is ``(category path, matching-document count)`` — the count is
    the SERP's own count for ``?q=…&category=…``, because the backend runs
    the same text predicate the SERP runs; a number that merely resembles it
    would be the suggest-count lie all over again, and
    ``test_a_goods_row_count_is_the_serp_count`` compares the two ends.

    Display names come from ONE batched ``categories.names`` read
    (stapel-categories 0.13.0) — ids are this module\'s, names are the tree
    provider\'s, the same ownership rule as every other category fact here.
    A segment the provider does not answer for keeps its id — a truthful
    segment, never an invented name — and a provider that is missing or
    down leaves every id in place with ``category_names`` in ``degraded[]``,
    so an operator learns the read is dark from the payload rather than
    from a dropdown quietly full of numbers. The ``category`` filter string
    keeps the IDS regardless: display changed, the tap did not.
    """
    fallback = getattr(backend, "suggest_categories", None)
    if fallback is None:
        return [], ["category_listing_suggestions"]
    try:
        pairs = fallback(doc_type, q, language=language, limit=limit)
    except Exception as exc:  # noqa: BLE001 - an engine fault must not 500 a keystroke
        logger.warning(
            "suggest_categories on backend %s failed: %s",
            getattr(backend, "name", "unknown"),
            exc,
        )
        return [], ["category_listing_suggestions"]

    rows: list[dict[str, Any]] = []
    for path, count in pairs or []:
        path_ids = [str(segment) for segment in path if str(segment)]
        if not path_ids:
            continue
        leaf = path_ids[-1]
        rows.append(
            {
                # The provider serves integer pks; keep the type where the
                # segment allows it so the two row kinds read alike.
                "id": int(leaf) if leaf.lstrip("-").isdigit() else leaf,
                "slug": "",
                "name": leaf,
                "path": list(path_ids),
                "category": "/".join(path_ids),
                "count": int(count),
                "depth": len(path_ids),
                "match": LISTINGS_MATCH,
            }
        )
    if not rows:
        return rows, []
    names, names_degraded = _category_names(
        {segment for row in rows for segment in row["path"]}
    )
    for row in rows:
        row["path"] = [
            (names.get(segment) or {}).get("name") or segment
            for segment in row["path"]
        ]
        resolved = names.get(str(row["id"])) or {}
        row["name"] = resolved.get("name") or row["name"]
        row["slug"] = resolved.get("slug") or row["slug"]
    return rows, names_degraded


def _category_names(ids: set[str]) -> tuple[dict[str, dict], list[str]]:
    """One batched id -> {name, slug} read, fail-soft.

    ``LookupError`` (no responder) and ``CommError`` (a responder that
    cannot answer) degrade the same way: empty mapping plus the
    ``category_names`` marker — the caller keeps serving truthful id
    segments and the payload says why they are ids.
    """
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    name = search_settings.CATEGORY_NAMES_FUNCTION
    try:
        answer = call(name, {"ids": sorted(ids)})
    except (CommError, LookupError, KeyError, TypeError) as exc:
        logger.warning("%s unavailable: %s", name, exc)
        return {}, ["category_names"]
    resolved = (answer or {}).get("names") or {}
    if not isinstance(resolved, dict):
        logger.warning("%s answered a non-mapping; serving ids", name)
        return {}, ["category_names"]
    return {str(k): v for k, v in resolved.items() if isinstance(v, dict)}, []


def suggest_categories(
    doc_type: str,
    q: str,
    *,
    language: str,
    limit: int,
    backend: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ranked category suggestions for *q*, plus any degradations.

    Ranking is **match class, then stocked before empty, then match rank,
    then live listing count descending, then depth ascending, then name**,
    and the order of the first two is the 0.9.1 lesson, learned on a live
    stand.

    *Match class* first — the strong grades (``exact`` / ``prefix`` /
    ``word``) plus the goods-driven ``listings`` rows above ``substring``
    and anything unknown. 0.9.0 sorted stocked-before-empty first, across
    grades, and a real catalogue punished it: «айфон» expands to a group
    whose SERP-recall fragment «ифон» is a mid-word substring of «Сифоны»,
    the siphon category happened to be stocked, and the buyer got plumbing
    traps as the ONLY suggestion — a stocked accident outranking every real
    hit. Stock is a prediction of what the buyer will find; it must never
    promote a row to a question the buyer did not ask. (The fragment itself
    no longer reaches the matcher — :func:`query_terms` — but the ranking
    must hold for every group the dictionaries will ever grow.)

    *Stocked before empty* second keeps 0.7.0's product decision where it
    was right: BETWEEN real hits, the place with listings belongs above the
    place without. What it is NOT is a filter. A 3036-leaf catalogue with
    100 listings in it is 2924 empty leaves, and dropping them left «шорты»
    and «квартира» with no panel at all. An empty row is offered, and it
    says «0» rather than pretending.

    *Match rank* third is the fix 0.7.0 did not have: an all-zero result
    set used to sort by depth and then by NAME, so the node the buyer typed
    letter for letter came third behind two «Брюки и шорты», because Б
    precedes Ш. The grade is the only signal that survives an empty corpus.
    Inside the strong class it also places ``listings`` rows below ``word``
    ones — a name that says the word is a promise about the whole category,
    a co-occurrence in documents is weaker evidence — which is why the
    fallback only runs at all when no strong NAME row exists.

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

    if backend is None:
        from .backends import get_backend

        backend = get_backend()

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
        # the caller the way every other one in this module does. The goods
        # fallback deliberately does NOT run here: a comm outage is a fault,
        # and papering over it with backend rows would hide from the
        # operator that the category provider is down.
        logger.warning("%s unavailable: %s", name, exc)
        return [], ["category_suggestions"]

    candidates = (answer or {}).get("categories") or []

    degraded: list[str] = []
    rows: list[dict[str, Any]] = []
    if candidates:
        counts = category_counts(doc_type)
        # Ancestry that never arrived means every stored path is one segment
        # long, so a candidate's root->leaf prefix matches nothing and every
        # count would read 0. Saying so beats printing a catalogue of zeros.
        if path_degradation():
            degraded.append("category_rollup")
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

    if not any(str(row["match"]) in STRONG_NAME_MATCHES for row in rows):
        listing_rows, listing_degraded = _listing_rows(
            backend, doc_type, q, language=language, limit=limit
        )
        degraded.extend(listing_degraded)
        # A path the name matcher already offered keeps its named row — it
        # has a real display name and a rollup count; the goods row for the
        # same place would be a duplicate destination in the dropdown.
        known = {row["category"] for row in rows}
        rows.extend(row for row in listing_rows if row["category"] not in known)

    rows.sort(
        key=lambda row: (
            0 if str(row["match"]) in _FIRST_CLASS else 1,
            0 if row["count"] > 0 else 1,
            _match_rank(row["match"]),
            -row["count"],
            row["depth"],
            row["name"],
        )
    )
    return rows[:limit], degraded


__all__ = [
    "LISTINGS_MATCH",
    "MATCH_QUALITY",
    "STRONG_NAME_MATCHES",
    "category_counts",
    "counts_cache_key",
    "invalidate_counts",
    "query_terms",
    "suggest_categories",
]
