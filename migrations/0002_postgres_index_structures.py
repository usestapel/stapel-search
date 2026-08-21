"""Postgres-only index structures — expand phase, vendor-guarded.

Three columns Django does not model, plus the GIN indexes that make them
worth having. They are deliberately outside ``models.py`` so the model
layer stays portable and the naive backend can run the same conformance
suite on SQLite; they are maintained by ``PostgresSearchBackend.upsert``,
because maintaining an engine's own structures is what a backend is for.

Why ``text_vec`` is a plain column rather than ``GENERATED ALWAYS AS ...
STORED``: a generated column has to name its text-search configuration in
the DDL, which would freeze ``STAPEL_SEARCH["FTS_CONFIGS"]`` into a
migration. Adding a language would then be a schema change instead of a
settings change, and the per-document config choice (``language`` is a
row value) would have to become a hard-coded CASE. The backend writes the
vector on upsert with the configured regconfig instead.

``CREATE EXTENSION pg_trgm`` needs elevated privileges on some managed
Postgres offerings. If it fails, the migration does NOT fail the deploy —
it logs, and ``stapel_search.E003`` reports the missing extension at every
``manage.py check`` from then on. A loud, repeated, actionable check beats
a deploy that cannot proceed, and beats silence in both directions.
"""
from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_COLUMNS = (
    # The stemmed arm. Weights A/B/C are applied by the backend on write.
    "ALTER TABLE search_document ADD COLUMN IF NOT EXISTS text_vec tsvector",
    # The COUNTING structure: remaining-option counts unfold this with one
    # lateral unnest per candidate. Over jsonb the same count would cost
    # jsonb_each + jsonb_array_elements — two lateral joins and a parse per
    # row, which the benchmark is what told us not to pay.
    "ALTER TABLE search_document ADD COLUMN IF NOT EXISTS facet_terms_arr text[]",
    "ALTER TABLE search_document ADD COLUMN IF NOT EXISTS category_path_arr text[]",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS search_doc_tsv_idx ON search_document USING gin (text_vec)",
    "CREATE INDEX IF NOT EXISTS search_doc_trgm_idx ON search_document "
    "USING gin (text_plain gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS search_doc_terms_idx ON search_document "
    "USING gin (facet_terms_arr)",
    # jsonb_path_ops rather than the default jsonb_ops: 2-3x smaller and
    # faster on `@>`, and containment is the only operator a facet filter
    # ever needs.
    "CREATE INDEX IF NOT EXISTS search_doc_facets_idx ON search_document "
    "USING gin (facets jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS search_doc_catpath_idx ON search_document "
    "USING gin (category_path_arr)",
)

_DROP = (
    "DROP INDEX IF EXISTS search_doc_catpath_idx",
    "DROP INDEX IF EXISTS search_doc_facets_idx",
    "DROP INDEX IF EXISTS search_doc_terms_idx",
    "DROP INDEX IF EXISTS search_doc_trgm_idx",
    "DROP INDEX IF EXISTS search_doc_tsv_idx",
    "ALTER TABLE search_document DROP COLUMN IF EXISTS category_path_arr",
    "ALTER TABLE search_document DROP COLUMN IF EXISTS facet_terms_arr",
    "ALTER TABLE search_document DROP COLUMN IF EXISTS text_vec",
)


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception as exc:  # noqa: BLE001 - privilege, not correctness
            logger.warning(
                "stapel-search: could not create the pg_trgm extension (%s). The "
                "typo-tolerant arm will be unavailable and stapel_search.E003 will "
                "report it on every check. Run 'CREATE EXTENSION pg_trgm;' as a "
                "superuser to enable it.",
                exc,
            )
        for statement in _COLUMNS:
            cursor.execute(statement)
        for statement in _INDEXES:
            try:
                cursor.execute(statement)
            except Exception as exc:  # noqa: BLE001 - trgm index needs the extension
                logger.warning("stapel-search: index not created (%s): %s", statement, exc)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for statement in _DROP:
            cursor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [("search", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
