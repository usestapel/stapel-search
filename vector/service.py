"""Query-side vector service: embed (cached), search, floor.

The latency shape this encodes: the pgvector scan is local single-digit
milliseconds; the expensive step is the embedding round trip, which on a
proxied stand can cost hundreds of milliseconds. Two mechanisms keep that
off the p50:

- the fallback only runs at all when determinism came back empty-handed
  (:mod:`.integration`), so the common keystroke never pays it;
- the query embedding is cached under the NORMALIZED query
  (``VECTOR_QUERY_CACHE_TTL``, a week by default) — type-ahead traffic is
  Zipfian and every repeat of a popular misspelling is a cache hit. The
  cache key includes the model tag, so a re-embed to a new space never
  reads stale vectors.
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

_QUERY_CACHE_PREFIX = "stapel_search:vecq:"


def enabled() -> bool:
    """Is the vector layer switched on? (`VECTOR_SUGGEST`, default off.)"""
    from ..conf import search_settings

    flag = search_settings.VECTOR_SUGGEST
    if isinstance(flag, str):
        return flag.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(flag)


def model_tag() -> str:
    """``<model>@<dims>`` — the identity of the embedding SPACE.

    Stamped on every stored row and every cache key: vectors from two
    models (or two dimension cuts of one model) are not comparable, and
    a tag mismatch is how a needed re-embed is DETECTED rather than
    silently searched across.
    """
    from ..conf import search_settings

    return f"{search_settings.VECTOR_MODEL}@{int(search_settings.VECTOR_DIMENSIONS)}"


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """One batch through the fleet's embedding seam. ``None`` on failure."""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from ..conf import search_settings

    name = search_settings.VECTOR_EMBED_FUNCTION
    try:
        answer = call(
            name,
            {
                "texts": list(texts),
                "model": str(search_settings.VECTOR_MODEL),
                "timeout_seconds": int(search_settings.VECTOR_EMBED_TIMEOUT),
                "provider_options": {
                    "dimensions": int(search_settings.VECTOR_DIMENSIONS)
                },
            },
        )
    except (CommError, LookupError, KeyError, TypeError) as exc:
        logger.warning("%s unavailable: %s", name, exc)
        return None
    if not answer or answer.get("status") != "ok":
        logger.warning(
            "%s failed: %s", name, (answer or {}).get("reason", "no answer")
        )
        return None
    vectors = (answer.get("embeddings") or {}).get("vectors") or []
    if len(vectors) != len(texts):
        logger.warning(
            "%s answered %d vectors for %d texts — refusing the batch",
            name,
            len(vectors),
            len(texts),
        )
        return None
    return vectors


def embed_query(q: str, language: str) -> list[float] | None:
    """The (cached) embedding of one normalized query. ``None`` on failure."""
    from django.core.cache import cache

    from ..conf import search_settings
    from . import seam

    normalized = seam.normalize(q, language)
    if not normalized:
        return None
    tag = model_tag()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    key = f"{_QUERY_CACHE_PREFIX}{tag}:{digest}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    vectors = embed_texts([normalized])
    if vectors is None:
        return None
    cache.set(key, vectors[0], int(search_settings.VECTOR_QUERY_CACHE_TTL))
    return vectors[0]


def similar(
    kind: str,
    q: str,
    *,
    language: str = "",
    limit: int | None = None,
    floor: float | None = None,
) -> tuple[list[dict], str | None]:
    """Corpus entries of *kind* near *q*: ``([hits], degradation | None)``.

    A hit is ``{"key", "text", "payload", "similarity"}``, best first,
    floor applied. The second element names the shortfall when the layer
    is on but cannot answer (store or embedder unavailable) — ``None``
    both on success and on an honest empty answer.

    The store is checked BEFORE the embedder: a deployment without
    pgvector must not pay an API round trip to learn it has nowhere to
    search.
    """
    from ..conf import search_settings
    from . import store

    if not store.available():
        return [], "vector_suggestions"
    vector = embed_query(q, language)
    if vector is None:
        return [], "vector_suggestions"
    if limit is None:
        limit = int(search_settings.VECTOR_TOP_K)
    if floor is None:
        # One floor cannot serve two corpora (measured live: LaBSE puts
        # cross-script brand matches near 0.85+ over a wide gap, but a
        # 3.4k-name Russian category corpus puts character-overlap
        # accidents at the same height). VECTOR_KIND_FLOORS overrides the
        # global per corpus; an explicit argument overrides both.
        kind_floors = search_settings.VECTOR_KIND_FLOORS or {}
        floor = float(
            kind_floors.get(kind, search_settings.VECTOR_SIMILARITY_FLOOR)
        )
    hits = store.search(kind, vector, model_tag=model_tag(), limit=limit)
    return [
        {
            "key": key,
            "text": text,
            "payload": payload,
            "similarity": round(similarity, 4),
        }
        for key, text, payload, similarity in hits
        if similarity >= floor
    ], None


__all__ = [
    "embed_query",
    "embed_texts",
    "enabled",
    "model_tag",
    "similar",
]
