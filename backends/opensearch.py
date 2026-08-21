"""OpenSearch backend stub — a pointer, not a promise.

The form is ``stapel_geo/search/elasticsearch.py``: every verb raises, and
the hint names both the implementation path and the settings key, so the
next person does not have to guess whether this was abandoned or never
started.

OpenSearch/Elasticsearch becomes the right engine when the demand is mixed
text+geo relevance ranking with per-field boosting on a corpus past the
point where Meilisearch's memory profile stops being pleasant. Until that
demand is real and measured, implementing it would mean a third engine to
keep green in the conformance suite for nobody.
"""
from __future__ import annotations

#: This module implements no read path. ``IDX002`` skips a backend that
#: declares itself a stub, because a stub that must "implement" every read
#: path is a rule pushing you to write a fake one.
IS_STUB = True

READ_PATH_IMPL: dict[str, str] = {}

_HINT = (
    "OpenSearchBackend is a stub. Implement it against the OpenSearch query "
    "DSL (bool/must for filters, multi_match with field boosts for q, "
    "terms aggregations for facet counts, geo_distance / geo_bounding_box "
    "for geo), keep an index synced from search_document through upsert(), "
    "then point STAPEL_SEARCH['BACKEND'] at your class. The conformance "
    "suite (stapel_search.testing.backend_conformance) is the acceptance "
    "criterion — a new backend without it green does not merge."
)


class OpenSearchBackend:
    """Not implemented (see :data:`_HINT`)."""

    name = "opensearch"

    def capabilities(self):
        raise NotImplementedError(_HINT)

    def health(self):
        raise NotImplementedError(_HINT)

    def upsert(self, docs):
        raise NotImplementedError(_HINT)

    def delete(self, doc_type, keys):
        raise NotImplementedError(_HINT)

    def clear(self, doc_type=None):
        raise NotImplementedError(_HINT)

    def apply_settings(self, doc_type, settings):
        raise NotImplementedError(_HINT)

    def query(self, q):
        raise NotImplementedError(_HINT)

    def facets(self, q, plan):
        raise NotImplementedError(_HINT)

    def suggest(self, doc_type, prefix, *, limit, scope=None):
        raise NotImplementedError(_HINT)


__all__ = ["IS_STUB", "READ_PATH_IMPL", "OpenSearchBackend"]
