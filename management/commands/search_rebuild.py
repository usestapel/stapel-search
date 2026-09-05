"""Rebuild the index for one document type from the source of truth.

The backfill and the recovery path in one command, over the source's
snapshot Function with the ``{cursor, limit} -> {rows, cursor, total}``
contract borrowed verbatim from ``stapel_core.comm.projections``. It is
also the switch procedure: point ``STAPEL_SEARCH["BACKEND"]`` at another
engine, run this, and no module code has changed — which is the test of
whether the seam is real.

``--prune`` hard-deletes index rows whose key the source's full snapshot no
longer names — visible or already tombstoned by an earlier run — instead of
leaving them for ``purge_tombstones``'s retention window to catch up (or
never, on a deployment with no beat scheduled). Without it, rebuild keeps
its original behaviour: only currently visible stale rows are tombstoned.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Rebuild the search index for a document type from its source of truth."

    def add_arguments(self, parser):
        parser.add_argument("--type", dest="doc_type", required=True)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--apply-settings",
            action="store_true",
            help="Push engine settings (synonyms, stopwords, attributes) first.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help=(
                "Hard-delete index rows whose key is absent from the source's "
                "full key set for this type (visible or already tombstoned), "
                "instead of leaving them for the retention beat to purge later."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --prune, report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        from stapel_search.registry import SourceNotRegistered
        from stapel_search.services import apply_settings, rebuild

        doc_type = options["doc_type"]
        prune = options["prune"]
        dry_run = options["dry_run"]
        if dry_run and not prune:
            raise CommandError("--dry-run only applies together with --prune")

        try:
            if options["apply_settings"]:
                apply_settings(doc_type)
                self.stdout.write(f"engine settings applied for {doc_type}")
            report = rebuild(
                doc_type, batch_size=options["batch_size"], prune=prune, dry_run=dry_run
            )
        except SourceNotRegistered as exc:
            raise CommandError(str(exc)) from None

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"{doc_type}: dry run — indexed {report.indexed}, would prune "
                    f"{report.removed}, stale {report.skipped_stale}, duplicate "
                    f"{report.skipped_duplicate}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{doc_type}: indexed {report.indexed}, removed {report.removed}, "
                    f"stale {report.skipped_stale}, duplicate {report.skipped_duplicate}"
                )
            )
        if report.truncated_terms:
            self.stdout.write(
                self.style.WARNING(
                    f"{report.truncated_terms} facet term(s) were truncated — two option "
                    "values may now share one count (search.W005)"
                )
            )
