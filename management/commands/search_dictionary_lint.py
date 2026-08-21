"""Check the merged dictionaries for the mistakes that quietly change recall.

A dictionary is contract data. A cycle in ``rewrites``, a term in two
equivalents groups, or a word that is both a stopword and a synonym member
does not crash anything — it just changes what users can find, in a way
nobody notices until somebody complains that a product "is not in the
catalogue".
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Lint the synonym/stopword dictionaries for every configured language."

    def add_arguments(self, parser):
        parser.add_argument("--lang", dest="languages", action="append", default=None)
        parser.add_argument("--strict", action="store_true")

    def handle(self, *args, **options):
        from stapel_search.conf import search_settings
        from stapel_search.text import lint_dictionary

        languages = options["languages"] or sorted(
            set(search_settings.FTS_CONFIGS or {}) | set(search_settings.DICTIONARIES or {})
        )
        found = 0
        for language in languages:
            problems = lint_dictionary(language)
            if not problems:
                self.stdout.write(self.style.SUCCESS(f"{language}: ok"))
                continue
            found += len(problems)
            for problem in problems:
                self.stdout.write(self.style.WARNING(f"{language}: {problem}"))
        if found and options["strict"]:
            raise CommandError(f"{found} dictionary problem(s)")
