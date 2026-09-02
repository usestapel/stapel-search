"""The vector store: portable metadata table + conditional pgvector column.

The table is plain Django — it exists everywhere, SQLite included, so the
admin, counts and pruning never depend on an extension. The ``embedding``
column is added HERE only when the deployment already has pgvector
installed; everywhere else the store reports itself unavailable and the
feature degrades to a declared shortfall. A deployment that installs the
extension later heals through ``vector.store.ensure_schema()`` (the index
builder calls it) — no fake migration required.
"""
from django.db import migrations, models


def _add_embedding_column(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        if cursor.fetchone() is None:
            return
        cursor.execute(
            "ALTER TABLE search_vector_embedding ADD COLUMN embedding vector NULL"
        )


def _drop_embedding_column(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE search_vector_embedding DROP COLUMN IF EXISTS embedding"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0002_postgres_index_structures"),
    ]

    operations = [
        migrations.CreateModel(
            name="VectorEmbedding",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("kind", models.CharField(max_length=32)),
                ("key", models.CharField(max_length=160)),
                ("text", models.TextField()),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("model_tag", models.CharField(max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "search_vector_embedding"},
        ),
        migrations.AddConstraint(
            model_name="vectorembedding",
            constraint=models.UniqueConstraint(
                fields=("kind", "key"), name="search_vector_kind_key_uniq"
            ),
        ),
        migrations.AddIndex(
            model_name="vectorembedding",
            index=models.Index(
                fields=["kind", "model_tag"], name="search_vector_kind_tag_idx"
            ),
        ),
        migrations.RunPython(_add_embedding_column, _drop_embedding_column),
    ]
