"""Single-module Django settings for stapel-search.

One ``settings.configure(...)`` block serves three callers, which is the
point: the test suite, the contract-emission harness and the capabilities
emitter cannot drift apart if there is nothing to drift.

The default backend here is the **naive** one, because the test database is
SQLite and the Postgres backend refuses to run off Postgres rather than
degrading into ``icontains``. The Postgres and Meilisearch suites opt in
through ``STAPEL_SEARCH_TEST_DB`` / ``STAPEL_SEARCH_MEILI_URL``.
"""
from __future__ import annotations

import os


def database() -> dict:
    """SQLite by default; a real Postgres when ``STAPEL_SEARCH_TEST_DB`` is set.

    Env-gated exactly like ``STAPEL_RECORDINGS_TEST_DB``: the Postgres-only
    behaviour (FTS configs, GIN, TABLESAMPLE) cannot be faked, so it is
    tested against a real server or skipped — never simulated.
    """
    url = os.environ.get("STAPEL_SEARCH_TEST_DB", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}

    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (parsed.path or "/postgres").lstrip("/"),
        "USER": unquote(parsed.username or "postgres"),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": str(parsed.port or 5432),
    }


def default_backend() -> str:
    if os.environ.get("STAPEL_SEARCH_TEST_DB"):
        return "stapel_search.backends.postgres.PostgresSearchBackend"
    return "stapel_search.backends.naive.NaiveSearchBackend"


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_search.tests.urls",
    contract: bool = False,
) -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module search instance."""
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under). Inlined, not imported, to
        # dodge the import-time settings read.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            # stapel_attributes and stapel_geo are L1 libraries (no Django
            # app): imported for the attribute-type registry and for pure
            # geohash arithmetic, never installed.
            "stapel_search",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={"default": database()},
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        STAPEL_SEARCH={"BACKEND": default_backend()},
        # NOTE migrations are NOT disabled for the users app. Skipping them
        # works on SQLite by accident of table-creation order and fails on
        # Postgres ("relation auth_group does not exist"), and the Postgres
        # suite is the one that has to be real.
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


#: The multi-module common path prefix drf-spectacular auto-detects inside an
#: all-modules aggregate. Forced on the singleton by the harness so a
#: single-module instance derives the same operationIds.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
