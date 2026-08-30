"""``user.merged`` — a guest's documents stay findable after signing in.

stapel-auth absorbs an anonymous guest into an existing account and then
DELETES the guest row. ``SearchDocument.owner_key`` is a copy of the source's
owner, and nothing else in the fleet corrects it: the source modules
re-parent their own rows with a bulk ``UPDATE`` and emit no per-document
signal, so an index left alone keeps every one of the guest's documents filed
under an id that can no longer sign in. What is pinned here:

* the whole index moves, across doc types, in one call;
* it is a **re-index**, not a table write: the engine's copy is pushed too,
  because rewriting only the row leaves the other half of the index stale;
* it is **not** a source re-pull — asserted, because a pull would ask the
  source about a merge it may not have processed yet, and ``ingest`` treats a
  document missing from the source's answer as a delete;
* the handler is idempotent, and a no-op for ids it has never seen;
* signal-owned fields (boost, promoted, popularity) survive the transfer;
* a malformed or missing id does not raise — an escaping exception is a
  poison pill on an at-least-once bus.
"""
from __future__ import annotations

import types
import uuid

import pytest

from stapel_search.actions import handle_user_merged
from stapel_search.models import SearchDocument
from stapel_search.services import reassign_owner

pytestmark = pytest.mark.django_db

GUEST = "11111111-1111-4111-8111-111111111111"
SURVIVOR = "22222222-2222-4222-8222-222222222222"


class RecordingBackend:
    """Stands in for the engine half of the index."""

    name = "recording"

    def __init__(self):
        self.upserted: list[str] = []
        self.deleted: list[str] = []
        self.owners: dict[str, str] = {}

    def upsert(self, docs) -> None:
        for doc in docs:
            self.upserted.append(doc.doc_key)
            self.owners[doc.doc_key] = doc.owner_key

    def delete(self, doc_type, keys) -> None:
        self.deleted.extend(keys)


@pytest.fixture
def engine(monkeypatch):
    backend = RecordingBackend()
    monkeypatch.setattr(
        "stapel_search.backends.get_backend", lambda *a, **kw: backend
    )
    return backend


def _doc(owner_key, doc_key="d1", doc_type="listing", **extra):
    return SearchDocument.objects.create(
        doc_type=doc_type,
        doc_key=doc_key,
        owner_key=owner_key,
        title=f"doc {doc_key}",
        **extra,
    )


def _event(from_user_id, into_user_id, event_id="evt-merge"):
    return types.SimpleNamespace(
        payload={
            "from_user_id": str(from_user_id),
            "into_user_id": str(into_user_id),
            "reason": "anonymous_promotion",
        },
        event_id=event_id,
    )


# ── the happy path ──────────────────────────────────────────────────────


def test_every_document_moves_across_doc_types(engine):
    mine_a = _doc(GUEST, "d1", doc_type="listing")
    mine_b = _doc(GUEST, "p1", doc_type="profile")
    theirs = _doc("someone-else", "d2")

    handle_user_merged(_event(GUEST, SURVIVOR))

    mine_a.refresh_from_db()
    mine_b.refresh_from_db()
    theirs.refresh_from_db()
    assert mine_a.owner_key == SURVIVOR
    assert mine_b.owner_key == SURVIVOR
    assert theirs.owner_key == "someone-else"
    assert not SearchDocument.objects.filter(owner_key=GUEST).exists()


def test_the_engine_copy_is_re_indexed_not_just_the_row(engine):
    """The index has two halves. Rewriting only the table would leave every
    document filed in the engine under an account that cannot sign in."""
    _doc(GUEST, "d1")
    _doc(GUEST, "d2")

    handle_user_merged(_event(GUEST, SURVIVOR))

    assert sorted(engine.upserted) == ["d1", "d2"]
    assert engine.owners == {"d1": SURVIVOR, "d2": SURVIVOR}
    assert engine.deleted == []


def test_a_tombstoned_document_moves_too(engine):
    """``visible=False`` is a document that left the indexed statuses, not
    one that stopped being the person's."""
    hidden = _doc(GUEST, "d1", visible=False)

    handle_user_merged(_event(GUEST, SURVIVOR))

    hidden.refresh_from_db()
    assert hidden.owner_key == SURVIVOR
    assert hidden.visible is False


def test_signal_owned_fields_survive_the_transfer(engine):
    """A merge must not undo a promotion — the source document and the
    signal write own disjoint columns, and this touches neither."""
    row = _doc(GUEST, "d1", boost=2.5, promoted=True, popularity=42)

    handle_user_merged(_event(GUEST, SURVIVOR))

    row.refresh_from_db()
    assert (row.boost, row.promoted, row.popularity) == (2.5, True, 42)


def test_the_ordering_token_is_untouched(engine):
    """``source_seq``/``source_event_id`` describe the SOURCE's last word
    about this document. A merge is not one, so a later real event must not
    be mistaken for a stale one."""
    row = _doc(GUEST, "d1", source_seq=99, source_event_id="e-99")

    handle_user_merged(_event(GUEST, SURVIVOR))

    row.refresh_from_db()
    assert (row.source_seq, row.source_event_id) == (99, "e-99")


# ── not a re-pull ───────────────────────────────────────────────────────


def test_the_source_is_never_asked(engine, monkeypatch):
    """A pull would ask the source about a merge it may not have processed
    yet — and would re-stamp the guest's id back onto the row this handler
    just corrected, with nothing left to fix it. It would also delete: a
    document missing from the source's answer is a removal."""
    def explode(name, payload):
        raise AssertionError(f"user.merged must not call the source ({name})")

    monkeypatch.setattr("stapel_core.comm.call", explode)
    _doc(GUEST, "d1")

    handle_user_merged(_event(GUEST, SURVIVOR))

    assert SearchDocument.objects.get(doc_key="d1").owner_key == SURVIVOR


def test_documents_of_a_source_that_is_no_longer_registered_still_move(engine):
    """The handler walks rows, not the source registry: a host that dropped
    a source from settings has not thereby given up its index rows."""
    _doc(GUEST, "x1", doc_type="a-type-nobody-registered")

    handle_user_merged(_event(GUEST, SURVIVOR))

    assert SearchDocument.objects.get(doc_key="x1").owner_key == SURVIVOR


# ── idempotency and the quiet paths ─────────────────────────────────────


def test_second_delivery_changes_nothing_and_touches_the_engine_once(engine):
    _doc(GUEST, "d1")

    handle_user_merged(_event(GUEST, SURVIVOR))
    handle_user_merged(_event(GUEST, SURVIVOR))  # at-least-once delivery

    assert SearchDocument.objects.get(doc_key="d1").owner_key == SURVIVOR
    assert engine.upserted == ["d1"]


def test_a_guest_with_no_documents_is_a_clean_no_op(engine):
    assert reassign_owner(GUEST, SURVIVOR) == 0
    assert engine.upserted == []


def test_an_event_naming_users_with_nothing_here_does_nothing(engine):
    untouched = _doc(GUEST, "d1")

    handle_user_merged(_event(uuid.uuid4(), uuid.uuid4()))

    untouched.refresh_from_db()
    assert untouched.owner_key == GUEST
    assert engine.upserted == []


def test_merge_into_self_is_a_no_op(engine):
    row = _doc(GUEST, "d1")

    handle_user_merged(_event(GUEST, GUEST))

    row.refresh_from_db()
    assert row.owner_key == GUEST
    assert engine.upserted == []


def test_a_survivor_this_deployment_has_never_seen_needs_no_retry(engine):
    """Unlike the modules that hold a real FK, ``owner_key`` is an opaque
    CharField: nothing has to exist before the id can be written, so there is
    no ordering lag to raise about."""
    _doc(GUEST, "d1")
    never_seen = str(uuid.uuid4())

    handle_user_merged(_event(GUEST, never_seen))

    assert SearchDocument.objects.get(doc_key="d1").owner_key == never_seen


# ── malformed and missing payloads ──────────────────────────────────────


def test_missing_ids_are_reported_and_ignored(engine):
    row = _doc(GUEST, "d1")

    handle_user_merged(types.SimpleNamespace(payload={"into_user_id": SURVIVOR}, event_id="e1"))
    handle_user_merged(types.SimpleNamespace(payload={"from_user_id": GUEST}, event_id="e2"))
    handle_user_merged(types.SimpleNamespace(payload={}, event_id="e3"))

    row.refresh_from_db()
    assert row.owner_key == GUEST


def test_an_unusable_id_does_not_raise(engine):
    """``owner_key`` is text, so ``"not-a-uuid"`` addresses no row rather
    than exploding — but the handler must not depend on that: a raise here
    would be a poison pill the bus redelivers forever."""
    row = _doc(GUEST, "d1")

    handle_user_merged(_event("not-a-uuid", SURVIVOR))
    handle_user_merged(_event(GUEST, "not-a-uuid"))

    row.refresh_from_db()
    # The second call is a legitimate move onto an opaque key.
    assert row.owner_key == "not-a-uuid"


# ── wiring ──────────────────────────────────────────────────────────────


def test_the_subscription_is_registered():
    from stapel_core.comm import action_registry

    assert handle_user_merged in action_registry.handlers("user.merged")


def test_the_lifecycle_pair_check_is_green():
    """``stapel_core.lifecycle.E001`` — one half of an account's life cycle
    answered and not the other is an ERROR as of core 0.52.x."""
    from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

    assert check_lifecycle_pairs() == []


def test_the_consumes_schema_is_committed():
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "schemas" / "consumes" / "user.merged.json"
    )
    schema = json.loads(path.read_text())
    assert schema["title"] == "user.merged"
    assert set(schema["required"]) == {"from_user_id", "into_user_id"}


def test_owner_key_is_the_only_column_that_names_an_account():
    """The handler moves exactly one column. A second one would be silently
    stranded — fail here, not in production."""
    columns = {
        f"{model.__name__}.{field.name}"
        for model in (SearchDocument,)
        for field in model._meta.get_fields()
        if getattr(field, "name", "").endswith(("owner_key", "user_id", "author_id"))
    }
    assert columns == {"SearchDocument.owner_key"}
