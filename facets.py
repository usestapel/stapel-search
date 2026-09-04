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

#: Shares of the candidate set that separate two slugs in
#: :func:`evidence_plan`: "most of this page carries it", "a tenth of it
#: does", "less". Not a setting and not a finer scale — see the docstring
#: there for the measurement that says the prediction cannot resolve more.
EVIDENCE_BANDS = (0.5, 0.1)

#: Set when a category-path lookup had to fall back to a single segment.
#: Read by ``search.W006`` and by the response's ``degraded[]``.
_path_degraded = {"reason": ""}


def _pointer_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}rev:{category_id}"


def _features_key(category_id: Any, revision: Any) -> str:
    return f"{_CACHE_PREFIX}feat:{category_id}:{revision}"


def _path_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}path:{category_id}"


def _slug_path_key(slug: Any) -> str:
    return f"{_CACHE_PREFIX}slugpath:{slug}"


def _slug_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}slug:{category_id}"


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
    cache.delete(_slug_key(category_id))


def path_degradation() -> str:
    """Why the last category path was incomplete, or ``""``."""
    return _path_degraded["reason"]


def reset_path_degradation() -> None:
    _path_degraded["reason"] = ""


def note_path_degradation(reason: str) -> None:
    """Record why a path lookup was incomplete, for a caller that did its own.

    :func:`lookup_path` reports rather than degrades, so the caller that
    chose to carry on with an unresolved segment is the one that owes the
    answer its ``degraded[]`` entry.
    """
    _path_degraded["reason"] = reason


def lookup_path(category_id: Any) -> tuple[tuple[str, ...], str, str]:
    """``(path, outcome, detail)`` for *category_id* — the raw provider lookup.

    ``outcome`` is one of:

    - ``"ok"`` — the provider answered with an ancestry;
    - ``"unknown"`` — the provider answered and knows no such node
      (``categories.path`` simply omits an id with no row);
    - ``"unavailable"`` — nobody answered at all.

    The last two are a different fact and only ONE of them is the caller's
    fault. :func:`category_path` collapses both into the loud single-segment
    fallback, because an INDEXER cannot refuse a document over an unreachable
    provider; a READ that was handed a bare category id can, and does
    (``services._resolve_category``): a 400 that names the id beats a
    silent ``count: 0`` over a catalogue that has the node.
    """
    if category_id in (None, ""):
        return (), "ok", ""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    cached = cache.get(_path_key(category_id))
    if cached is not None:
        return tuple(cached), "ok", ""

    name = search_settings.CATEGORY_PATH_FUNCTION
    try:
        result = call(name, {"category_ids": [_coerce_category_id(category_id)]})
    except (CommError, LookupError, KeyError, TypeError) as exc:
        return (), "unavailable", f"{name} unavailable: {exc.__class__.__name__}"

    raw = _answered_path(result, category_id)
    if not raw:
        return (), "unknown", f"{name} returned no path for {category_id!r}"

    path = tuple(str(segment) for segment in raw)
    cache.set(_path_key(category_id), list(path), search_settings.CATEGORY_CACHE_TIMEOUT)
    return path, "ok", ""


def _answered_path(result: Any, key: Any) -> Any:
    """The one ancestry a batch answer holds for *key* — flat, or under ``paths``."""
    if not isinstance(result, dict):
        return None
    raw = result.get(str(key)) or result.get(_coerce_category_id(key))
    if raw is None and isinstance(result.get("paths"), dict):
        paths = result["paths"]
        raw = paths.get(str(key)) or paths.get(_coerce_category_id(key))
    return raw


def lookup_slug(slug: Any) -> tuple[tuple[str, ...], str, str]:
    """``(id path, outcome, detail)`` for a category SLUG.

    :func:`lookup_path` over the other key stapel-categories guarantees is
    unique — ``Category.slug`` (``unique=True``, the key behind ``GET
    /categories/api/v1/categories/by-slug/{slug}/``). Uniqueness is GLOBAL,
    so one leaf slug names one node and its whole ancestry comes back with
    it: ``avtomobili`` alone is as complete an address as ``141/151``.

    The three outcomes are :func:`lookup_path`'s, for the same reason — a
    caller that was handed a slug can refuse an unknown one and may not
    refuse an outage. ``CATEGORY_SLUG_FUNCTION`` has no provider in the
    fleet yet (the ``stapel-shop/projections.py:23-35`` canon: name the
    Function the owner does not have). Absent, every slug segment is
    ``unavailable`` — the segment stands, ``degraded:
    ["category_rollup"]`` says so, and nothing 400s.

    The answer is ``{"<slug>": ["<root id>", ..., "<id>"]}``, the
    ``categories.path`` shape keyed by slug; an absent key is "no such
    slug". Cached for ``CATEGORY_CACHE_TIMEOUT`` and only that long:
    ``category.changed`` carries an id and no slug, so a RENAME is invisible
    to :func:`note_changed` and expires instead of being dropped.
    """
    if slug in (None, ""):
        return (), "ok", ""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    cached = cache.get(_slug_path_key(slug))
    if cached is not None:
        return tuple(cached), "ok", ""

    name = search_settings.CATEGORY_SLUG_FUNCTION
    try:
        result = call(name, {"slugs": [str(slug)]})
    except (CommError, LookupError, KeyError, TypeError) as exc:
        return (), "unavailable", f"{name} unavailable: {exc.__class__.__name__}"

    raw = _answered_path(result, slug)
    if not raw:
        return (), "unknown", f"{name} returned no path for {slug!r}"

    path = tuple(str(segment) for segment in raw)
    ttl = search_settings.CATEGORY_CACHE_TIMEOUT
    cache.set(_slug_path_key(slug), list(path), ttl)
    # The reverse direction of the same fact, free: an echo that has to name
    # this node's slug does not ask again.
    cache.set(_slug_key(path[-1]), str(slug), ttl)
    return path, "ok", ""


def slugs_for_ids(ids) -> tuple[dict[str, str], bool]:
    """``({id: slug}, unavailable)`` — the reverse of :func:`lookup_slug`.

    What lets an answer echo the address in the OTHER form: a request that
    arrived as ids is owed the slug path a client can rewrite to. One
    batched ``categories.names`` call for the ids not already cached, and
    fail-soft — an id nobody names simply has no slug, and the caller says
    ``category_names`` rather than inventing one out of the id.
    """
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from .conf import search_settings

    wanted = [str(i) for i in ids if i not in (None, "")]
    found = {i: cache.get(_slug_key(i)) for i in wanted}
    missing = sorted(i for i, slug in found.items() if not slug)
    resolved = {i: slug for i, slug in found.items() if slug}
    if not missing:
        return resolved, False

    name = search_settings.CATEGORY_NAMES_FUNCTION
    try:
        answer = call(name, {"ids": missing})
    except (CommError, LookupError, KeyError, TypeError) as exc:
        logger.warning("%s unavailable: %s", name, exc)
        return resolved, True

    rows = (answer or {}).get("names") if isinstance(answer, dict) else None
    if not isinstance(rows, dict):
        logger.warning("%s answered a non-mapping; no slugs for %s", name, missing)
        return resolved, True

    ttl = search_settings.CATEGORY_CACHE_TIMEOUT
    for category_id, row in rows.items():
        slug = (row or {}).get("slug") if isinstance(row, dict) else None
        if slug:
            resolved[str(category_id)] = str(slug)
            cache.set(_slug_key(category_id), str(slug), ttl)
    return resolved, False


def category_path(category_id: Any) -> tuple[str, ...]:
    """Root->leaf ancestry for *category_id*, as strings.

    Falls back to ``(category_id,)`` when no provider answers — see the
    module docstring for why that is a loud degradation and not a failure.
    """
    if category_id in (None, ""):
        return ()
    path, outcome, detail = lookup_path(category_id)
    if outcome == "ok":
        return path
    _path_degraded["reason"] = detail
    return (str(category_id),)


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


def _is_choice(config: dict, mapping: Any) -> bool:
    """Does this feature offer a BOUNDED set of values to choose from?

    Two things have to hold, and the second is the one a type alone cannot
    tell you: the axis has to be discrete (``term``/``path`` — a number is
    narrowed with two bounds, not with a checkbox per value), and it has to
    have an option set — inline ``options``, or an ``optionsRef`` pointing at
    a vocabulary level. A ``string`` is a term axis with neither, and it
    enumerates as many values as there are documents: on the live cars leaf
    those are ``grn`` (a plate number) and four discount blurbs.
    """
    if getattr(mapping, "kind", "") not in ("term", "path"):
        return False
    return bool(config.get("options") or config.get("optionsRef"))


def _facet_rank(feature: dict, config: dict, mapping: Any) -> tuple[int, int]:
    """How much of the facet budget this feature has earned, lower first.

    The budget is ``MAX_FACET_FIELDS`` and a wide imported category spends
    it in *authoring* order, which is an accident of the feed the category
    came from. Live, on a phones board, that meant the panel counted parcel
    weight, length, height and width — the delivery block happens to be
    authored first — and reported Colour and RAM as *skipped*, which are
    the two a phone buyer actually narrows by.

    So rank by what the category itself already says about each feature,
    rather than by a list of slugs kept in this module (which would be a
    search library holding opinions about phones). Two keys, and the first
    one is 0.8.0's fix.

    **The BAND: a choice outranks a measurement, always.** 0.7.0 ranked on
    the author's flags alone, and on the imported cars leaf — 59 features —
    that spent the last budget slot on ``vin``, a mandatory ``int``, while
    the vocabulary chain «Поколение → Модификация → Комплектация» and
    «Мощность» fell past the cap. A buyer was offered the body number and
    nine dealer promotions to filter a car by, and not the make. The band is
    not a slug list: it asks whether the feature has a bounded option set at
    all (:func:`_is_choice`), which is exactly the difference between an
    axis a panel can draw as a list of choices and one it cannot. Numbers
    lose nothing by it — a range axis is drawn from the category schema and
    from ``core_ranges``, neither of which is capped by this budget — so a
    numeric slug in the plan buys a bucket-per-distinct-number and costs an
    axis somebody could actually click.

    **Then the author's own flags**, unchanged except for one insertion:

    - ``show_at_title`` — the author put it in the listing's own title;
    - an ``optionsRef`` — the value comes from a VOCABULARY, which is what a
      catalogue's identity chain is made of (make, model, generation) and
      what a hand-written ``select`` of five options is not;
    - ``show_as_badge`` — the author put it on the card;
    - ``mandatory`` — every listing in the category has it, so its buckets
      partition the corpus instead of describing a fraction of it.

    Ties keep the authored order, so the ranking never reshuffles a panel
    whose features are all flagged the same.
    """
    band = 0 if _is_choice(config, mapping) else 1
    if feature.get("show_at_title"):
        return (band, 0)
    if band == 0 and config.get("optionsRef"):
        return (band, 1)
    if feature.get("show_as_badge"):
        return (band, 2)
    if feature.get("mandatory"):
        return (band, 3)
    return (band, 4)


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


def _is_public(feature: dict, config: dict) -> bool:
    """Whether the category lets ANY reader see this feature's values.

    ``FeatureDef.visibility`` (stapel-attributes 0.8): ``public`` — the
    default, and what every definition written before the axis existed reads
    as — against ``owner``/``staff``, which mark a value that identifies a
    specific physical unit (VIN, IMEI, serial, registry number).

    Read with ``.get`` off the feature and then off its config, exactly like
    :func:`_is_facetable`, because ``categories.features`` does not serve the
    field yet: this consumes it the moment stapel-categories ships it and
    reads ``public`` meanwhile.

    Fail-closed on a value this library does not know:
    ``normalize_visibility`` raises on a typo like ``"private"``, and the
    answer to "I cannot tell what this means" is not to publish a VIN.
    """
    from stapel_attributes.visibility import PUBLIC, UnknownVisibility, normalize_visibility

    for holder in (feature, config):
        raw = holder.get("visibility")
        if raw in (None, ""):
            continue
        try:
            return normalize_visibility(raw) == PUBLIC
        except UnknownVisibility:
            logger.warning(
                "unknown visibility %r on feature %r — treated as hidden",
                raw,
                feature.get("slug"),
            )
            return False
    return True


class _Fold:
    """The accumulators one or more categories' features fold into.

    Extracted from ``facet_plan``'s loop when ``evidence_plan`` needed the
    identical admission rules over SEVERAL categories. The rules are the
    load-bearing part — non-public before anything else, ``skip`` kinds and
    ``facet: false`` excluded and un-re-admittable — and having them stated
    twice is how one of the two copies eventually stops excluding a VIN.
    """

    def __init__(self) -> None:
        self.kinds: dict[str, str] = {}
        self.closed: dict[str, tuple[str, ...]] = {}
        #: Slugs some declaring category leaves OPEN (``allowCustom``, or a
        #: vocabulary pointer, or no options at all). A closed option set is
        #: zero-filled, so one open declaration has to veto the fill for
        #: every declaration: otherwise a branch page offers options that
        #: exist in one of its leaves and in none of the others.
        self.open: set[str] = set()
        self.labels: dict[str, dict[str, str]] = {}
        self.translatable: dict[str, bool] = {}
        #: ``{slug: (name, translatable)}`` — the definition's own NAME, which
        #: is what a panel puts above the bucket list. Taken as a pair from
        #: ONE declarer so a name can never be paired with another
        #: declaration's opinion about whether it is a translation key.
        self.group_labels: dict[str, tuple[str, bool]] = {}
        #: Slugs whose declaring categories disagree about a caption's
        #: nature or a vocabulary's address. The reader cannot tell `b.apple`
        #: from `Б/у` by looking, and neither can this: on a disagreement the
        #: overlay is dropped and the raw code prints, which is honest.
        self.conflicted: set[str] = set()
        self.vocabulary_refs: dict[str, tuple[str, str]] = {}
        self.rank: dict[str, tuple[int, int]] = {}
        self.position: dict[str, int] = {}
        #: Documents in the candidate set whose category declares this slug.
        #: Zero for the single-category plan, which does not rank by it.
        self.weight: dict[str, int] = {}
        self.excluded: set[str] = set()
        self.hidden: set[str] = set()

    def admitted(self) -> list[str]:
        """Slugs that survived every exclusion, in first-authored order."""
        return sorted(
            (slug for slug in self.kinds if slug not in self.excluded),
            key=lambda slug: self.position[slug],
        )

    def closed_options(self) -> dict[str, tuple[str, ...]]:
        return {slug: values for slug, values in self.closed.items() if slug not in self.open}


def _group_label(feature: dict) -> tuple[str, bool] | None:
    """The definition's display name and whether it is a translation KEY.

    ``FeatureDef.name`` is the caption stapel-categories already stores for
    the axis, and ``FeatureDef.translate`` is the same module's declaration
    of what may be run through a catalogue: ``all`` (title + options),
    ``title``, or ``none``. So the group's caption is read exactly like an
    option's — the value plus the flag that says how to read it — rather
    than being guessed at from the slug, and a definition that carries no
    name yields NOTHING here instead of a fabricated one.

    Defaults to translatable, which is ``TranslateMode.ALL``: a definition
    written before the field existed reads as the model's own default.
    """
    name = str(feature.get("name") or "").strip()
    if not name:
        return None
    mode = str(feature.get("translate") or "all").strip().lower()
    return name, mode in ("all", "title")


def _collect(features: list[dict], fold: _Fold, *, weight: int = 0) -> None:
    """Fold one category's resolved features into *fold*, weighted by *weight*."""
    from .registry import get_facet_mapping

    for position, feature in enumerate(features):
        slug = feature.get("slug")
        config = feature.get("config") or {}
        type_slug = config.get("type") or ""
        if not slug or not type_slug:
            continue
        # Before the type, before the opt-out: a non-public feature is not an
        # axis at all, whatever it is made of. Ordering it first is what keeps
        # the rule from depending on a mapping the registry happens to know.
        #
        # Across categories this is FAIL-CLOSED by construction: `excluded` is
        # never un-set, so one category marking a slug `owner` withholds it
        # from a branch page whose other leaves call it public. A VIN that is
        # a VIN anywhere is a VIN here.
        if not _is_public(feature, config):
            fold.excluded.add(slug)
            fold.hidden.add(slug)
            continue
        mapping = get_facet_mapping(type_slug)
        if mapping.kind == "skip" or not _is_facetable(feature, config):
            fold.excluded.add(slug)
            continue
        fold.kinds[slug] = mapping.kind
        # First declarer wins the tie-breaks, and `evidence_plan` folds
        # categories BUSIEST FIRST, so the flags and the authored position
        # come from the category most of the weight came from.
        #
        # Taking the min of either was measured wrong on the reference stand:
        # under `/c/elektronika`, six categories holding one listing each sat
        # beside one holding 46, and a slug authored second in a one-listing
        # laptop schema jumped ahead of `color_ref_select` — authored fourth
        # in the schema 88.5% of the page is made of — off nothing but a
        # smaller index. A minority category may CONTRIBUTE an axis; it may
        # not reorder the majority's.
        fold.rank.setdefault(slug, _facet_rank(feature, config, mapping))
        fold.position.setdefault(slug, position)
        fold.weight[slug] = fold.weight.get(slug, 0) + weight
        label = _group_label(feature)
        if label is not None:
            fold.group_labels.setdefault(slug, label)
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
            fold.open.add(slug)
            vocabulary = _ref_field(config["optionsRef"], "vocabulary")
            level = _ref_field(config["optionsRef"], "level")
            if vocabulary and level:
                address = (vocabulary, level)
                if fold.vocabulary_refs.setdefault(slug, address) != address:
                    fold.conflicted.add(slug)
            continue
        options = config.get("options")
        if not options:
            fold.open.add(slug)
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
            fold.labels.setdefault(slug, {}).update(captions)
            flag = bool(config.get("translatable_options", True))
            if fold.translatable.setdefault(slug, flag) != flag:
                fold.conflicted.add(slug)
        if config.get("allowCustom"):
            fold.open.add(slug)
        else:
            values = tuple(captions)
            if values:
                fold.closed[slug] = tuple(dict.fromkeys(fold.closed.get(slug, ()) + values))
            else:
                fold.open.add(slug)


def _shape(
    fold: _Fold,
    ordered: list[str],
    *,
    requested: tuple[str, ...] | None,
    revision: Any = None,
    evidence: tuple[str, ...] = (),
) -> FacetPlan:
    """Cut *ordered* to the budget and dress it as a :class:`FacetPlan`."""
    from .conf import search_settings
    from .index_schema import CORE_RANGE_FIELDS

    max_fields = int(search_settings.MAX_FACET_FIELDS)
    kinds = fold.kinds

    if requested is not None:
        wanted = [slug for slug in requested if slug and slug not in fold.excluded]
        ordered = [slug for slug in wanted if slug in kinds] + [
            slug for slug in wanted if slug not in kinds
        ]
        for slug in ordered:
            kinds.setdefault(slug, "term")

    selected = tuple(ordered[:max_fields])
    closed = fold.closed_options()
    labels = {
        slug: values
        for slug, values in fold.labels.items()
        if slug not in fold.conflicted
    }
    refs = {
        slug: address
        for slug, address in fold.vocabulary_refs.items()
        if slug not in fold.conflicted
    }
    return FacetPlan(
        slugs=selected,
        kinds={slug: kinds.get(slug, "term") for slug in selected},
        closed_options={slug: closed[slug] for slug in selected if slug in closed},
        skipped=tuple(ordered[max_fields:]),
        hidden=tuple(sorted(fold.hidden)),
        revision=revision,
        option_labels={slug: labels[slug] for slug in selected if slug in labels},
        translatable_labels={
            slug: fold.translatable[slug]
            for slug in selected
            if slug in fold.translatable and slug not in fold.conflicted
        },
        vocabulary_refs={slug: refs[slug] for slug in selected if slug in refs},
        group_labels={
            slug: fold.group_labels[slug]
            for slug in selected
            if slug in fold.group_labels
        },
        # Not conditioned on the category: a core range addresses a column
        # every document in every corpus has. Announcing it here is what
        # lets a panel offer «Цена от … до …» without the frontend keeping
        # its own list of which slugs are core.
        core_ranges=tuple(CORE_RANGE_FIELDS),
        evidence=tuple(slug for slug in selected if slug in evidence),
    )


def facet_plan(
    category_id: Any = None, *, requested: tuple[str, ...] | None = None
) -> FacetPlan:
    """What to count for this query, from the queried category's own schema.

    ``requested`` is the caller's ``facets=`` list; ``None`` means "the
    category's plan". Slugs past ``MAX_FACET_FIELDS`` land in ``skipped``
    rather than vanishing — N active facet filters already cost N+1
    candidate sets, and the cap is what stops a wide category page from
    turning into a dozen sequential scans.

    ``categories.features`` resolves a category's own features plus the ones
    it inherits from its ANCESTORS, which is why this answers nothing at all
    for a branch (a branch declares no axes; its LEAVES do) and nothing for
    ``category_id=None`` (a text query names no category). That is D175, and
    :func:`evidence_plan` is where it is answered — here, deliberately, the
    behaviour is unchanged.
    """
    fold = _Fold()
    features, revision = _feature_defs(category_id) if category_id else ([], None)
    _collect(features, fold)
    ranked = sorted(
        fold.admitted(), key=lambda slug: (fold.rank[slug], fold.position[slug])
    )
    return _shape(fold, ranked, requested=requested, revision=revision)


def evidence_plan(
    category_counts: list[tuple[Any, int]], *, requested: tuple[str, ...] | None = None
) -> FacetPlan:
    """What to count, drawn from the categories the CANDIDATE SET contains.

    *category_counts* is ``[(category id, documents), …]`` — one aggregate
    over the query's own candidate set, busiest first (the optional backend
    verb ``category_counts``). Each of those categories' resolved features
    are folded in, and a slug's weight is the number of documents whose
    category declares it.

    **The ranking is coverage, and it is the fleet's existing one.** The
    frontend has ordered facet groups by ``facetCoverage`` — the sum of a
    group's bucket counts — since ``@stapel/search-react`` 0.18.0, on both
    the chip row and the rail, because schema order on the deployed phones
    leaf put battery health and four parcel dimensions above the brand.
    That function needs counts, which exist only after counting; this one
    needs to choose WHAT to count. So it ranks by the same quantity
    predicted from the aggregate — documents whose category declares the
    slug — and ``_facet_rank`` stays as the tie-break, unchanged, for slugs
    with equal support. Two surfaces sorting by evidence and a planner
    choosing by authoring flags is how a 12-slug budget gets spent on axes
    that describe nothing.

    What it deliberately does NOT do is union the catalogue subtree. On the
    reference stand ``elektronika``'s subtree is 210 categories declaring
    439 feature definitions; seven of them hold a listing, and one holds
    88.5% of them. The subtree is a description of the catalogue; the
    aggregate is a description of the corpus, and only one of the two is
    what the reader is looking at.
    """
    from .conf import search_settings

    limit = int(search_settings.FACET_EVIDENCE_CATEGORIES)
    pairs = [(cid, int(count)) for cid, count in list(category_counts)[:limit]]
    total = sum(count for _cid, count in pairs)
    fold = _Fold()
    for category_id, documents in pairs:
        features, _revision = _feature_defs(category_id)
        _collect(features, fold, weight=documents)

    def band(slug: str) -> int:
        """How much of the candidate set this slug's support covers, coarsely.

        Coarse on purpose, and the coarseness is the measured part. The
        weight is a PREDICTION — documents whose category DECLARES the slug
        — and declaring an axis is not the same fact as carrying a value for
        it. On the reference stand's ``/c/elektronika``, ``case_condition``
        is declared by the 46-listing phones leaf AND by a 1-listing laptop
        leaf, so it predicted 47 against ``color_ref_select``'s 46 and took
        its budget slot; the counts, once taken, were 31 and 44. One
        document of prediction is not a reason to reorder a panel, and no
        finer scale survives that — deciles do not, because 46 and 47 out of
        52 straddle a decile boundary.

        What the prediction CAN say is which of three things a slug is: an
        axis most of this page carries, one a tenth of it carries, or a
        sliver. Inside a band ``_facet_rank`` decides — unchanged, the same
        band-then-author's-flags ranking 0.8.0 wrote for a single category,
        which is what puts a vocabulary-backed choice above an optional
        select and both above a measurement. The sliver band is where a
        1-listing sibling leaf's axes land, and ``FACET_MIN_COVERAGE``
        withholds whatever of it still reaches the panel.
        """
        if total <= 0:
            return 0
        share = fold.weight[slug] / total
        return sum(1 for edge in EVIDENCE_BANDS if share < edge)

    ranked = sorted(
        fold.admitted(),
        key=lambda slug: (band(slug), fold.rank[slug], fold.position[slug]),
    )
    return _shape(fold, ranked, requested=requested, evidence=tuple(ranked))


def fill_zero_options(counts: dict[str, dict[str, int]], plan: FacetPlan) -> dict:
    """Add the zeros a closed option set owes the panel."""
    filled = {slug: dict(values) for slug, values in counts.items()}
    for slug, options in plan.closed_options.items():
        bucket = filled.setdefault(slug, {})
        for option in options:
            bucket.setdefault(option, 0)
    return filled


__all__ = [
    "evidence_plan",
    "category_path",
    "facet_plan",
    "fill_zero_options",
    "lookup_path",
    "lookup_slug",
    "note_changed",
    "note_path_degradation",
    "path_degradation",
    "reset_path_degradation",
    "slugs_for_ids",
]
