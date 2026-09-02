"""The pgvector store — one table, raw SQL, no driver dependency.

The embedding column is NOT a Django field. ``vector`` is an extension
type; declaring it in the model would make every migration, every SQLite
test run and every extensionless Postgres depend on pgvector being there.
Instead migration ``0003`` creates the metadata table portably, and the
column + the SQL that touches it live here, guarded by
:func:`available` — a deployment without the extension keeps a working
suggest and a declared ``vector_suggestions`` shortfall, the same posture
as every other engine difference in this package.

The column is UNTYPED ``vector`` (no fixed dimensionality): the model tag
(``<model>@<dims>``) scopes every search, so rows from two embedding
spaces can coexist during a re-embed and are never compared. The searches
are exact scans — at the intended corpus (10³–10⁵ short strings) an exact
cosine scan answers in single-digit milliseconds and has perfect recall;
an ANN index (HNSW) needs a TYPED column and becomes worth its build cost
somewhere past ~5·10⁵ rows. That growth step is a change of this module
only: type the column, add the index, keep every caller.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

TABLE = "search_vector_embedding"

#: Process-local availability memo: (known, value). The check costs two
#: catalog reads; a keystroke path must not repeat it.
_availability: list = [False, False]


def _postgres() -> bool:
    from django.db import connection

    return connection.vendor == "postgresql"


def _extension_installed() -> bool:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return cursor.fetchone() is not None


def _column_exists() -> bool:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'embedding'",
            [TABLE],
        )
        return cursor.fetchone() is not None


def available() -> bool:
    """Can this deployment store and search embeddings? Memoized."""
    if _availability[0]:
        return _availability[1]
    try:
        value = _postgres() and _extension_installed() and _column_exists()
    except Exception:  # noqa: BLE001 - no database at call time is "no"
        return False
    _availability[0], _availability[1] = True, value
    return value


def reset_availability() -> None:
    """Forget the memo — after :func:`ensure_schema`, and in tests."""
    _availability[0] = _availability[1] = False


def ensure_schema() -> bool:
    """Add the ``embedding`` column where the extension allows it.

    Idempotent, called by the index builder (and the postgres test rig) so
    a deployment that installs pgvector AFTER migrating heals itself on
    the next build instead of needing a fake migration. Returns whether
    the store is usable afterwards.
    """
    from django.db import connection

    if not _postgres() or not _extension_installed():
        return False
    if not _column_exists():
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN embedding vector NULL")
        logger.info("added %s.embedding (pgvector)", TABLE)
    reset_availability()
    return available()


def _literal(vector) -> str:
    """A pgvector input literal. Truncated repr keeps the wire small."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


def upsert_many(kind: str, rows, *, model_tag: str) -> int:
    """Insert-or-replace ``(key, text, payload, vector)`` rows for *kind*."""
    from django.db import connection
    from django.utils import timezone

    now = timezone.now()
    written = 0
    with connection.cursor() as cursor:
        for key, text, payload, vector in rows:
            cursor.execute(
                f"INSERT INTO {TABLE} (kind, key, text, payload, model_tag, "
                "updated_at, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::vector) "
                "ON CONFLICT (kind, key) DO UPDATE SET text = EXCLUDED.text, "
                "payload = EXCLUDED.payload, model_tag = EXCLUDED.model_tag, "
                "updated_at = EXCLUDED.updated_at, embedding = EXCLUDED.embedding",
                [
                    kind,
                    key,
                    text,
                    json.dumps(payload, ensure_ascii=False),
                    model_tag,
                    now,
                    _literal(vector),
                ],
            )
            written += 1
    return written


def search(kind: str, query_vector, *, model_tag: str, limit: int):
    """Exact cosine top-*limit* of *kind* rows in *model_tag*'s space.

    Returns ``[(key, text, payload, similarity)]``, best first. The floor
    is applied by the CALLER: it is a product decision about dropdowns,
    not a storage property.
    """
    from django.db import connection

    literal = _literal(query_vector)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT key, text, payload, 1 - (embedding <=> %s::vector) "
            f"FROM {TABLE} "
            "WHERE kind = %s AND model_tag = %s AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector ASC LIMIT %s",
            [literal, kind, model_tag, literal, int(limit)],
        )
        out = []
        for key, text, payload, similarity in cursor.fetchall():
            if isinstance(payload, str):
                payload = json.loads(payload or "{}")
            out.append((key, text, payload or {}, float(similarity)))
        return out


def prune(kind: str, *, model_tag: str, before) -> int:
    """Drop *kind* rows not re-written by the build that started at *before*
    (stale corpus entries) and rows from other embedding spaces."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {TABLE} WHERE kind = %s "
            "AND (model_tag != %s OR updated_at < %s)",
            [kind, model_tag, before],
        )
        return cursor.rowcount


def count(kind: str | None = None) -> int:
    from ..models import VectorEmbedding

    queryset = VectorEmbedding.objects.all()
    if kind:
        queryset = queryset.filter(kind=kind)
    return queryset.count()


__all__ = [
    "TABLE",
    "available",
    "count",
    "ensure_schema",
    "prune",
    "reset_availability",
    "search",
    "upsert_many",
]
