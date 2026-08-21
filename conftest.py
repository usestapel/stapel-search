def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        from stapel_search._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())

        import django

        django.setup()

        from stapel_core.comm.schemas import autoload_schemas

        autoload_schemas()

        # autoload_schemas only registers emits/ + functions/. Register the
        # consumes/ contracts too, so a test emitting an action we subscribe
        # to is validated against the documented shape rather than delivered
        # unchecked.
        import json
        from pathlib import Path

        from stapel_core.comm.registry import action_registry

        consumes = Path(__file__).resolve().parent / "schemas" / "consumes"
        for schema_file in sorted(consumes.glob("*.json")):
            action_registry.register_schema(
                schema_file.stem,
                json.loads(schema_file.read_text(encoding="utf-8")),
            )


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def staff_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username="ops", email="ops@example.com", is_staff=True
    )


@pytest.fixture
def capture_events():
    """``events = capture_events("search.signal")`` collects deliveries."""
    from stapel_core.comm import action_registry, subscribe_action

    subscribed = []

    def _capture(name):
        events = []
        subscribe_action(name, events.append)
        subscribed.append((name, events.append))
        return events

    yield _capture
    for name, handler in subscribed:
        try:
            action_registry._subscribers.get(name, []).remove(handler)
        except (ValueError, AttributeError):
            pass


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Registries and caches are process-global; tests must not leak into each other."""
    from django.core.cache import cache

    from stapel_search import registry, text
    from stapel_search.backends import reset_backend_cache

    yield
    cache.clear()
    reset_backend_cache()
    text.reset_dictionary_cache()
    registry.reset_defaulted_type_slugs()
    registry._runtime_sources.clear()
    registry._runtime_facet_mappings.clear()
    registry._runtime_scorers.clear()
    registry._runtime_dictionaries.clear()
    registry._facet_cache["version"] = None


@pytest.fixture
def conformance(db):
    """A loaded conformance corpus against the configured backend."""
    from stapel_search.testing import harness

    with harness() as ctx:
        yield ctx
