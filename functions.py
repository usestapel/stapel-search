"""comm Functions provided by stapel-search.

Server-side callers (a recommendation job, an admin tool, a sibling module
building a "similar listings" strip) get the same query contract the HTTP
surface serves, without an HTTP round trip and without importing this
package:

    from stapel_core.comm import call
    call("search.query", {"type": "listing", "q": "iphone", "limit": 5})

Every Function carries a JSON schema in ``schemas/functions/``; tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly rather than at the far end.
"""
import json
from pathlib import Path

from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@function("search.query", schema=_schema("search.query"))
def query_function(payload: dict) -> dict:
    """Run a search. Same parser, same envelope as ``GET .../query``."""
    from .services import search

    return search(payload or {})


@function("search.reindex", schema=_schema("search.reindex"))
def reindex_function(payload: dict) -> dict:
    """Re-pull specific keys, or rebuild a whole type.

    Given ``keys`` it re-pulls exactly those (cheap, targeted — what a host
    calls after a bulk import). Without them it rebuilds the type from the
    source's snapshot Function, which is the expensive, complete path.
    """
    from .services import ingest, rebuild

    doc_type = payload["doc_type"]
    keys = payload.get("keys")
    report = ingest(doc_type, keys) if keys else rebuild(doc_type)
    return {
        "doc_type": doc_type,
        "indexed": report.indexed,
        "removed": report.removed,
        "skipped_stale": report.skipped_stale,
        "skipped_duplicate": report.skipped_duplicate,
    }


__all__ = ["query_function", "reindex_function"]
