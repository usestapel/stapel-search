"""Drop index rows whose source document is no longer publicly served.

The sweep for writes that never became events (the Д50 ghost cards): every
VISIBLE row is re-pulled through the same ingest path a live signal uses, and
the pulled document's status — not the missing event — decides. Idempotent;
safe to run on a live stand; a healthy deployment reports 0 dropped.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Re-pull every visible index row from its source; tombstone the rows "
        "whose document is gone or no longer in a visible status."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            dest="doc_type",
            help="Document type to sweep (default: every registered source).",
        )
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        from stapel_search.models import SearchDocument
        from stapel_search.registry import SourceNotRegistered, get_sources
        from stapel_search.services import reconcile

        doc_types = [options["doc_type"]] if options["doc_type"] else sorted(get_sources())
        if not doc_types:
            self.stdout.write("no sources registered — nothing to reconcile")
            return

        for doc_type in doc_types:
            visible_before = SearchDocument.objects.filter(
                doc_type=doc_type, visible=True
            ).count()
            try:
                report = reconcile(doc_type, batch_size=options["batch_size"])
            except SourceNotRegistered as exc:
                raise CommandError(str(exc)) from None
            visible_after = SearchDocument.objects.filter(
                doc_type=doc_type, visible=True
            ).count()
            dropped = visible_before - visible_after
            line = (
                f"{doc_type}: checked {visible_before}, dropped {dropped} ghost(s) "
                f"(re-indexed {report.indexed}, source-missing {report.removed}, "
                f"stale-skipped {report.skipped_stale})"
            )
            self.stdout.write(
                self.style.SUCCESS(line) if dropped == 0 else self.style.WARNING(line)
            )
