"""Facet planning and category ancestry — both over comm, no imports.

Two things this module needs from stapel-categories and takes by *name*,
never by import (``stapel-listings/MODULE.md:161``, applied to ourselves):

- ``categories.features`` -> which slugs are facetable, of what type, and
  whether their option set is closed. That is the facet PLAN, and it is
  what makes a zero count possible: a closed option set answers with every
  option, zeros included, because a filter that only ever shows values that
  are already present is a panel that cannot narrow anything.
- ``categories.path`` -> the root->leaf ancestry of a category id, which
  becomes ``category_path``.

**``categories.path`` has no provider in the fleet yet.** stapel-listings
0.4.0 serves ``category_id`` and nothing else, and stapel-categories has a
tree but exposes only ``categories.features`` over comm (spec §19.1 says the
path is ours to build, without naming the call). So the canonical name is
declared here now — the ``stapel-shop/projections.py:23-35`` canon, "name
the Function the owner does not have yet" — and until a provider appears
the path degrades to a single segment: exact-category filtering keeps
working, rollup does not, ``search.W006`` says so at deploy time and
``degraded: ["category_rollup"]`` says so in the answer. Degrading loudly
beats a module that will not start, and beats one that silently answers
"no results" for every parent category.

Caching mirrors ``stapel-listings/services/category_schema.py``
verbatim in mechanism: a revision-versioned data key plus a pointer key,
with ``category.changed`` advancing the pointer. That closes the
read-then-set race a single key has.
"""
from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from .dto import FacetPlan

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "stapel_search:cat:"

#: Set when a category-path lookup had to fall back to a single segment.
#: Read by ``search.W006`` and by the response's ``degraded[]``.
_path_degraded = {"reason": ""}


def _pointer_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}rev:{category_id}"


def _features_key(category_id: Any, revision: Any) -> str:
    return f"{_CACHE_PREFIX}feat:{category_id}:{revision}"


def _path_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}path:{category_id}"


def _coerce_category_id(category_id: Any) -> Any:
    """The categories schema types the id as an integer; opaque ids are not."""
    text = str(category_id)
    return int(text) if text.isdigit() else text


def note_changed(category_id: Any, revision: Any) -> None:
    """React to ``category.changed``: advance the pointer, drop the path."""
    from .conf import search_settings

    ttl = search_settings.CATEGORY_CACHE_TIMEOUT
    if revision is None:
        cache.delete(_pointer_key(category_id))
    else:
        current = cache.get(_pointer_key(category_id))
        if current is None or revision >= current:
            cache.set(_pointer_key(category_id), revision, ttl)
    cache.delete(_path_key(category_id))


def path_degradation() -> str:
    """Why the last category path was incomplete, or ``""``."""
    return _path_degraded["reason"]


def reset_path_degradation() -> None:
    _path_degraded["reason"] = ""


def category_path(category_id: Any) -> tuple[str, ...]:
    """Root->leaf ancestry for *category_id*, as strings.

    Falls back to ``(category_id,)`` when no provider answers — see the
    module docstring for why that is a loud degradation and not a failure.
    """
    if category_id in (None, ""):
        return ()
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    cached = cache.get(_path_key(category_id))
    if cached is not None:
        return tuple(cached)

    name = search_settings.CATEGORY_PATH_FUNCTION
    try:
        result = call(name, {"category_ids": [_coerce_category_id(category_id)]})
    except (CommError, LookupError, KeyError, TypeError) as exc:
        _path_degraded["reason"] = f"{name} unavailable: {exc.__class__.__name__}"
        return (str(category_id),)

    raw = None
    if isinstance(result, dict):
        raw = result.get(str(category_id)) or result.get(_coerce_category_id(category_id))
        if raw is None and isinstance(result.get("paths"), dict):
            paths = result["paths"]
            raw = paths.get(str(category_id)) or paths.get(_coerce_category_id(category_id))
    if not raw:
        _path_degraded["reason"] = f"{name} returned no path for {category_id!r}"
        return (str(category_id),)

    path = tuple(str(segment) for segment in raw)
    cache.set(_path_key(category_id), list(path), search_settings.CATEGORY_CACHE_TIMEOUT)
    return path


def _feature_defs(category_id: Any) -> tuple[list[dict], Any]:
    """``categories.features`` for *category_id*, revision-cached."""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    revision = cache.get(_pointer_key(category_id))
    if revision is not None:
        cached = cache.get(_features_key(category_id, revision))
        if cached is not None:
            return cached, revision

    try:
        result = call(
            search_settings.CATEGORY_FEATURES_FUNCTION,
            {"category_id": _coerce_category_id(category_id)},
        )
    except (CommError, LookupError, KeyError, TypeError) as exc:
        logger.debug("categories.features unavailable for %r: %s", category_id, exc)
        return [], None

    features = result.get("features", []) if isinstance(result, dict) else []
    revision = result.get("revision") if isinstance(result, dict) else None
    if revision is not None:
        ttl = search_settings.CATEGORY_CACHE_TIMEOUT
        cache.set(_features_key(category_id, revision), features, ttl)
        current = cache.get(_pointer_key(category_id))
        if current is None or revision >= current:
            cache.set(_pointer_key(category_id), revision, ttl)
    return features, revision


def facet_plan(
    category_id: Any = None, *, requested: tuple[str, ...] | None = None
) -> FacetPlan:
    """What to count for this query.

    ``requested`` is the caller's ``facets=`` list; ``None`` means "the
    category's plan". Slugs past ``MAX_FACET_FIELDS`` land in ``skipped``
    rather than vanishing — N active facet filters already cost N+1
    candidate sets, and the cap is what stops a wide category page from
    turning into a dozen sequential scans.
    """
    from .conf import search_settings
    from .registry import get_facet_mapping

    max_fields = int(search_settings.MAX_FACET_FIELDS)
    features, revision = _feature_defs(category_id) if category_id else ([], None)

    kinds: dict[str, str] = {}
    closed: dict[str, tuple[str, ...]] = {}
    ordered: list[str] = []
    #: Slugs the category declares with a `skip` kind (`header`, `group`).
    #: Kept so an explicit `facets=` list cannot re-admit them below — a slug
    #: the writer never indexes would otherwise plan as a term facet and answer
    #: every query with an empty panel.
    excluded: set[str] = set()

    for feature in features:
        slug = feature.get("slug")
        config = feature.get("config") or {}
        type_slug = config.get("type") or ""
        if not slug or not type_slug:
            continue
        mapping = get_facet_mapping(type_slug)
        if mapping.kind == "skip":
            excluded.add(slug)
            continue
        kinds[slug] = mapping.kind
        ordered.append(slug)
        if config.get("optionsRef"):
            # A vocabulary-backed field (ref_select, and any host type that
            # points at a vocabulary the same way) has no closed option set to
            # zero-fill: the level lives outside the schema and can hold
            # thousands of terms. Counting what is present is the whole panel.
            continue
        options = config.get("options")
        # Labels are translation keys living in the category config, so we
        # return codes and counts and let the frontend resolve captions from
        # the schema it already fetched (spec §1.3).
        if options and not config.get("allowCustom"):
            values = tuple(
                str(option.get("value"))
                for option in options
                if isinstance(option, dict) and option.get("value") is not None
            )
            if values:
                closed[slug] = values

    if requested is not None:
        wanted = [slug for slug in requested if slug and slug not in excluded]
        ordered = [slug for slug in wanted if slug in kinds] + [
            slug for slug in wanted if slug not in kinds
        ]
        for slug in ordered:
            kinds.setdefault(slug, "term")

    selected = tuple(ordered[:max_fields])
    skipped = tuple(ordered[max_fields:])
    return FacetPlan(
        slugs=selected,
        kinds={slug: kinds.get(slug, "term") for slug in selected},
        closed_options={slug: closed[slug] for slug in selected if slug in closed},
        skipped=skipped,
        revision=revision,
    )


def fill_zero_options(counts: dict[str, dict[str, int]], plan: FacetPlan) -> dict:
    """Add the zeros a closed option set owes the panel."""
    filled = {slug: dict(values) for slug, values in counts.items()}
    for slug, options in plan.closed_options.items():
        bucket = filled.setdefault(slug, {})
        for option in options:
            bucket.setdefault(option, 0)
    return filled


__all__ = [
    "category_path",
    "facet_plan",
    "fill_zero_options",
    "note_changed",
    "path_degradation",
    "reset_path_degradation",
]
