"""The seam to the deterministic query-normalization layer.

CONTRACT — ``VECTOR_QUERY_NORMALIZER`` names a
``Callable[[str, str], str]``: ``(raw query, language) -> the canonical
string``. That string is both the embedding input and the query-cache key,
so two spellings the deterministic layer already considers equal share one
embedding and one cache entry.

This module deliberately does NOT normalize beyond :func:`fold_normalizer`
(case, diacritics, whitespace). Synonyms, transliteration and alias tables
belong to the deterministic layer (``text.py`` and its successors); when
that layer exports a canonical single-string form, point
``VECTOR_QUERY_NORMALIZER`` at it and this module follows without a code
change — the REPLACE-seam rule, same as ``BACKEND``.
"""
from __future__ import annotations


def fold_normalizer(q: str, language: str) -> str:
    """Case/diacritic fold + whitespace collapse — the default canon.

    Wraps :func:`stapel_search.text.fold` so the vector layer can never
    disagree with the index about what «Ё» equals.
    """
    from ..text import fold

    return " ".join(fold(q or "").split())


def normalize(q: str, language: str) -> str:
    """*q* through the configured normalizer."""
    from django.utils.module_loading import import_string

    from ..conf import search_settings

    target = search_settings.VECTOR_QUERY_NORMALIZER
    normalizer = import_string(target) if isinstance(target, str) else target
    return normalizer(q, language)


__all__ = ["fold_normalizer", "normalize"]
