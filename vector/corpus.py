"""What gets embedded — the corpus registry.

``VECTOR_CORPORA`` maps a kind to the dotted path of a PROVIDER::

    {"category": "stapel_classified.vector_corpora.category_corpus",
     "vocab_label": "stapel_vocabularies.vector.label_corpus"}

A provider is a zero-argument callable yielding
``{"key": str, "text": str, "payload": dict}`` — every string a user
might be typing toward, with whatever the consumer needs to render a row
from a bare hit. The registry is EMPTY by design, the ``SOURCES``
precedent exactly: this module knows nothing about categories or
vocabularies; the composite that is allowed to know both sides declares
the entries.

Scope is the point, not a limitation. The typo problem lives in the
strings people TYPE — category names, brand/make labels — a corpus of
thousands to tens of thousands. Embedding an entire 800k-term vocabulary
store would cost little money and a lot of index for strings nobody
types; a provider that enumerates less is a better provider.
"""
from __future__ import annotations

from collections.abc import Iterable


def providers() -> dict[str, object]:
    """``{kind: provider callable}`` from ``VECTOR_CORPORA``, resolved."""
    from django.utils.module_loading import import_string

    from ..conf import search_settings

    out: dict[str, object] = {}
    for kind, target in (search_settings.VECTOR_CORPORA or {}).items():
        if not target:
            continue
        out[kind] = import_string(target) if isinstance(target, str) else target
    return out


def entries(kind: str) -> Iterable[dict]:
    """The corpus for *kind*, from its registered provider."""
    registry = providers()
    if kind not in registry:
        raise LookupError(
            f"no corpus provider for kind {kind!r} — declare it in "
            "STAPEL_SEARCH['VECTOR_CORPORA']"
        )
    return registry[kind]()


__all__ = ["entries", "providers"]
