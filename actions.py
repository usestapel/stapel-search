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

The account life cycle is the one place both rules are suspended, and on
purpose: ``user.deleted`` and ``user.merged`` (the pair core 0.52.x requires,
``stapel_core.lifecycle.E001``) address rows by ``owner_key`` and are answered
from the index itself. A pull would ask the source a question about an account
it may not have finished answering for itself — see
:func:`~stapel_search.services.reassign_owner`.

Handlers are idempotent by construction — delivery is at-least-once, and
``index_documents`` drops a redelivered ``source_event_id`` and refuses a
``source_seq`` older than the stored one.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured, ValidationError

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


@on_action("user.merged")
def handle_user_merged(event) -> None:
    """Re-index a merged-away account's documents under the survivor.

    stapel-auth absorbs an anonymous guest into an existing account and then
    DELETES the guest row. ``SearchDocument.owner_key`` is a copy of the
    source's owner, and nothing else in the fleet will come along to correct
    it: the source modules re-parent their own rows with a bulk ``UPDATE``
    and emit no per-document signal, so an index left alone keeps every one
    of the guest's documents filed under an id that can no longer sign in —
    invisible to "my listings", and never erased, because no erasure was ever
    requested for it. ``user.deleted`` is the wrong tool for it too: that
    handler *removes* documents, and a merge removes nothing.

    Unlike a deletion this is a re-index, not a table write. Rewriting only
    the row would leave the ENGINE's copy of each document still owned by the
    guest, so every touched row is pushed back through the same path a signal
    write uses. Why it is not a re-pull from the source —
    :func:`~stapel_search.services.reassign_owner` states the two reasons in
    full: the source may not have processed its own merge yet, and ``ingest``
    treats a missing document as a delete.

    No survivor probe and no retry raise, unlike the modules that hold a real
    FK: ``owner_key`` is an opaque ``CharField``, so the id needs nothing to
    exist here before it can be written. Idempotent by the same token — a
    redelivery matches no rows and reports zero.
    """
    from .services import reassign_owner

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    try:
        moved = reassign_owner(from_user_id, into_user_id)
    except (ValidationError, ValueError, TypeError):
        # An id that cannot address a row here names nothing. Django raises
        # ValidationError (not a ValueError) wherever a key is coerced, and an
        # escaping exception is a poison pill: no redelivery can fix a typo.
        logger.warning("user.merged with unusable user ids: %s", event.event_id)
        return
    if moved:
        logger.info(
            "user.merged %s -> %s: %s document(s) re-indexed",
            from_user_id, into_user_id, moved,
        )


__all__ = [
    "dispatch_source_signal",
    "handle_category_changed",
    "handle_search_signal",
    "handle_user_deleted",
    "handle_user_merged",
    "reset_wiring",
    "wire_sources",
]
