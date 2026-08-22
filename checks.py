"""Django system checks — what a deploy must not get past.

Level policy differs from ``stapel-geo`` deliberately. There, a broken
search backend is ``W`` because "a broken search backend only breaks the
search verbs" (``stapel_geo/checks.py:3-7``). Here the search verbs ARE the
module: a service that came up with an unusable backend answers 5xx to its
entire reason for existing, so the backend checks are ``E``.

The ``W`` checks are the other half of §11's discipline, turned on
ourselves: an empty source registry, an attribute type indexed by an
unreviewed default, a catch-up job nobody scheduled, a sort with no signal
behind it, and terms long enough to stop discriminating. Each of them is a
state that otherwise reaches production as silence.
"""
from __future__ import annotations

import inspect

from django.core import checks


@checks.register("stapel_search")
def check_backend(app_configs, **kwargs):
    """E001/E002: the configured engine imports and implements the protocol."""
    from .backends.base import VERBS, missing_verbs
    from .conf import search_settings

    try:
        backend = search_settings.BACKEND
    except ImportError as exc:
        return [
            checks.Error(
                f"STAPEL_SEARCH['BACKEND'] cannot be imported: {exc}",
                hint="Fix the dotted path, or install the missing extra "
                     "(pip install 'stapel-search[meili]' for the Meilisearch backend). "
                     "Every search endpoint answers 503 until it resolves.",
                id="stapel_search.E001",
            )
        ]
    if not inspect.isclass(backend) or missing_verbs(backend):
        absent = ", ".join(missing_verbs(backend)) if inspect.isclass(backend) else ", ".join(VERBS)
        return [
            checks.Error(
                f"STAPEL_SEARCH['BACKEND'] resolves to {backend!r}, which does not "
                f"implement the SearchBackend protocol (missing: {absent}).",
                hint="Implement stapel_search.backends.base.SearchBackend and run "
                     "stapel_search.testing.backend_conformance against it — a backend "
                     "without that suite green is not a backend.",
                id="stapel_search.E002",
            )
        ]
    return []


#: ``(app_label, name)`` of the migration that creates ``pg_trgm`` (see
#: ``migrations/0002_postgres_index_structures.py``). Kept as one constant so
#: the check and the migration cannot name it differently by typo.
_EXTENSION_MIGRATION = ("search", "0002_postgres_index_structures")


def _extension_migration_applied() -> bool:
    """Has THIS module's own extension-creating migration run yet?

    Same seam ``stapel_core``'s ``stapel_preflight`` uses
    (``django.db.migrations.loader.MigrationLoader``, read-only, no schema
    touched) to answer "is this app's migration state ahead of the DB" —
    reused here instead of re-deriving it, because the question is the same
    one: does the database already reflect a migration this app shipped.
    """
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    return _EXTENSION_MIGRATION in loader.applied_migrations


@checks.register("stapel_search")
def check_postgres_backend_has_postgres(app_configs, **kwargs):
    """E003: the Postgres backend on a non-Postgres connection, or no pg_trgm.

    It raises rather than degrading to ``icontains``, so this check is the
    difference between finding out at deploy time and finding out from a
    user whose search returned nothing.

    One deadlock this must not be: Django runs system checks before
    ``migrate`` (some deploy scripts gate on ``manage.py check`` explicitly,
    e.g. via ``stapel_preflight``), and it is THIS module's own migration
    0002 that creates ``pg_trgm`` — so on a fresh database, before 0002 has
    run, the extension is genuinely absent and erroring here would refuse
    the very migrate that fixes it, forever. A client fleet hit exactly
    this restart loop and worked around it at the postgres-bootstrap layer
    (``POSTGRES_EXTENSIONS`` in the fleet's docker-compose configuration,
    pre-creating the extension before any app connects). That is a
    legitimate deployment model, but it must not be the only escape from a
    deadlock this module creates — so the check stays quiet about a missing
    extension for as long as 0002 has not applied yet. Once 0002 HAS applied
    and pg_trgm is still missing (privilege denied on a managed Postgres is
    the common case — see 0002's own comment), the error is real and fires.
    """
    from django.db import connection

    from .conf import search_settings

    try:
        backend = search_settings.BACKEND
    except ImportError:
        return []  # already reported by E001
    if not inspect.isclass(backend) or backend.__name__ != "PostgresSearchBackend":
        return []

    if connection.vendor != "postgresql":
        return [
            checks.Error(
                "STAPEL_SEARCH['BACKEND'] is PostgresSearchBackend but the default "
                f"database vendor is {connection.vendor!r}.",
                hint="Use stapel_search.backends.naive.NaiveSearchBackend for SQLite "
                     "demos and tests — it declares typo_tolerance: False instead of "
                     "quietly answering worse.",
                id="stapel_search.E003",
            )
        ]
    try:
        if not backend.has_trigram() and _extension_migration_applied():
            return [
                checks.Error(
                    "The pg_trgm extension is not installed, so the typo-tolerant "
                    "arm of the Postgres backend cannot run.",
                    hint="Migration 0002 ran but could not create the extension "
                         "(insufficient privilege on a managed Postgres is the usual "
                         "cause — it logs a warning rather than failing the deploy). "
                         "Run CREATE EXTENSION pg_trgm; as a superuser, or pre-create "
                         "it at the postgres-bootstrap layer before this app connects.",
                    id="stapel_search.E003",
                )
            ]
    except Exception:  # noqa: BLE001 - no database at check time is not our error
        return []
    return []


@checks.register("stapel_search")
def check_sources(app_configs, **kwargs):
    """E004 / W001: the source registry resolves, and is not empty."""
    from .registry import get_sources

    try:
        sources = get_sources()
    except Exception as exc:  # noqa: BLE001 - any resolution failure is the finding
        return [
            checks.Error(
                f"STAPEL_SEARCH['SOURCES'] has an unresolvable entry: {exc}",
                hint="Each value is a dotted path to a stapel_search.registry.SourceSpec "
                     "(or a callable returning one); None removes a builtin.",
                id="stapel_search.E004",
            )
        ]
    if not sources:
        return [
            checks.Warning(
                "STAPEL_SEARCH['SOURCES'] is empty: stapel-search is installed but "
                "indexes nothing.",
                hint="A composite declares the source — it is the one place allowed to "
                     "know both the corpus and the index. See MODULE.md 'Registering a "
                     "source'.",
                id="stapel_search.W001",
            )
        ]
    return []


@checks.register("stapel_search")
def check_default_facet_mappings(app_configs, **kwargs):
    """W002: an attribute type is being indexed by the unreviewed default."""
    from .registry import defaulted_type_slugs

    slugs = sorted(defaulted_type_slugs())
    if not slugs:
        return []
    return [
        checks.Warning(
            "Attribute type(s) " + ", ".join(repr(s) for s in slugs) + " are indexed by "
            "the generic default mapping (value as a term).",
            hint="Declare STAPEL_SEARCH['FACET_MAPPINGS'][<type>] — or confirm the "
                 "default deliberately. A silent default is the disease the index "
                 "contract exists to prevent.",
            id="stapel_search.W002",
        )
    ]


@checks.register("stapel_search")
def check_beat_schedule(app_configs, **kwargs):
    """W003: a beat schedule exists but nothing schedules the catch-up."""
    from django.conf import settings

    from .tasks import REINDEX_STALE_TASK

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    if not schedule:
        return []
    if any(
        (entry or {}).get("task") == REINDEX_STALE_TASK
        for entry in schedule.values()
        if isinstance(entry, dict)
    ):
        return []
    return [
        checks.Warning(
            "CELERY_BEAT_SCHEDULE has no entry for "
            f"{REINDEX_STALE_TASK}: nothing catches the index up after a lost event.",
            hint="CELERY_BEAT_SCHEDULE = {**get_search_beat_schedule(), ...}",
            id="stapel_search.W003",
        )
    ]


@checks.register("stapel_search")
def check_popular_sort_has_a_signal(app_configs, **kwargs):
    """W004: ``popular`` is offered but no popularity signal ever arrived.

    The module's own rule applied to itself: a declared capability with
    nothing behind it is the thing §11 exists to catch, and a sort that
    orders every document by zero is exactly that.
    """
    from .conf import search_settings

    if "popular" not in (search_settings.SORTS or ()):
        return []
    try:
        from .models import SearchSignal

        if SearchSignal.objects.filter(kind=SearchSignal.KIND_POPULARITY).exists():
            return []
    except Exception:  # noqa: BLE001 - no database at check time
        return []
    return [
        checks.Warning(
            "'popular' is in STAPEL_SEARCH['SORTS'] but no search.signal carrying "
            "popularity has ever been received: the sort orders every document by 0.",
            hint="Remove it until an emitter exists, or point one at search.signal.",
            id="stapel_search.W004",
        )
    ]


@checks.register("stapel_search")
def check_category_path_provider(app_configs, **kwargs):
    """W006: no ``categories.path`` provider, so rollup cannot work.

    Declared by canonical name before it exists — the
    ``stapel-shop/projections.py:23-35`` move. Without it, filtering by an
    exact category still works and filtering by a parent finds nothing,
    which is precisely the kind of half-working that must not be silent.
    """
    from stapel_core.comm import function_unreachable_reason

    from .conf import search_settings

    name = search_settings.CATEGORY_PATH_FUNCTION
    try:
        reason = function_unreachable_reason(name)
    except Exception:  # noqa: BLE001 - comm not configured yet at check time
        return []
    if not reason:
        return []
    return [
        checks.Warning(
            f"The comm Function {name!r} is unreachable ({reason}), so category "
            "ancestry cannot be resolved: category_path degrades to one segment and "
            "filtering by a parent category finds no descendants.",
            hint="Provide it from stapel-categories (or a composite) as "
                 "{'category_ids': [...]} -> {id: [root..leaf]}. Until then every "
                 "answer carries degraded: ['category_rollup'].",
            id="stapel_search.W006",
        )
    ]


@checks.register("stapel_search")
def check_truncated_terms(app_configs, **kwargs):
    """W005: facet terms are being cut, so two options may share one count."""
    from .services import MAX_TERM_CHARS

    try:
        from .models import SearchDocument

        row = (
            SearchDocument.objects.exclude(facet_terms=[])
            .values_list("facet_terms", flat=True)
            .first()
        )
    except Exception:  # noqa: BLE001 - no database at check time
        return []
    if not row:
        return []
    if not any(isinstance(term, str) and len(term) >= MAX_TERM_CHARS for term in row):
        return []
    return [
        checks.Warning(
            f"Facet terms are being truncated at {MAX_TERM_CHARS} characters: two "
            "distinct option values can collapse into one count.",
            hint="Shorten the option codes, or raise the term cap deliberately.",
            id="stapel_search.W005",
        )
    ]


__all__ = [
    "check_backend",
    "check_beat_schedule",
    "check_category_path_provider",
    "check_default_facet_mappings",
    "check_popular_sort_has_a_signal",
    "check_postgres_backend_has_postgres",
    "check_sources",
    "check_truncated_terms",
]
