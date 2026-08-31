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


def _ref_field(options_ref: Any, name: str) -> str:
    """One field of an ``optionsRef``, dataclass or dict alike.

    ``stapel_attributes.types.refs.ref_field`` in a form this module can call
    on a raw config dict off the wire: `categories.features` serves JSON, so
    the dataclass arm is defensive rather than the normal path.
    """
    if options_ref is None:
        return ""
    value = (
        options_ref.get(name)
        if isinstance(options_ref, dict)
        else getattr(options_ref, name, None)
    )
    return str(value) if value else ""


def vocabulary_labels(
    plan: FacetPlan, counts: dict[str, dict[str, int]]
) -> dict[str, dict[str, str]]:
    """Captions for the vocabulary-backed slugs, for the codes actually counted.

    The asymmetry this closes was the tell in the live report: an inline
    ``select`` printed ``b-u`` while a ``ref_select`` printed "Apple" on a
    listing CARD — because a ref DAO carries a label snapshot taken at write
    time and an inline select did not. In the FACET PANEL it was the other way
    round: 0.4.0 gave the inline selects captions and left the ref slugs as
    bare codes, so one panel showed «Состояние: Б/у» directly above
    «Производитель: apple». Neither half was wrong about its own type; the
    panel was wrong as a whole.

    Resolution happens HERE, after counting, and not in ``facet_plan``, for a
    reason that is about size rather than tidiness: a level of the phone
    catalogue holds 15 844 terms and the plan does not know which of them a
    query will produce. What a query produces is at most ``MAX_FACET_VALUES``
    codes per slug, and they are asked for in ONE batched call per slug.

    The resolver is ``stapel-attributes``' — the same abstraction the ref
    types validate and snapshot through, so a deployment wires a vocabulary
    once. With no resolver registered this returns ``{}`` and the panel is
    exactly what it was: codes. A caption is an improvement on a code, never a
    precondition for answering.
    """
    if not plan.vocabulary_refs:
        return {}
    try:
        from stapel_attributes.vocabularies import get_vocabulary_resolver
    except ImportError:  # pragma: no cover - stapel-attributes is a hard dep
        return {}

    resolver = get_vocabulary_resolver()
    if resolver is None:
        return {}

    out: dict[str, dict[str, str]] = {}
    for slug, (vocabulary, level) in plan.vocabulary_refs.items():
        codes = [code for code in (counts.get(slug) or {}) if code]
        if not codes:
            continue
        try:
            # `VocabularyResolver.labels`, not `refs.resolve_labels`. The
            # latter labels an unresolved code as ITSELF, which is right for a
            # stored DAO — something must be shown — and wrong here: a caption
            # map is an OVERLAY, and `{"apple": "apple"}` would make a map that
            # resolved nothing indistinguishable from one that resolved every
            # term to its own name. `labels()` omits what it does not know,
            # which is the distinction this needs, and a reader falls back to
            # the code for a missing key anyway. (`realme`'s catalogue label
            # really is `realme`; the two are not the same fact.)
            mapping = resolver.labels(vocabulary, level, list(codes)) or {}
        except Exception as exc:  # noqa: BLE001 — a caption is never fatal
            logger.warning(
                "vocabulary labels unavailable for facet %r (%s/%s): %s",
                slug,
                vocabulary,
                level,
                exc,
            )
            continue
        captions = {
            code: str(mapping[code])
            for code in codes
            if mapping.get(code) not in (None, "")
        }
        if captions:
            out[slug] = captions
    return out


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


def _facet_rank(feature: dict) -> int:
    """How much of the facet budget this feature has earned, lower first.

    The budget is ``MAX_FACET_FIELDS`` and a wide imported category spends
    it in *authoring* order, which is an accident of the feed the category
    came from. Live, on a phones board, that meant the panel counted parcel
    weight, length, height and width — the delivery block happens to be
    authored first — and reported Colour and RAM as *skipped*, which are
    the two a phone buyer actually narrows by.

    So rank by what the category itself already says about each feature,
    rather than by a list of slugs kept in this module (which would be a
    search library holding opinions about phones):

    - ``show_at_title`` — the author put it in the listing's own title;
    - ``show_as_badge`` — the author put it on the card;
    - ``mandatory`` — every listing in the category has it, so its buckets
      partition the corpus instead of describing a fraction of it.

    Ties keep the authored order, so the ranking never reshuffles a panel
    whose features are all flagged the same.
    """
    if feature.get("show_at_title"):
        return 0
    if feature.get("show_as_badge"):
        return 1
    if feature.get("mandatory"):
        return 2
    return 3


def _is_facetable(feature: dict, config: dict) -> bool:
    """Whether the category offers this feature as a filter axis.

    ``facet: false`` is the opt-out, read from the FeatureDef (or from its
    config) and **defaulting to true** so a category that says nothing keeps
    today's behaviour. This is the ``categories.path`` canon applied again:
    name the field the owner does not serve yet, consume it the moment they
    do, and degrade to something sane meanwhile. It is what a category
    author needs to say "the parcel's width is a shipping input, not a
    filter" — a thing no library can infer from the type, because the very
    same ``int`` is a filter axis one category over.
    """
    for holder in (feature, config):
        flag = holder.get("facet")
        if flag is not None:
            return bool(flag)
    return True


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
    from .index_schema import CORE_RANGE_FIELDS
    from .registry import get_facet_mapping

    max_fields = int(search_settings.MAX_FACET_FIELDS)
    features, revision = _feature_defs(category_id) if category_id else ([], None)

    kinds: dict[str, str] = {}
    closed: dict[str, tuple[str, ...]] = {}
    labels: dict[str, dict[str, str]] = {}
    translatable: dict[str, bool] = {}
    vocabulary_refs: dict[str, tuple[str, str]] = {}
    ranked: list[tuple[int, int, str]] = []
    ordered: list[str] = []
    #: Slugs the category declares with a `skip` kind (`header`, `group`).
    #: Kept so an explicit `facets=` list cannot re-admit them below — a slug
    #: the writer never indexes would otherwise plan as a term facet and answer
    #: every query with an empty panel.
    excluded: set[str] = set()

    for position, feature in enumerate(features):
        slug = feature.get("slug")
        config = feature.get("config") or {}
        type_slug = config.get("type") or ""
        if not slug or not type_slug:
            continue
        mapping = get_facet_mapping(type_slug)
        if mapping.kind == "skip" or not _is_facetable(feature, config):
            excluded.add(slug)
            continue
        kinds[slug] = mapping.kind
        ranked.append((_facet_rank(feature), position, slug))
        if config.get("optionsRef"):
            # A vocabulary-backed field (ref_select, and any host type that
            # points at a vocabulary the same way) has no closed option set to
            # zero-fill: the level lives outside the schema and can hold
            # thousands of terms. Counting what is present is the whole panel.
            #
            # Its CAPTIONS are a different question, and 0.4.0 answered it
            # wrongly by not answering it: `facet_labels` simply omitted these
            # slugs, so a panel built from the answer printed «Производитель:
            # apple 13, xiaomi 10» and «Модель: redmi-note-12 3» — the same
            # defect as `b-u`, one type over, and the two halves of one panel
            # disagreeing about whether a facet is readable. The address is
            # recorded here and the codes are resolved after the count, where
            # the set is small and known. See `vocabulary_labels`.
            vocabulary = _ref_field(config["optionsRef"], "vocabulary")
            level = _ref_field(config["optionsRef"], "level")
            if vocabulary and level:
                vocabulary_refs[slug] = (vocabulary, level)
            continue
        options = config.get("options")
        if not options:
            continue
        # The caption ships WITH the count. Until 0.4.0 it did not, on the
        # reasoning that the frontend has the schema already (spec §1.3) —
        # and that is true of the compose form, which fetches the category
        # to draw itself, but not of a SERP: a host that renders a panel
        # from the search answer alone has no schema, and the panel then
        # prints storage slugs — «Состояние: b-u», «Вид объявления:
        # prodayu-svoe» — at buyers. The labels cost nothing here: the very
        # next lines already walk these same option dicts.
        #
        # `translatable_options` rides along because the reader cannot tell
        # a key from a caption by looking: `b.apple` and `Б/у` are both
        # strings, and guessing wrong prints either a dotted key or an
        # untranslated word.
        captions = {
            str(option["value"]): str(option.get("label") or option["value"])
            for option in options
            if isinstance(option, dict) and option.get("value") is not None
        }
        if captions:
            labels[slug] = captions
            translatable[slug] = bool(config.get("translatable_options", True))
        if not config.get("allowCustom"):
            values = tuple(captions)
            if values:
                closed[slug] = values

    ranked.sort(key=lambda row: (row[0], row[1]))
    ordered = [slug for _, _, slug in ranked]

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
        option_labels={slug: labels[slug] for slug in selected if slug in labels},
        translatable_labels={
            slug: translatable[slug] for slug in selected if slug in translatable
        },
        vocabulary_refs={
            slug: vocabulary_refs[slug] for slug in selected if slug in vocabulary_refs
        },
        # Not conditioned on the category: a core range addresses a column
        # every document in every corpus has. Announcing it here is what
        # lets a panel offer «Цена от … до …» without the frontend keeping
        # its own list of which slugs are core.
        core_ranges=tuple(CORE_RANGE_FIELDS),
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
