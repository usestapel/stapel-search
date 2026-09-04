"""Write the ``search_number`` rows the already-indexed documents never got.

Until 0.14.7 a producer that hands over the ``features_search`` projection
instead of stapel-attributes DAOs produced NO numeric rows at all — the
projection carries values with no type, and the indexer had nothing to call
a range. A live stand therefore held a full ``search_document`` table beside
an empty ``search_number`` one: every listing carried a year and a mileage,
``r.year=2015..2020`` matched nothing, and the panel could draw no from/to
picker because the answer reported no bounds.

The fix is in the indexer; this is the back-fill for the documents indexed
before it. It re-derives the numbers from the row's OWN stored ``facets``,
so it needs no source pull, no comm call and no engine — a stand is caught
up in one pass over its own table.

Two deliberate limits:

- it only ADDS. A slug that already has a row is left alone, because a row
  written from a DAO knew the feature's type and this pass does not.
- it skips a slug whose stored terms carry a ``/``, which is how a path
  facet (``hierarchical_select``) is recognized without the schema: a
  root->leaf address is not a magnitude even when its first segment reads
  like one.

A full re-index (``search_rebuild``) does the same thing and more, at the
cost of re-pulling the whole corpus from its source. Reach for that one when
the documents themselves are stale; reach for this one when they are not.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Derive missing search_number rows from the indexed documents' own "
        "stored facets, so r.<slug> filters and range bounds work without a "
        "full re-index."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            dest="doc_type",
            help="Document type to back-fill (default: every type in the index).",
        )
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written and write nothing.",
        )

    def handle(self, *args, **options):
        from stapel_search.models import SearchDocument, SearchNumber
        from stapel_search.services import _numeric_code

        rows = SearchDocument.objects.all()
        if options["doc_type"]:
            rows = rows.filter(doc_type=options["doc_type"])

        batch_size = max(1, int(options["batch_size"]))
        dry_run = bool(options["dry_run"])
        scanned = 0
        written = 0
        slugs: set[str] = set()
        pending: list[SearchNumber] = []

        for row in rows.only("id", "facets", "facet_terms").iterator(
            chunk_size=batch_size
        ):
            scanned += 1
            facets = row.facets if isinstance(row.facets, dict) else {}
            if not facets:
                continue
            paths = {
                term.split("=", 1)[0]
                for term in (row.facet_terms or [])
                if isinstance(term, str) and "=" in term and "/" in term.split("=", 1)[1]
            }
            existing = set(
                SearchNumber.objects.filter(document_id=row.id).values_list(
                    "slug", flat=True
                )
            )
            for slug, values in facets.items():
                if slug in existing or slug in paths:
                    continue
                listed = list(values) if isinstance(values, list) else [values]
                number = _numeric_code([v for v in listed if v not in (None, "")])
                if number is None:
                    continue
                slugs.add(str(slug))
                written += 1
                pending.append(
                    SearchNumber(document_id=row.id, slug=str(slug), value=number)
                )
            if not dry_run and len(pending) >= batch_size:
                SearchNumber.objects.bulk_create(pending, ignore_conflicts=True)
                pending = []

        if pending and not dry_run:
            SearchNumber.objects.bulk_create(pending, ignore_conflicts=True)

        verb = "would write" if dry_run else "wrote"
        line = (
            f"search_backfill_numbers: scanned {scanned} document(s), {verb} "
            f"{written} number row(s) across {len(slugs)} slug(s)"
        )
        if slugs:
            line += f": {', '.join(sorted(slugs)[:20])}"
        self.stdout.write(self.style.SUCCESS(line) if written else line)
