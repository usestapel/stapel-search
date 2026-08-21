"""Action subscriptions — the invalidation half of event -> pull.

The seam form is ``stapel-docs/actions.py:41-88``: the registry maps a name
to a dotted-path mapper, ``wire_sources()`` runs from ``ready()``, and ONE
dispatcher serves every registered source, so a host never writes a
subscriber. Broken configuration raises ``ImproperlyConfigured`` rather than
log-and-skip — "configured but broken must not be silent".

Two rules this module will not bend:

1. **An event is a signal, not a document.** The ``listing.*`` payloads are
   ``additionalProperties: false`` and carry identity only; the document is
   pulled through the source's content Function. The same ``call()`` is
   in-process in a monolith and a bus round trip in a split.
2. **Visibility is never inferred from the event name.** Republishing a live
   listing emits ``listing.updated`` carrying ``status: "pending"`` and
   emits no ``listing.removed`` at all (spec §19.7). So even a "removal"
   signal goes through the pull: the pulled document's ``status`` decides,
   and a key the source no longer serves is the source saying "deleted".

Handlers are idempotent by construction — delivery is at-least-once, and
``index_documents`` drops a redelivered ``source_event_id`` and refuses a
``source_seq`` older than the stored one.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured

from stapel_core.comm import on_action, subscribe_action

logger = logging.getLogger(__name__)

#: Signal names already subscribed, so re-running ``wire_sources()`` (a
#: second ``ready()``, a test) does not stack handlers.
_wired: set[str] = set()


def dispatch_source_signal(event) -> None:
    """The single dispatcher for every registered source's signals."""
    from .registry import get_sources
    from .services import ingest

    name = getattr(event, "event_type", "")
    payload = getattr(event, "payload", None) or {}

    for doc_type, spec in get_sources().items():
        if name not in spec.signals:
            continue
        key = spec.key_of(payload)
        if key is None:
            logger.error(
                "%s carried no document key for source %r (tried %s): %s",
                name,
                doc_type,
                ", ".join(spec.key_fields),
                getattr(event, "event_id", ""),
            )
            continue
        report = ingest(
            doc_type,
            [key],
            event_id=getattr(event, "event_id", "") or "",
            seq=int(getattr(event, "timestamp", 0) or 0),
        )
        logger.debug("%s -> %s:%s %s", name, doc_type, key, report)


def wire_sources() -> None:
    """Subscribe the dispatcher to every signal the registry names.

    Called from ``ready()``. A source registered later at runtime calls
    :func:`wire_sources` again — subscriptions are idempotent by name.
    """
    from .registry import get_sources

    try:
        sources = get_sources()
    except (ImportError, TypeError) as exc:
        raise ImproperlyConfigured(
            f"STAPEL_SEARCH['SOURCES'] has an entry that cannot be resolved: {exc}"
        ) from exc

    for doc_type, spec in sources.items():
        if not callable(spec.mapper):
            raise ImproperlyConfigured(
                f"STAPEL_SEARCH['SOURCES'][{doc_type!r}] has a non-callable mapper"
            )
        for signal in spec.signals:
            if signal in _wired:
                continue
            subscribe_action(signal, dispatch_source_signal)
            _wired.add(signal)


def reset_wiring() -> None:
    """Forget which signals were wired (tests)."""
    _wired.clear()


@on_action("search.signal")
def handle_search_signal(event) -> None:
    """The one inbound door for boost / popularity / promoted.

    Who is allowed through it is a product decision (billing entitlement,
    editorial tooling) that lives outside this module: v1 owns the field and
    the audit row, not the policy. Adding tiers or a promo marketplace later
    is a new emitter, not a redesign here.
    """
    from .services import apply_signal

    apply_signal(event.payload or {}, event_id=getattr(event, "event_id", "") or "")


@on_action("category.changed")
def handle_category_changed(event) -> None:
    """Invalidate the cached facet plan and ancestry for a mutated category."""
    from .facets import note_changed

    payload = event.payload or {}
    category_id = payload.get("category_id")
    if category_id is None:
        logger.warning("category.changed without category_id: %s", event.event_id)
        return
    revision = payload.get("revision")
    note_changed(category_id, revision)
    note_changed(str(category_id), revision)


@on_action("user.deleted")
def handle_user_deleted(event) -> None:
    """Erase a deleted user's documents from the index (GDPR Art. 17).

    The index is derived data, but derived data is still data: a listing
    erased at the source and left searchable here is the erasure failing in
    the only place a stranger would notice.
    """
    from .registry import get_sources
    from .services import remove_documents

    user_id = (event.payload or {}).get("user_id")
    if not user_id:
        logger.error("user.deleted without user_id: %s", event.event_id)
        return

    from .models import SearchDocument

    for doc_type in get_sources():
        keys = list(
            SearchDocument.objects.filter(
                doc_type=doc_type, owner_key=str(user_id)
            ).values_list("doc_key", flat=True)
        )
        if keys:
            remove_documents(doc_type, keys)
            SearchDocument.objects.filter(doc_type=doc_type, doc_key__in=keys).delete()
            logger.info("search: erased %s %s document(s) for user %s", len(keys), doc_type, user_id)


__all__ = [
    "dispatch_source_signal",
    "handle_category_changed",
    "handle_search_signal",
    "handle_user_deleted",
    "reset_wiring",
    "wire_sources",
]
