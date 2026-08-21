"""System checks and the settings namespace.

The W-checks are §11's discipline turned on this module: an empty source
registry, a type indexed by an unreviewed default, a catch-up job nobody
scheduled, a sort with no signal behind it. Each is a state that otherwise
reaches production as silence.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from stapel_search import checks

pytestmark = pytest.mark.django_db


def _ids(findings):
    return sorted(f.id for f in findings)


# --- backend seam ----------------------------------------------------------


def test_e001_a_backend_that_cannot_be_imported():
    with override_settings(STAPEL_SEARCH={"BACKEND": "nope.NotThere"}):
        assert _ids(checks.check_backend(None)) == ["stapel_search.E001"]


def test_e002_a_backend_that_is_not_a_backend():
    with override_settings(STAPEL_SEARCH={"BACKEND": "decimal.Decimal"}):
        findings = checks.check_backend(None)
        assert _ids(findings) == ["stapel_search.E002"]
        assert "capabilities" in findings[0].msg


def test_the_shipped_backends_pass_the_duck_type_check():
    for path in (
        "stapel_search.backends.naive.NaiveSearchBackend",
        "stapel_search.backends.postgres.PostgresSearchBackend",
        "stapel_search.backends.meili.MeilisearchBackend",
        "stapel_search.backends.opensearch.OpenSearchBackend",
    ):
        with override_settings(STAPEL_SEARCH={"BACKEND": path}):
            assert checks.check_backend(None) == [], path


def test_e003_the_postgres_backend_on_sqlite():
    """It refuses rather than degrading into icontains, so the check must
    catch it at deploy time and not leave it to a user's empty result page."""
    from django.db import connection

    with override_settings(
        STAPEL_SEARCH={"BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend"}
    ):
        findings = checks.check_postgres_backend_has_postgres(None)
    if connection.vendor == "postgresql":
        assert findings == []
    else:
        assert _ids(findings) == ["stapel_search.E003"]
        assert "naive" in findings[0].hint


def test_e003_is_quiet_for_another_backend():
    with override_settings(
        STAPEL_SEARCH={"BACKEND": "stapel_search.backends.naive.NaiveSearchBackend"}
    ):
        assert checks.check_postgres_backend_has_postgres(None) == []


def test_extension_migration_applied_reflects_the_real_migration_state():
    """The test DB is fully migrated (pytest-django applies migrations, and
    0002's forwards() marks itself applied on every vendor, even sqlite,
    where its SQL is skipped by its own vendor guard) — so this must read
    True regardless of which tier is running."""
    assert checks._extension_migration_applied() is True


def test_e003_is_quiet_before_migration_0002_has_applied(monkeypatch):
    """The deadlock the darom.ai fleet hit: on a fresh database pg_trgm is
    genuinely absent before 0002 runs, and this check must not refuse the
    very migrate that would create it (see checks.py's docstring)."""
    from django.db import connection

    from stapel_search.backends.postgres import PostgresSearchBackend

    monkeypatch.setattr(PostgresSearchBackend, "has_trigram", staticmethod(lambda: False))
    monkeypatch.setattr(checks, "_extension_migration_applied", lambda: False)
    with override_settings(
        STAPEL_SEARCH={"BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend"}
    ):
        findings = checks.check_postgres_backend_has_postgres(None)
    if connection.vendor == "postgresql":
        assert findings == []
    else:
        # off postgres entirely, the vendor guard fires first — unrelated to
        # the migration-ordering question this test is about
        assert _ids(findings) == ["stapel_search.E003"]
        assert "naive" in findings[0].hint


def test_e003_fires_once_0002_has_applied_and_the_extension_is_still_missing(monkeypatch):
    """Once this module's own migration has had its chance and pg_trgm is
    STILL missing (privilege denied on a managed Postgres, most likely), the
    finding is real again."""
    from django.db import connection

    from stapel_search.backends.postgres import PostgresSearchBackend

    monkeypatch.setattr(PostgresSearchBackend, "has_trigram", staticmethod(lambda: False))
    monkeypatch.setattr(checks, "_extension_migration_applied", lambda: True)
    with override_settings(
        STAPEL_SEARCH={"BACKEND": "stapel_search.backends.postgres.PostgresSearchBackend"}
    ):
        findings = checks.check_postgres_backend_has_postgres(None)
    if connection.vendor == "postgresql":
        assert _ids(findings) == ["stapel_search.E003"]
        assert "privilege" in findings[0].hint
    else:
        assert _ids(findings) == ["stapel_search.E003"]
        assert "naive" in findings[0].hint


# --- sources ---------------------------------------------------------------


def test_w001_an_empty_source_registry():
    findings = checks.check_sources(None)
    assert _ids(findings) == ["stapel_search.W001"]
    assert "indexes nothing" in findings[0].msg


def test_w001_is_quiet_once_a_source_is_registered():
    from stapel_search.registry import register_source
    from stapel_search.testing import CONFORMANCE_SOURCE

    register_source(CONFORMANCE_SOURCE)
    assert checks.check_sources(None) == []


def test_e004_an_unresolvable_source_entry():
    with override_settings(STAPEL_SEARCH={"SOURCES": {"listing": "nowhere.LISTING"}}):
        findings = checks.check_sources(None)
        assert _ids(findings) == ["stapel_search.E004"]


def test_a_broken_source_entry_fails_wiring_loudly():
    """Configured-but-broken must not be silent (the stapel-docs INGEST canon)."""
    from django.core.exceptions import ImproperlyConfigured

    from stapel_search.actions import wire_sources

    with override_settings(STAPEL_SEARCH={"SOURCES": {"listing": "nowhere.LISTING"}}):
        with pytest.raises(ImproperlyConfigured):
            wire_sources()


# --- the module applying its own rules to itself ---------------------------


def test_w003_a_beat_schedule_with_no_catch_up_entry():
    with override_settings(CELERY_BEAT_SCHEDULE={"something-else": {"task": "other"}}):
        findings = checks.check_beat_schedule(None)
        assert _ids(findings) == ["stapel_search.W003"]


def test_w003_is_quiet_with_the_shipped_schedule():
    pytest.importorskip("celery", reason="celery is an optional dependency")
    from stapel_search.tasks import get_search_beat_schedule

    with override_settings(CELERY_BEAT_SCHEDULE=get_search_beat_schedule()):
        assert checks.check_beat_schedule(None) == []


def test_the_beat_helper_names_celery_when_it_is_missing(monkeypatch):
    """Celery is optional, so its absence must read as a named condition."""
    import builtins

    real_import = builtins.__import__

    def no_celery(name, *args, **kwargs):
        if name.startswith("celery"):
            raise ImportError("no celery here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_celery)
    from stapel_search.tasks import get_search_beat_schedule

    with pytest.raises(ImportError, match="search_reindex_stale"):
        get_search_beat_schedule()


def test_w003_is_quiet_when_there_is_no_beat_at_all():
    """Celery is optional; a cron-driven deployment is not doing it wrong."""
    assert checks.check_beat_schedule(None) == []


def test_w004_popular_offered_with_no_popularity_signal():
    with override_settings(STAPEL_SEARCH={"SORTS": ("relevance", "popular")}):
        findings = checks.check_popular_sort_has_a_signal(None)
        assert _ids(findings) == ["stapel_search.W004"]
        assert "orders every document by 0" in findings[0].msg


def test_w004_is_quiet_once_a_signal_has_arrived():
    from stapel_search.models import SearchSignal

    SearchSignal.objects.create(
        doc_type="listing", doc_key="1", kind=SearchSignal.KIND_POPULARITY, value=5
    )
    with override_settings(STAPEL_SEARCH={"SORTS": ("relevance", "popular")}):
        assert checks.check_popular_sort_has_a_signal(None) == []


def test_w004_is_quiet_by_default():
    """`popular` is not in the shipped vocabulary, so there is nothing to warn."""
    assert checks.check_popular_sort_has_a_signal(None) == []


def test_w005_truncated_terms():
    from stapel_search.models import SearchDocument
    from stapel_search.services import MAX_TERM_CHARS

    SearchDocument.objects.create(
        doc_type="listing", doc_key="1", facet_terms=["s=" + "z" * MAX_TERM_CHARS]
    )
    findings = checks.check_truncated_terms(None)
    assert _ids(findings) == ["stapel_search.W005"]


def test_w006_no_category_path_provider():
    """Declared by canonical name before a provider exists (spec §19.1)."""
    findings = checks.check_category_path_provider(None)
    assert _ids(findings) == ["stapel_search.W006"]
    assert "category_rollup" in findings[0].hint


def test_w006_is_quiet_once_somebody_provides_it():
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    register_function("categories.path", lambda p: {})
    try:
        assert checks.check_category_path_provider(None) == []
    finally:
        function_registry._providers.pop("categories.path", None)


# --- settings namespace ----------------------------------------------------


def test_every_default_is_closed():
    """An installed-but-unconfigured search indexes nothing and says so."""
    from stapel_search.conf import DEFAULTS

    assert DEFAULTS["SOURCES"] == {}
    assert DEFAULTS["FACET_MAPPINGS"] == {}
    assert DEFAULTS["SCORERS"] == {}
    assert DEFAULTS["DICTIONARIES"] == {}


def test_popular_is_absent_from_the_shipped_sorts():
    from stapel_search.conf import DEFAULTS

    assert "popular" not in DEFAULTS["SORTS"]


def test_the_benchmarked_numbers_are_the_defaults():
    from stapel_search.conf import DEFAULTS

    assert DEFAULTS["FACET_CANDIDATE_CAP"] == 15000
    assert DEFAULTS["MAX_FACET_FIELDS"] == 12
    assert DEFAULTS["MAX_RESULT_WINDOW"] == 1000


def test_transliteration_is_on_for_russian_by_default():
    from stapel_search.conf import DEFAULTS

    assert DEFAULTS["TRANSLITERATE"] == {"ru": True}


def test_only_the_backend_is_an_import_string():
    """One REPLACE seam. The merge registries resolve per entry, where None
    can tombstone a builtin — a whole-dict import_string could not express that."""
    from stapel_search.conf import search_settings

    assert search_settings.import_strings == frozenset({"BACKEND"})


def test_config_md_documents_every_default():
    """A settings key nobody documented is a key nobody can operate."""
    import re
    from pathlib import Path

    from stapel_search.conf import DEFAULTS

    text = (Path(__file__).resolve().parent.parent / "CONFIG.MD").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", text))
    missing = sorted(set(DEFAULTS) - documented)
    assert not missing, f"settings absent from CONFIG.MD: {missing}"
