"""Vector similarity — the net under the deterministic floor.

The deterministic layers each miss a class of query in their own honest
way: folding misses phonetics («тимбирленд» folds to itself, not to
«Timberland»), transliteration misses typos, trigram misses cross-script.
An embedding space catches what all of them drop, because closeness in it
is learned from how people actually spell things — at the price of an API
round trip and a similarity score instead of a proof.

That price sets the architecture:

- **Flag-gated, default OFF** (``VECTOR_SUGGEST``). Off means off: no
  request, no cache read, the suggest answer is byte-identical to a
  deployment without this package.
- **Below the floor, never instead of it.** The fallback runs only when
  the deterministic answer produced nothing first-class, and its rows are
  APPENDED under whatever determinism did produce — the same seam the
  goods-driven fallback draws. A similarity floor
  (``VECTOR_SIMILARITY_FLOOR``) turns "nearest garbage" into "nothing".
- **The query normalizer is a seam, not a copy** (`seam.py`): the
  deterministic normalization layer owns what a query IS; this layer only
  asks it, through ``VECTOR_QUERY_NORMALIZER``.
- **Embeddings come from the fleet's provider seam**, by comm name
  (``VECTOR_EMBED_FUNCTION`` → ``llm.embed``, stapel-agent): no HTTP
  client, no API key and no proxy configuration lives here.
- **Storage is pgvector** (`store.py`), the one engine-specific piece —
  behind ``ensure_schema()``/``available()`` so a deployment without the
  extension degrades to a declared shortfall, never an error.
"""
from . import corpus, seam, service, store
from .integration import augment_category_suggestions
from .service import enabled, model_tag, similar

__all__ = [
    "augment_category_suggestions",
    "corpus",
    "enabled",
    "model_tag",
    "seam",
    "service",
    "similar",
    "store",
]
