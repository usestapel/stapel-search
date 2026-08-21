"""Backend facade: one dotted-path key swaps the whole engine.

    from stapel_search.backends import get_backend
    hits = get_backend().query(parsed_query)

Form is ``stapel_geo/search/__init__.py:31-46`` unchanged — resolve
``STAPEL_SEARCH["BACKEND"]``, duck-type it against the verb tuple, raise
``ImproperlyConfigured`` otherwise. **The check level differs on purpose:**
geo raises W because "a broken search backend only breaks the search
verbs"; here the search verbs *are* the module, so a service that came up
with a broken backend answers 5xx to its entire reason for existing. Hence
``search.E001`` / ``search.E002``.

Shipped engines:

- ``postgres.PostgresSearchBackend`` — the default. Zero new infrastructure:
  ``russian``-family FTS + ``pg_trgm`` + the two GIN structures over the
  module's own table.
- ``meili.MeilisearchBackend`` — the scale switch, behind the ``[meili]``
  extra. Same conformance suite, one settings key and a rebuild away.
- ``naive.NaiveSearchBackend`` — declared, for tests and SQLite demos. It
  exists so that the Postgres backend never has to degrade into a silent
  ``icontains``: a demo gets ``typo_tolerance: False`` in its answer instead
  of a worse engine wearing the same name.
- ``opensearch.OpenSearchBackend`` — a pointer, not a promise.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from .base import VERBS, SearchBackend, missing_verbs

_instances: dict[type, object] = {}


def get_backend(*, cached: bool = True) -> SearchBackend:
    """Instantiate ``STAPEL_SEARCH["BACKEND"]`` (already an imported class).

    Instances are memoized per class: a Meilisearch client holds a
    connection pool, and rebuilding it per request would be a new socket per
    query. Pass ``cached=False`` where a test needs a fresh one.
    """
    from ..conf import search_settings

    backend_cls = search_settings.BACKEND
    if not isinstance(backend_cls, type) or missing_verbs(backend_cls):
        raise ImproperlyConfigured(
            "STAPEL_SEARCH['BACKEND'] must point to a class implementing the "
            f"SearchBackend protocol ({', '.join(VERBS)}), got {backend_cls!r}"
        )
    if not cached:
        return backend_cls()
    instance = _instances.get(backend_cls)
    if instance is None:
        instance = backend_cls()
        _instances[backend_cls] = instance
    return instance


def reset_backend_cache() -> None:
    """Drop memoized backend instances (settings changed, tests)."""
    _instances.clear()


__all__ = ["SearchBackend", "get_backend", "reset_backend_cache"]
