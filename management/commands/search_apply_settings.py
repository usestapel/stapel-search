"""Push engine-side settings: searchable/filterable attributes, dictionaries.

A no-op on Postgres (its schema IS its settings, and dictionaries are
applied query-side because a library cannot write files into the database
server's ``$SHAREDIR/tsearch_data``). On Meilisearch it is what makes
synonyms and stopwords take effect — run it after editing a dictionary.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply engine-side index settings and dictionaries for a document type."

    def add_arguments(self, parser):
        parser.add_argument("--type", dest="doc_type", required=True)

    def handle(self, *args, **options):
        from stapel_search.backends import get_backend
        from stapel_search.services import apply_settings

        doc_type = options["doc_type"]
        apply_settings(doc_type)
        backend = get_backend()
        self.stdout.write(
            self.style.SUCCESS(
                f"settings applied for {doc_type} on backend "
                f"{getattr(backend, 'name', '?')}"
            )
        )
        if not backend.capabilities().synonyms_native:
            self.stdout.write(
                "note: this engine has no native synonyms — equivalents are applied as "
                "query expansion, so phrase queries through a synonym do not match "
                "(reported as degraded: ['phrase_synonyms'])."
            )
