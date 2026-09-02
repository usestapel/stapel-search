"""Where the vector net attaches to the suggest answer.

One function, called from ``services.suggest`` after the deterministic
ranking has spoken. The contract with that ranking (deliberately narrow,
because the ranking is live code with its own release cadence):

- vector rows are considered only when NO first-class row exists — the
  same trigger the goods-driven fallback uses, read from the same
  constants (:data:`stapel_search.suggest._FIRST_CLASS`);
- vector rows are APPENDED below whatever determinism produced, ordered
  by similarity, graded ``match: "vector"`` — a grade the ranking already
  sorts last-class by its unknown-grade rule, so the two layers cannot
  disagree about precedence;
- a destination determinism already offered is never offered twice.
"""
from __future__ import annotations


def augment_category_suggestions(
    rows: list[dict],
    degraded: list[str],
    *,
    doc_type: str,
    q: str,
    language: str,
    limit: int,
) -> tuple[list[dict], list[str]]:
    """*rows*/*degraded*, with vector destinations appended when earned.

    Flag off: the very same objects come back, untouched — the off state
    is byte-identical, and paying even a cache read for it would make the
    flag a lie.
    """
    from .service import enabled, similar

    if not enabled():
        return rows, degraded

    from ..suggest import _FIRST_CLASS, category_counts

    if any(str(row.get("match")) in _FIRST_CLASS for row in rows):
        return rows, degraded

    hits, shortfall = similar("category", q, language=language)
    if shortfall:
        return rows, [*degraded, shortfall]
    if not hits:
        return rows, degraded

    counts = category_counts(doc_type)
    known = {row.get("category") for row in rows}
    appended = list(rows)
    for hit in hits:
        payload = hit.get("payload") or {}
        path_ids = [str(segment) for segment in (payload.get("path_ids") or [])]
        if not path_ids:
            continue
        category = "/".join(path_ids)
        if category in known:
            continue
        known.add(category)
        appended.append(
            {
                "id": payload.get("id"),
                "slug": payload.get("slug") or "",
                "name": payload.get("name") or hit["text"],
                "path": list(payload.get("path") or []),
                "category": category,
                "count": counts.get(tuple(path_ids), 0),
                "depth": int(payload.get("depth") or len(path_ids)),
                "match": "vector",
                "similarity": hit["similarity"],
            }
        )
    return appended[: int(limit)], degraded


__all__ = ["augment_category_suggestions"]
