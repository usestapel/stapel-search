"""stapel-search — search index, facets, geo radius and ranking for Stapel.

An L2 module with its own materialized index table and ONE dotted-path
backend seam. It knows nothing about listings: documents arrive through an
open source registry (``BUILTIN_SOURCES = {}``), and the composite that is
allowed to know both the corpus and the index declares the entry.

    STAPEL_SEARCH = {
        "BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend",
        "SOURCES": {"listing": "myshop.search.LISTING_SOURCE"},
    }

Public API (lazily exported, PEP 562 — importing this package never pulls
in Django or a configured settings module):

- ``search_settings`` — resolved app settings.
- ``SourceSpec`` / ``register_source`` — the document-source registry.
- ``FacetMapping`` / ``register_facet_mapping`` — index semantics per
  attribute type.
- ``Scorer`` / ``register_scorer`` — the ranking registry the P2B
  disclosure is generated from.
- ``register_dictionary`` — synonyms, rewrites and stopwords per language.
- ``SearchDocumentInput`` — what a source mapper returns.
- ``SearchBackend`` / ``get_backend`` — the engine seam.
- ``INDEX_FIELDS`` — the index contract, as data.

The models (``SearchDocument``, ``SearchNumber``, ``SearchSignal``) live in
``stapel_search.models`` — import them explicitly, not from here.
"""

__version__ = "0.11.1"

__all__ = [
    "FacetMapping",
    "INDEX_FIELDS",
    "IndexField",
    "Scorer",
    "SearchBackend",
    "SearchDocumentInput",
    "SourceSpec",
    "get_backend",
    "register_dictionary",
    "register_facet_mapping",
    "register_scorer",
    "register_source",
    "search_settings",
]

# name -> submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_search` stays Django-free.
_LAZY_EXPORTS = {
    "search_settings": ".conf",
    "SourceSpec": ".registry",
    "register_source": ".registry",
    "FacetMapping": ".registry",
    "register_facet_mapping": ".registry",
    "Scorer": ".registry",
    "register_scorer": ".registry",
    "register_dictionary": ".registry",
    "SearchDocumentInput": ".dto",
    "SearchBackend": ".backends.base",
    "get_backend": ".backends",
    "INDEX_FIELDS": ".index_schema",
    "IndexField": ".index_schema",
}


def __getattr__(name):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target, __name__), name)


def __dir__():
    return sorted(__all__)
