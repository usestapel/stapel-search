"""The conformance suite, run against every backend this package ships.

Naive runs everywhere. Postgres runs when ``STAPEL_SEARCH_TEST_DB`` points
at a real server (its FTS configs, GIN plans and TABLESAMPLE cannot be
faked, so they are tested for real or not at all). Meilisearch runs when
``STAPEL_SEARCH_MEILI_URL`` points at a reachable instance — and the
release gate is that the Meilisearch job EXISTS in CI, not that it passed
by being skipped: a second backend nobody exercises rots.
"""
from __future__ import annotations

import pytest

from stapel_search.testing import SCENARIOS, ConformanceSkip, harness

from .marks import requires_meili, requires_postgres

pytestmark = pytest.mark.django_db


def _run(scenario, backend_path: str, settings_extra: dict | None = None):
    from django.test import override_settings

    from stapel_search.backends import reset_backend_cache

    config = {"BACKEND": backend_path}
    config.update(settings_extra or {})
    with override_settings(STAPEL_SEARCH=config):
        reset_backend_cache()
        try:
            with harness() as ctx:
                scenario.run(ctx)
        except ConformanceSkip as skip:
            pytest.skip(f"backend does not claim {skip}")
        finally:
            reset_backend_cache()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_naive_backend(scenario):
    _run(scenario, "stapel_search.backends.naive.NaiveSearchBackend")


@requires_postgres
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_postgres_backend(scenario):
    _run(scenario, "stapel_search.backends.postgres.PostgresSearchBackend")


@requires_meili
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_meili_backend(scenario):
    import os

    _run(
        scenario,
        "stapel_search.backends.meili.MeilisearchBackend",
        {
            "MEILI_URL": os.environ["STAPEL_SEARCH_MEILI_URL"],
            "MEILI_KEY": os.environ.get("STAPEL_SEARCH_MEILI_KEY", ""),
        },
    )


def test_every_shipped_backend_declares_its_read_paths():
    """A shipped backend must claim every query read path, or be a stub.

    The static half of the same rule ``stapel_tools.index_lint`` enforces
    fleet-wide, kept here too so the package is self-checking without the
    linter installed.
    """
    from stapel_search.backends import meili, naive, postgres
    from stapel_search.index_schema import all_query_read_paths

    for module in (naive, postgres, meili):
        declared = set(module.READ_PATH_IMPL)
        missing = sorted(set(all_query_read_paths()) - declared)
        assert not missing, f"{module.__name__} does not answer {missing}"


def test_stub_backend_is_a_pointer_not_a_promise():
    from stapel_search.backends.opensearch import OpenSearchBackend

    backend = OpenSearchBackend()
    for verb in ("capabilities", "health"):
        with pytest.raises(NotImplementedError) as excinfo:
            getattr(backend, verb)()
        assert "STAPEL_SEARCH['BACKEND']" in str(excinfo.value)
