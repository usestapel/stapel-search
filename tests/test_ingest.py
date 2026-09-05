"""Event -> pull: the invalidation path, its guarantees, and its refusals."""
from __future__ import annotations

import pytest

from stapel_search.registry import SourceSpec, register_source
from stapel_search.testing import DOC_TYPE, _document

pytestmark = pytest.mark.django_db


LISTING_SIGNALS = ("listing.published", "listing.updated", "listing.removed")


def _listing_source(store: dict) -> SourceSpec:
    """A source whose content Function reads from *store* — the pull half."""

    def mapper(payload: dict) -> object:
        return _document(
            doc_key=payload["key"],
            status=payload.get("status", "published"),
            title=payload.get("title", ""),
            seq=int(payload.get("seq") or 0),
        )

    return SourceSpec(
        doc_type=DOC_TYPE,
        mapper=mapper,
        content_function="listings.search_documents",
        export_function="listings.search_export",
        signals=LISTING_SIGNALS,
        removal_signals=("listing.removed",),
        key_fields=("listing_id", "key"),
        visible_statuses=frozenset({"published"}),
    )


@pytest.fixture
def wired(monkeypatch):
    """A registered source plus a stubbed content Function."""
    store: dict[str, dict] = {}
    register_source(_listing_source(store))

    def fake_call(name, payload):
        if name == "listings.search_documents":
            return {k: store[k] for k in payload["keys"] if k in store}
        if name == "listings.search_export":
            rows = [{"key": k, **v} for k, v in sorted(store.items())]
            return {"rows": rows, "cursor": None, "total": len(rows)}
        raise AssertionError(f"unexpected comm call {name}")

    monkeypatch.setattr("stapel_core.comm.call", fake_call)
    return store


def _event(name, payload, event_id="e-1", timestamp=1000):
    from stapel_core.bus.event import Event

    return Event(
        event_type=name, service="listings", payload=payload,
        event_id=event_id, timestamp=timestamp,
    )


def _dispatch(event):
    from stapel_search.actions import dispatch_source_signal

    dispatch_source_signal(event)


def test_event_is_a_signal_and_the_document_is_pulled(wired):
    """The payload carries identity; the content comes from the Function."""
    from stapel_search.models import SearchDocument

    wired["7"] = {"key": "7", "status": "published", "title": "Pulled title", "seq": 1000}
    _dispatch(_event("listing.published", {"listing_id": 7, "status": "published"}))

    row = SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="7")
    assert row.title == "Pulled title", "the title was never on the event"
    assert row.visible is True


def test_visibility_comes_from_the_document_not_the_event_name(wired):
    """A republished listing emits `listing.updated` with `status: pending`.

    No ``listing.removed`` is emitted in that case (spec §19.7), so an index
    that trusted event names would keep a withdrawn listing searchable.
    """
    from stapel_search.models import SearchDocument

    wired["7"] = {"key": "7", "status": "published", "title": "Live", "seq": 1000}
    _dispatch(_event("listing.published", {"listing_id": 7}, event_id="a", timestamp=1000))
    assert SearchDocument.objects.get(doc_key="7").visible is True

    wired["7"] = {"key": "7", "status": "pending", "title": "Live", "seq": 2000}
    _dispatch(_event("listing.updated", {"listing_id": 7}, event_id="b", timestamp=2000))
    assert SearchDocument.objects.get(doc_key="7").visible is False


def test_a_key_the_source_no_longer_serves_is_removed(wired):
    """Absent from the batch IS the source saying "deleted"."""
    from stapel_search.models import SearchDocument

    wired["7"] = {"key": "7", "status": "published", "title": "Here", "seq": 1000}
    _dispatch(_event("listing.published", {"listing_id": 7}, event_id="a"))
    assert SearchDocument.objects.get(doc_key="7").visible is True

    del wired["7"]
    _dispatch(_event("listing.removed", {"listing_id": 7}, event_id="b", timestamp=2000))
    assert SearchDocument.objects.get(doc_key="7").visible is False


def test_redelivery_is_idempotent(wired):
    from stapel_search.models import SearchDocument

    wired["7"] = {"key": "7", "status": "published", "title": "First", "seq": 1000}
    _dispatch(_event("listing.published", {"listing_id": 7}, event_id="same", timestamp=1000))
    wired["7"]["title"] = "Second"
    _dispatch(_event("listing.published", {"listing_id": 7}, event_id="same", timestamp=1000))
    assert SearchDocument.objects.get(doc_key="7").title == "First"
    assert SearchDocument.objects.filter(doc_key="7").count() == 1


def test_out_of_order_delivery_does_not_regress_the_document(wired):
    from stapel_search.models import SearchDocument

    wired["7"] = {"key": "7", "status": "published", "title": "Newer", "seq": 5000}
    _dispatch(_event("listing.updated", {"listing_id": 7}, event_id="new", timestamp=5000))

    wired["7"] = {"key": "7", "status": "published", "title": "Older", "seq": 100}
    _dispatch(_event("listing.updated", {"listing_id": 7}, event_id="old", timestamp=100))
    assert SearchDocument.objects.get(doc_key="7").title == "Newer"


def test_a_payload_without_a_key_is_logged_not_crashed(wired, caplog):
    _dispatch(_event("listing.published", {"nothing": "useful"}))
    assert any("carried no document key" in record.message for record in caplog.records)


def test_rebuild_reconciles_and_drops_what_the_source_lost(wired):
    from stapel_search.models import SearchDocument
    from stapel_search.services import rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    rebuild(DOC_TYPE)
    assert SearchDocument.objects.filter(visible=True).count() == 2

    del wired["2"]
    rebuild(DOC_TYPE)
    assert set(
        SearchDocument.objects.filter(visible=True).values_list("doc_key", flat=True)
    ) == {"1"}


def test_drift_check_reports_without_repairing(wired):
    from stapel_search.models import SearchDocument
    from stapel_search.services import drift_check, rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    rebuild(DOC_TYPE)
    assert drift_check(DOC_TYPE).in_sync is True

    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    report = drift_check(DOC_TYPE)
    assert report.in_sync is False
    assert report.missing_keys == ("2",)
    assert SearchDocument.objects.count() == 1, "drift_check must not repair silently"


def test_drift_check_names_an_orphan_apart_from_a_missing_key(wired):
    """A row the index still shows after its source row is gone, and a row
    the source has that the index has not pulled yet, are not the same
    problem — the report must say which is which."""
    from stapel_search.services import drift_check, rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    rebuild(DOC_TYPE)

    del wired["2"]  # the source row is gone; the index still has it
    rebuild(DOC_TYPE)  # default rebuild only TOMBSTONES it — still in the table

    report = drift_check(DOC_TYPE)
    assert report.orphaned_keys == ("2",)
    assert report.missing_keys == ()
    assert report.in_sync is False


def test_rebuild_without_prune_leaves_the_orphan_row_in_the_table(wired):
    """The behaviour this patch must not change: without --prune, a stale
    row is tombstoned, not deleted — the pre-existing contract."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    rebuild(DOC_TYPE)

    del wired["2"]
    report = rebuild(DOC_TYPE)

    assert report.removed == 1
    row = SearchDocument.objects.get(doc_key="2")
    assert row.visible is False, "still tombstoned, not gone"


def test_rebuild_prune_deletes_the_orphan_row_outright(wired):
    from stapel_search.models import SearchDocument
    from stapel_search.services import drift_check, rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    rebuild(DOC_TYPE)

    del wired["2"]
    rebuild(DOC_TYPE)  # tombstone it first, as a live deployment already has
    assert SearchDocument.objects.filter(doc_key="2").exists()

    report = rebuild(DOC_TYPE, prune=True)

    assert report.removed == 1
    assert not SearchDocument.objects.filter(doc_key="2").exists()
    assert drift_check(DOC_TYPE).in_sync is True


def test_rebuild_prune_dry_run_deletes_nothing(wired):
    from stapel_search.models import SearchDocument
    from stapel_search.services import rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    rebuild(DOC_TYPE)

    del wired["2"]

    report = rebuild(DOC_TYPE, prune=True, dry_run=True)

    assert report.removed == 1
    assert SearchDocument.objects.filter(doc_key="2").exists()


def test_rebuild_prune_dry_run_and_apply_agree_and_spare_every_live_row(wired):
    """dry-run and apply are the same query behind one flag: on an unchanged
    source they must report the same count, and apply must not touch a live
    row's identity — checked by surviving primary key, not just by count."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import rebuild

    for key in ("1", "2", "3", "4", "5"):
        wired[key] = {"key": key, "status": "published", "title": f"Doc {key}", "seq": int(key)}
    rebuild(DOC_TYPE)
    live_ids = set(
        SearchDocument.objects.filter(doc_key__in=("1", "2", "3")).values_list("id", flat=True)
    )

    del wired["4"]
    del wired["5"]

    dry = rebuild(DOC_TYPE, prune=True, dry_run=True)
    assert dry.removed == 2
    assert SearchDocument.objects.filter(doc_key__in=("4", "5")).count() == 2

    applied = rebuild(DOC_TYPE, prune=True, dry_run=False)
    assert applied.removed == dry.removed == 2
    assert not SearchDocument.objects.filter(doc_key__in=("4", "5")).exists()

    survivors = set(
        SearchDocument.objects.filter(doc_key__in=("1", "2", "3")).values_list("id", flat=True)
    )
    assert survivors == live_ids, "a live row must survive --prune with its original identity"


def test_prune_documents_reports_documents_not_the_cascade_total(wired):
    """`SearchNumber.document` cascades off `SearchDocument`: deleting one
    orphaned document with several `SearchNumber` rows makes
    `QuerySet.delete()`'s total larger than the document count. `removed`
    must report documents in both branches, so dry-run and apply agree."""
    from decimal import Decimal

    from stapel_search.models import SearchDocument, SearchNumber
    from stapel_search.services import prune_documents

    doc = SearchDocument.objects.create(doc_type=DOC_TYPE, doc_key="orphan")
    for slug, value in (("mileage", "1"), ("year", "2"), ("power", "3")):
        SearchNumber.objects.create(document=doc, slug=slug, value=Decimal(value))

    dry = prune_documents(DOC_TYPE, ["orphan"], dry_run=True)
    assert dry.removed == 1
    assert SearchDocument.objects.filter(doc_key="orphan").exists()

    applied = prune_documents(DOC_TYPE, ["orphan"])
    assert applied.removed == dry.removed == 1, (
        "removed must count the document, not the cascaded SearchNumber rows"
    )
    assert not SearchDocument.objects.filter(doc_key="orphan").exists()
    assert not SearchNumber.objects.filter(slug="mileage").exists()


def test_rebuild_prune_never_deletes_a_row_written_during_the_run(wired, monkeypatch):
    """A live signal can land a brand-new row while the snapshot is still
    being paged: the export already answered without it, so its key is in
    neither the old table state nor `seen`. That row is fresher than the
    run diffing against, not an orphan, and --prune must not delete it."""
    import stapel_search.services as services_module
    from stapel_search.models import SearchDocument
    from stapel_search.services import ingest, rebuild

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    rebuild(DOC_TYPE)

    original_pages = services_module._snapshot_pages

    def racing_pages(spec, batch_size):
        for rows, total in original_pages(spec, batch_size):
            # A listing.created signal lands mid-scan, after the export
            # already computed the page it is yielding.
            wired["9"] = {"key": "9", "status": "published", "title": "Late arrival", "seq": 9}
            ingest(DOC_TYPE, ["9"])
            yield rows, total

    monkeypatch.setattr(services_module, "_snapshot_pages", racing_pages)

    report = rebuild(DOC_TYPE, prune=True)

    assert SearchDocument.objects.filter(doc_key="9").exists(), (
        "a row written during the run must survive --prune"
    )
    assert report.removed == 0


def test_search_rebuild_command_prunes(wired, capsys):
    from django.core.management import call_command

    from stapel_search.models import SearchDocument

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    call_command("search_rebuild", "--type", DOC_TYPE)

    del wired["2"]
    call_command("search_rebuild", "--type", DOC_TYPE)  # tombstone

    call_command("search_rebuild", "--type", DOC_TYPE, "--prune")

    assert not SearchDocument.objects.filter(doc_key="2").exists()


def test_search_rebuild_command_refuses_dry_run_without_prune(wired):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}

    with pytest.raises(CommandError):
        call_command("search_rebuild", "--type", DOC_TYPE, "--dry-run")


def test_search_drift_check_command_reports_orphaned(wired, capsys):
    from django.core.management import call_command

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    wired["2"] = {"key": "2", "status": "published", "title": "Two", "seq": 2}
    call_command("search_rebuild", "--type", DOC_TYPE)

    del wired["2"]
    call_command("search_rebuild", "--type", DOC_TYPE)  # tombstone, not delete

    call_command("search_drift_check", "--type", DOC_TYPE)

    out = capsys.readouterr().out
    assert "orphaned=1" in out
    assert "orphaned: 2" in out


def test_signal_writes_an_audit_row_and_is_idempotent(wired):
    from stapel_search.models import SearchSignal
    from stapel_search.services import apply_signal

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    _dispatch(_event("listing.published", {"listing_id": 1}))

    assert apply_signal({"doc_type": DOC_TYPE, "doc_key": "1", "boost": 2.0}, event_id="s1")
    assert not apply_signal({"doc_type": DOC_TYPE, "doc_key": "1", "boost": 3.0}, event_id="s1")
    assert SearchSignal.objects.filter(kind="boost").count() == 1

    from stapel_search.models import SearchDocument

    assert SearchDocument.objects.get(doc_key="1").boost == 2.0


def test_boost_is_clamped_at_the_seam(wired):
    """A signal producer must not be able to own the whole result page."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import apply_signal

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    _dispatch(_event("listing.published", {"listing_id": 1}))
    apply_signal({"doc_type": DOC_TYPE, "doc_key": "1", "boost": 10**9}, event_id="huge")
    assert SearchDocument.objects.get(doc_key="1").boost == 5.0


def test_reindexing_does_not_undo_a_promotion(wired):
    """The source document and the signal write disjoint columns."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import apply_signal

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    _dispatch(_event("listing.published", {"listing_id": 1}, event_id="a", timestamp=1))
    apply_signal({"doc_type": DOC_TYPE, "doc_key": "1", "promoted": True}, event_id="p")

    wired["1"]["seq"] = 9999
    wired["1"]["title"] = "One, edited"
    _dispatch(_event("listing.updated", {"listing_id": 1}, event_id="b", timestamp=9999))

    row = SearchDocument.objects.get(doc_key="1")
    assert row.title == "One, edited"
    assert row.promoted is True


def test_user_deleted_erases_the_owner_documents(wired):
    """An index is derived data, and erasure still has to reach it."""
    from stapel_search.actions import handle_user_deleted
    from stapel_search.models import SearchDocument

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    _dispatch(_event("listing.published", {"listing_id": 1}))
    assert SearchDocument.objects.filter(doc_key="1").exists()

    handle_user_deleted(_event("user.deleted", {"user_id": "u1"}))
    assert not SearchDocument.objects.filter(doc_key="1").exists()


def test_tombstones_are_purged_after_the_retention_window(wired):
    from datetime import timedelta

    from django.utils import timezone

    from stapel_search.models import SearchDocument
    from stapel_search.services import purge_tombstones, remove_documents

    wired["1"] = {"key": "1", "status": "published", "title": "One", "seq": 1}
    _dispatch(_event("listing.published", {"listing_id": 1}))
    remove_documents(DOC_TYPE, ["1"])

    assert purge_tombstones() == 0, "a fresh tombstone still guards against a late event"
    SearchDocument.objects.filter(doc_key="1").update(
        indexed_at=timezone.now() - timedelta(days=99)
    )
    assert purge_tombstones() == 1


def test_an_unregistered_type_is_a_named_refusal():
    from stapel_search.registry import SourceNotRegistered, get_source

    with pytest.raises(SourceNotRegistered) as excinfo:
        get_source("never-registered")
    assert "never-registered" in str(excinfo.value)


def test_features_search_fallback_is_lossy_but_declared():
    """A host that will not hand over DAOs still gets terms, not silence."""
    from stapel_search.services import build_facets

    doc = _document(
        doc_key="x",
        features={},
        features_search={"brand": ["apple"], "color": ["red", "blue"]},
    )
    facets, terms, numbers, _ = build_facets(doc)
    assert facets == {"brand": ["apple"], "color": ["red", "blue"]}
    assert set(terms) == {"brand=apple", "color=red", "color=blue"}
    # The loss is real and visible: no type means no range, so nothing lands
    # in the numeric side table.
    assert numbers == {}


# --- reconcile: the sweep for writes that never became events ---------------
#
# The Д50 ghosts: `Listing.status = "archived"; save()` through a path that
# emitted nothing (a queryset .update(), an older stapel-listings) leaves the
# index serving cards whose click answers «снято с публикации». reconcile()
# re-pulls every VISIBLE row through the same ingest path an event uses, so
# the pulled document's status — not the missing event — decides.


def test_reconcile_drops_rows_whose_source_is_no_longer_published(wired):
    from stapel_search.models import SearchDocument
    from stapel_search.services import reconcile

    wired["1"] = {"key": "1", "status": "published", "title": "Live", "seq": 1000}
    wired["2"] = {"key": "2", "status": "published", "title": "Ghost-archived", "seq": 1000}
    wired["3"] = {"key": "3", "status": "published", "title": "Ghost-deleted", "seq": 1000}
    for key in ("1", "2", "3"):
        _dispatch(_event("listing.published", {"listing_id": int(key)}, event_id=f"e{key}"))
    assert SearchDocument.objects.filter(visible=True).count() == 3

    # The source moves on WITHOUT an event: one archived, one gone entirely.
    wired["2"]["status"] = "archived"
    wired["2"]["seq"] = 2000
    del wired["3"]

    report = reconcile(DOC_TYPE)
    assert report.removed == 1  # key "3": absent from the source's answer
    assert SearchDocument.objects.get(doc_key="1").visible is True
    assert SearchDocument.objects.get(doc_key="2").visible is False
    assert SearchDocument.objects.get(doc_key="3").visible is False


def test_reconcile_pages_past_rows_it_just_tombstoned(wired):
    """Keyset paging: flipping a row invisible must not shift the cursor."""
    from stapel_search.models import SearchDocument
    from stapel_search.services import reconcile

    for i in range(1, 6):
        wired[str(i)] = {"key": str(i), "status": "published", "title": f"t{i}", "seq": 1000}
        _dispatch(_event("listing.published", {"listing_id": i}, event_id=f"e{i}"))
    for i in range(1, 5):
        wired[str(i)]["status"] = "archived"
        wired[str(i)]["seq"] = 2000

    reconcile(DOC_TYPE, batch_size=2)
    assert SearchDocument.objects.filter(visible=True).count() == 1
    assert SearchDocument.objects.get(doc_key="5").visible is True


def test_reconcile_management_command(wired):
    from django.core.management import call_command
    from stapel_search.models import SearchDocument

    wired["9"] = {"key": "9", "status": "published", "title": "Ghost", "seq": 1000}
    _dispatch(_event("listing.published", {"listing_id": 9}, event_id="e9"))
    wired["9"]["status"] = "archived"
    wired["9"]["seq"] = 2000

    call_command("search_reconcile", "--type", DOC_TYPE)
    assert SearchDocument.objects.get(doc_key="9").visible is False
