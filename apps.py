from django.apps import AppConfig


class SearchConfig(AppConfig):
    name = "stapel_search"
    label = "search"
    verbose_name = "Search: index, facets, geo radius and ranking"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: system checks, error-key registration,
        # comm providers. Keep each in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # Action subscriptions: the fixed ones (search.signal, category.changed,
        # user.deleted) come from importing the module; the per-source
        # invalidation signals are wired from the registry, so a host never
        # writes a subscriber (the stapel-docs INGEST form).
        from . import actions

        actions.wire_sources()
