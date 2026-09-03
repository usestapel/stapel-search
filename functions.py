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
    """Run a search. Same parser, same envelope as ``GET .../query``.

    The payload's ``audience`` says who the answer is FOR, and it defaults to
    ``anonymous``: a server-side caller assembling a public strip must not
    get finer coordinates than the page it is building would. A caller that
    IS the owner or staff says so, and says it per call — the transport
    cannot infer it, and a transport that guessed would be the side door
    around the whole gate.
    """
    from stapel_attributes import visibility

    from .services import search

    payload = payload or {}
    return search(
        payload, audience=visibility.normalize_audience(payload.get("audience"))
    )


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


@function("search.similar", schema=_schema("search.similar"))
def similar_function(payload: dict) -> dict:
    """Vector neighbours of a query in one registered corpus.

    The door a sibling module's type-ahead knocks on when its own
    deterministic matching came back thin: stapel-vocabularies asks
    ``{"kind": "vocab_label", "q": "тимбирленд"}`` and gets the labels an
    embedding space places next to the typo, floor applied, best first.
    The caller maps labels back onto its own rows — this module never
    learns what a vocabulary is.

    Flag-gated like the rest of the layer: OFF answers
    ``degraded: ["vector_disabled"]`` without paying for an embedding, so
    a caller can leave its side wired and follow this module's switch.
    """
    from .vector import service

    if not service.enabled():
        return {"results": [], "degraded": ["vector_disabled"]}
    hits, shortfall = service.similar(
        payload["kind"],
        payload["q"],
        language=str(payload.get("language") or ""),
        limit=payload.get("limit"),
        floor=payload.get("floor"),
    )
    return {"results": hits, "degraded": [shortfall] if shortfall else []}


__all__ = ["query_function", "reindex_function", "similar_function"]
