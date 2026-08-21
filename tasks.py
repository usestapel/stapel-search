"""Scheduled work — the catch-up and expiry that make the index self-healing.

Celery is OPTIONAL: every function here is a plain callable a cron job, a
systemd timer or a management command can invoke. When celery is installed
they are additionally registered as shared tasks under stable names.

Wire it into a host's beat schedule::

    from stapel_search.tasks import get_search_beat_schedule

    CELERY_BEAT_SCHEDULE = {**get_search_beat_schedule(), ...}

``checks.py`` raises ``search.W003`` when a host has a beat schedule with no
entry pointing here: a catch-up job nobody schedules is a promise, not a
mechanism (the DOCS-02 lesson, applied to ourselves).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

REINDEX_STALE_TASK = "stapel_search.tasks.search_reindex_stale"
EXPIRE_SIGNALS_TASK = "stapel_search.tasks.search_expire_signals"
PURGE_TOMBSTONES_TASK = "stapel_search.tasks.search_purge_tombstones"


def search_reindex_stale(limit: int = 500) -> dict:
    """Re-pull documents the source has moved on from, type by type.

    The safety net for a LOST EVENT, not a crutch against the source. Since
    stapel-listings 0.4.0 rebuilds ``features_search`` on every save that
    touches features and emits ``listing.updated`` from the real edit paths,
    a healthy deployment should see this find nothing — and a job that
    always finds something is a signal worth reading, not noise to mute.
    """
    from .registry import get_sources
    from .services import reindex_stale

    results: dict[str, int] = {}
    for doc_type in get_sources():
        report = reindex_stale(doc_type, limit=limit)
        results[doc_type] = report.indexed
    logger.info("search reindex_stale: %s", results)
    return results


def search_expire_signals() -> int:
    """Drop promotions past their expiry — the paid slot really ends."""
    from .services import expire_signals

    count = expire_signals()
    if count:
        logger.info("search: %s promotion(s) expired", count)
    return count


def search_purge_tombstones() -> int:
    """Delete rows that have been invisible longer than the retention window."""
    from .services import purge_tombstones

    count = purge_tombstones()
    if count:
        logger.info("search: %s tombstone(s) purged", count)
    return count


def get_search_beat_schedule() -> dict:
    """Beat entries for the three jobs, on the configured cadences.

    This is the one function here that genuinely needs celery — it returns
    ``crontab`` objects. Everything else in this module is a plain callable
    a cron job or a systemd timer can invoke, which is what "celery is
    optional" means. Asked for it without celery installed, say so by name
    rather than surfacing a bare ImportError from three frames down.
    """
    try:
        from celery.schedules import crontab
    except ImportError as exc:  # pragma: no cover - celery is present in most envs
        raise ImportError(
            "get_search_beat_schedule() builds celery crontab entries and needs "
            "celery installed. Without celery, schedule the plain callables "
            "instead: stapel_search.tasks.search_reindex_stale, "
            "search_expire_signals and search_purge_tombstones are all "
            "invocable from cron or a systemd timer."
        ) from exc

    from .conf import search_settings

    stale = dict(search_settings.STALE_REINDEX_SCHEDULE or {"minute": "*/10"})
    nightly = dict(search_settings.REINDEX_SCHEDULE or {"hour": 3, "minute": 20})
    return {
        "stapel-search-reindex-stale": {
            "task": REINDEX_STALE_TASK,
            "schedule": crontab(**stale),
        },
        "stapel-search-expire-signals": {
            "task": EXPIRE_SIGNALS_TASK,
            "schedule": crontab(minute="*/5"),
        },
        "stapel-search-purge-tombstones": {
            "task": PURGE_TOMBSTONES_TASK,
            "schedule": crontab(**nightly),
        },
    }


try:  # pragma: no cover - exercised only where celery is installed
    from celery import shared_task
except ImportError:  # pragma: no cover
    pass
else:  # pragma: no cover
    search_reindex_stale = shared_task(name=REINDEX_STALE_TASK)(search_reindex_stale)
    search_expire_signals = shared_task(name=EXPIRE_SIGNALS_TASK)(search_expire_signals)
    search_purge_tombstones = shared_task(name=PURGE_TOMBSTONES_TASK)(search_purge_tombstones)


__all__ = [
    "EXPIRE_SIGNALS_TASK",
    "PURGE_TOMBSTONES_TASK",
    "REINDEX_STALE_TASK",
    "get_search_beat_schedule",
    "search_expire_signals",
    "search_purge_tombstones",
    "search_reindex_stale",
]
