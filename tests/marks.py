"""Environment gates for the suites that need real infrastructure.

Same shape as ``STAPEL_RECORDINGS_TEST_DB``: behaviour that cannot be
faked — Postgres FTS configs and GIN plans, a Meilisearch analyzer — is
tested against a real server or skipped, never simulated. A simulated
engine that passes is worse than a skipped one, because it reports
confidence nobody earned.
"""
import os

import pytest

POSTGRES_URL = os.environ.get("STAPEL_SEARCH_TEST_DB", "")
MEILI_URL = os.environ.get("STAPEL_SEARCH_MEILI_URL", "")

requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set STAPEL_SEARCH_TEST_DB to a postgres URL (see MODULE.md 'Testing')",
)


def _meili_reachable() -> bool:
    """Reachable, or a LOUD failure when somebody asked for it.

    A skip-marked suite that skips is indistinguishable from a suite that
    passes, and that is exactly how a second backend rots — the spec makes
    the Meilisearch job a release gate by EXISTING, not by being green
    through absence. So: no URL configured is an honest skip, but a URL
    configured and unreachable is a build failure. Nobody sets that variable
    by accident.
    """
    if not MEILI_URL:
        return False
    try:
        import meilisearch
    except ImportError as exc:  # pragma: no cover - the extra is installed in CI
        raise RuntimeError(
            "STAPEL_SEARCH_MEILI_URL is set but the client is missing: "
            "pip install 'stapel-search[meili]'"
        ) from exc
    try:
        meilisearch.Client(
            MEILI_URL, os.environ.get("STAPEL_SEARCH_MEILI_KEY") or None, timeout=5
        ).health()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"STAPEL_SEARCH_MEILI_URL={MEILI_URL} is set but unreachable ({exc}). "
            "Refusing to skip: a second backend nobody exercises rots."
        ) from exc
    return True


requires_meili = pytest.mark.skipif(
    not _meili_reachable(),
    reason="STAPEL_SEARCH_MEILI_URL is unset (see MODULE.md 'Testing')",
)
