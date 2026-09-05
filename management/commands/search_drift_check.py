"""Compare the index against the owner's snapshot, without rewriting it.

Detection is a separate act from repair on purpose: a job that silently
fixes drift also silently hides that drift keeps happening, and the number
that matters operationally is how often this reports non-zero.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Report drift between the search index and its source of truth."

    def add_arguments(self, parser):
        parser.add_argument("--type", dest="doc_type", required=True)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero when the index is not in sync (for CI / monitoring).",
        )

    def handle(self, *args, **options):
        from stapel_search.registry import SourceNotRegistered
        from stapel_search.services import drift_check

        try:
            report = drift_check(options["doc_type"], batch_size=options["batch_size"])
        except SourceNotRegistered as exc:
            raise CommandError(str(exc)) from None

        line = (
            f"{report.name}: local={report.local} source={report.source} "
            f"stale={report.stale} missing={len(report.missing_keys)} "
            f"orphaned={len(report.orphaned_keys)}"
        )
        if report.in_sync:
            self.stdout.write(self.style.SUCCESS(line + " IN SYNC"))
            return
        self.stdout.write(self.style.WARNING(line + " DRIFTED"))
        for key in report.missing_keys[:20]:
            self.stdout.write(f"  missing: {key}")
        if len(report.missing_keys) > 20:
            self.stdout.write(f"  ... and {len(report.missing_keys) - 20} more")
        # A missing key needs a re-pull; an orphaned one needs `rebuild
        # --prune` — printed under its own label so the two are never read
        # as the same problem.
        for key in report.orphaned_keys[:20]:
            self.stdout.write(f"  orphaned: {key}")
        if len(report.orphaned_keys) > 20:
            self.stdout.write(f"  ... and {len(report.orphaned_keys) - 20} more")
        if options["strict"]:
            raise CommandError("search index is not in sync with its source")
