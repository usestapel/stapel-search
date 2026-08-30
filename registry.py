"""The four merge-registries of stapel-search.

Form is ``stapel-docs/doc_types.py`` verbatim in shape (library-standard
§3.3): ``BUILTIN_*`` -> ``STAPEL_SEARCH[...]`` settings overlay
(``{key: dotted-path | None}``, where ``None`` tombstones a builtin) ->
runtime ``register_*`` calls, last layer wins. Each registry has a paired
system check so a configured-but-broken entry is loud, never silent.

- ``SOURCES`` — **empty by design** (``BUILTIN_SOURCES = {}``, the
  ``stapel-reviews`` ``BUILTIN_TARGET_TYPES`` precedent). This module knows
  nothing about listings, chats or profiles; a :class:`SourceSpec` names the
  invalidation signals and the two comm Functions the document is pulled
  through, and the mapper that shapes it. The composite that is allowed to
  know both sides declares the entry (spec §3).
- ``FACET_MAPPINGS`` — keyed by *attribute type slug*. stapel-attributes
  carries no index metadata at all (spec §1.3), so index semantics live
  here: they are a property of the engine, not of the field type.
- ``SCORERS`` — the ranking registry, rendered into the P2B disclosure.
- ``DICTIONARIES`` — extra synonym/transliteration data per language.

An empty registry is an honest "none", not an undeclared promise: an
unbuilt doc type is not a declared-but-unwired artifact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# --------------------------------------------------------------------------
# shared overlay resolution
# --------------------------------------------------------------------------


def _resolve_overlay_entry(registry_key: str, key: str, dotted: str, expected: type):
    """Import ``dotted``, call it if it is a factory, and type-check it."""
    from django.utils.module_loading import import_string

    value = import_string(dotted)
    if callable(value) and not isinstance(value, expected):
        value = value()
    if not isinstance(value, expected):
        raise TypeError(
            f"STAPEL_SEARCH[{registry_key!r}][{key!r}] -> {dotted!r} is not a "
            f"{expected.__name__}"
        )
    return value


# --------------------------------------------------------------------------
# 1. document sources
# --------------------------------------------------------------------------


class SourceNotRegistered(LookupError):
    """No source is registered for this ``doc_type``."""

    def __init__(self, doc_type: str):
        self.doc_type = doc_type
        super().__init__(f"no search source registered for doc_type {doc_type!r}")


@dataclass(frozen=True)
class SourceSpec:
    """One indexable corpus, declared by the composite that owns the clue.

    ``signals`` are Action names treated as *invalidation*, never as
    documents: the payloads of ``listing.published`` / ``listing.updated``
    are ``additionalProperties: false`` and carry identity only. On a signal
    the indexer pulls the document through ``content_function``.

    **Visibility is never inferred from the event name** (spec §19.7): a
    republished live listing emits ``listing.updated`` carrying
    ``status: pending``, and ``listing.removed`` is not emitted at all.
    ``visible_statuses`` is the predicate, applied to the pulled document's
    own ``status``. ``removal_signals`` only shortcut the pull for events
    that mean "this key is gone"; the pull result still decides.
    """

    doc_type: str
    #: ``payload -> SearchDocumentInput``; the cross-domain glue.
    mapper: Callable[[dict], Any]
    #: comm Function: ``{"keys": [...]} -> {key: document}``.
    content_function: str
    #: comm Function: ``{"cursor", "limit"} -> {"rows", "cursor", "total"}``.
    export_function: str
    #: Action names that invalidate a key.
    signals: tuple[str, ...] = ()
    #: Subset of ``signals`` whose meaning is "gone" (still verified by pull).
    removal_signals: tuple[str, ...] = ()
    #: Payload keys holding the document key, tried in order.
    key_fields: tuple[str, ...] = ("listing_id", "key", "id")
    #: Document ``status`` values that are IN the index.
    visible_statuses: frozenset[str] = frozenset({"published"})

    def key_of(self, payload: dict) -> str | None:
        """The document key carried by an invalidation payload."""
        for name in self.key_fields:
            value = (payload or {}).get(name)
            if value not in (None, ""):
                return str(value)
        return None


BUILTIN_SOURCES: dict[str, SourceSpec] = {}

_runtime_sources: dict[str, SourceSpec] = {}


def register_source(spec: SourceSpec) -> None:
    """Register (or replace) a document source at runtime."""
    if not isinstance(spec, SourceSpec):
        raise TypeError(f"expected SourceSpec, got {type(spec)!r}")
    _runtime_sources[spec.doc_type] = spec


def unregister_source(doc_type: str) -> None:
    """Remove a runtime registration (tests)."""
    _runtime_sources.pop(doc_type, None)


def get_sources() -> dict[str, SourceSpec]:
    """The effective registry: builtins <- settings overlay <- runtime."""
    from .conf import search_settings

    registry = dict(BUILTIN_SOURCES)
    for key, dotted in (search_settings.SOURCES or {}).items():
        if dotted is None:
            registry.pop(key, None)
        else:
            registry[key] = _resolve_overlay_entry("SOURCES", key, dotted, SourceSpec)
    registry.update(_runtime_sources)
    return registry


def get_source(doc_type: str) -> SourceSpec:
    """Spec for *doc_type*, or :class:`SourceNotRegistered`."""
    try:
        return get_sources()[doc_type]
    except KeyError:
        raise SourceNotRegistered(doc_type) from None


# --------------------------------------------------------------------------
# 2. facet mappings, keyed by attribute-type slug
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FacetMapping:
    """How one stapel-attributes type lands in the index.

    ``kind`` is a closed vocabulary: ``term`` (a discrete filterable value),
    ``range`` (a number, also written to ``SearchNumber``), ``path``
    (root->leaf, rolled up by prefix) or ``skip``.
    """

    kind: str
    extract: Callable[[dict], list]
    numeric: bool = False

    _KINDS = frozenset({"term", "range", "path", "skip"})

    def __post_init__(self) -> None:
        if self.kind not in self._KINDS:
            raise ValueError(
                f"FacetMapping.kind must be one of {sorted(self._KINDS)}, got {self.kind!r}"
            )


def _value_list(dao: dict) -> list:
    value = (dao or {}).get("value")
    return [] if value is None else [value]


def _sequence_value(dao: dict) -> list:
    return list((dao or {}).get("value") or [])


def _hex_color_simple(dao: dict) -> list:
    # hex_color has NO `value`: it flattens to simple/hex/label, and `simple`
    # is the facet axis (stapel-attributes/types/hex_color/constants.py:3-10).
    # `hex` is a paint code and `label` is a translation key — neither is a
    # thing a user filters by.
    simple = (dao or {}).get("simple")
    return [] if simple in (None, "") else [simple]


#: The thirteen builtin stapel-attributes types (spec §5.3). `convertible_unit`
#: is always stored in the family's base unit, so no read-time conversion.
#: `date` is a unix timestamp int, which is why it is a range, not a term.
#: The two vocabulary-backed types index exactly like their inline twins: the
#: DAO's `value` is term CODES (`labels` is a display snapshot), so `ref_select`
#: is a term axis and `ref_hierarchical_select` a root->leaf path.
#: `group` (the composite) joins `header` on the `skip` branch: its DAO value is
#: a list of ROWS of child DAOs, so it has no single value to filter on — five
#: discount-ladder steps are one answer, not five terms, and flattening them
#: would count a row rather than a listing. A composite is a form shape, not a
#: search axis; a child worth filtering on belongs outside the group.
BUILTIN_FACET_MAPPINGS: dict[str, FacetMapping] = {
    "int": FacetMapping("range", _value_list, numeric=True),
    "float": FacetMapping("range", _value_list, numeric=True),
    "convertible_unit": FacetMapping("range", _value_list, numeric=True),
    "date": FacetMapping("range", _value_list, numeric=True),
    "string": FacetMapping("term", _value_list),
    "bool": FacetMapping("term", _value_list),
    "select": FacetMapping("term", _sequence_value),
    "hierarchical_select": FacetMapping("path", _sequence_value),
    "ref_select": FacetMapping("term", _sequence_value),
    "ref_hierarchical_select": FacetMapping("path", _sequence_value),
    "hex_color": FacetMapping("term", _hex_color_simple),
    "header": FacetMapping("skip", lambda dao: []),
    "group": FacetMapping("skip", lambda dao: []),
}

#: The branch a fourteenth, host-registered type falls into. Indexing it
#: silently is the disease §11 exists to stop, so taking this branch raises
#: ``search.W002`` and logs once per slug.
DEFAULT_FACET_MAPPING = FacetMapping("term", _value_list)

_runtime_facet_mappings: dict[str, FacetMapping] = {}

#: Slugs that fell through to DEFAULT_FACET_MAPPING, for search.W002.
_defaulted_slugs: set[str] = set()

_facet_cache: dict[str, Any] = {"version": None, "value": None}


def register_facet_mapping(slug: str, mapping: FacetMapping) -> None:
    """Register (or replace) a facet mapping at runtime."""
    if not isinstance(mapping, FacetMapping):
        raise TypeError(f"expected FacetMapping, got {type(mapping)!r}")
    _runtime_facet_mappings[slug] = mapping
    _facet_cache["version"] = None


def unregister_facet_mapping(slug: str) -> None:
    """Remove a runtime registration (tests)."""
    _runtime_facet_mappings.pop(slug, None)
    _facet_cache["version"] = None


def _attributes_registry_version() -> Any:
    """Cache key mirroring stapel-attributes' own monotonic registry version."""
    try:
        from stapel_attributes.registry import registry_version
    except Exception:  # pragma: no cover - attributes is a hard dependency
        return None
    return registry_version()


def get_facet_mappings() -> dict[str, FacetMapping]:
    """The effective registry, memoized on ``registry_version()``.

    A host registering a further feature type changes the DAO shape under us
    (spec risk §17.6); keying the cache on attributes' own monotonic version
    is what makes the mapping follow instead of going stale.
    """
    from .conf import search_settings

    version = (_attributes_registry_version(), id(search_settings))
    if _facet_cache["version"] == version and _facet_cache["value"] is not None:
        return dict(_facet_cache["value"])

    registry = dict(BUILTIN_FACET_MAPPINGS)
    for slug, dotted in (search_settings.FACET_MAPPINGS or {}).items():
        if dotted is None:
            registry.pop(slug, None)
        else:
            registry[slug] = _resolve_overlay_entry(
                "FACET_MAPPINGS", slug, dotted, FacetMapping
            )
    registry.update(_runtime_facet_mappings)

    _facet_cache["version"] = version
    _facet_cache["value"] = registry
    return dict(registry)


def get_facet_mapping(type_slug: str) -> FacetMapping:
    """Mapping for *type_slug*, falling back to the generic term branch.

    The fallback is recorded (``defaulted_type_slugs()``) so ``search.W002``
    can name it: "type X is indexed by the default — declare a mapping or
    confirm the default".
    """
    mapping = get_facet_mappings().get(type_slug)
    if mapping is None:
        _defaulted_slugs.add(type_slug)
        return DEFAULT_FACET_MAPPING
    return mapping


def defaulted_type_slugs() -> frozenset[str]:
    """Attribute-type slugs that took the generic branch this process."""
    return frozenset(_defaulted_slugs)


def reset_defaulted_type_slugs() -> None:
    """Forget the generic-branch record (tests)."""
    _defaulted_slugs.clear()


# --------------------------------------------------------------------------
# 3. scorers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scorer:
    """One weighted contribution to the relevance score.

    ``applies_to_sorts`` is the structure that expresses the module invariant
    "an explicit sort never gets a promotion boost" (spec §7.3): the invariant
    is a property of the registry, not a check repeated in three places.
    """

    slug: str
    weight: float
    description_key: str
    params: dict = field(default_factory=dict)
    applies_to_sorts: frozenset[str] = frozenset({"relevance"})


BUILTIN_SCORERS: dict[str, Scorer] = {
    "relevance": Scorer("relevance", 1.0, "search.scorer.relevance", {}),
    "freshness_decay": Scorer(
        "freshness_decay", 0.3, "search.scorer.freshness", {"half_life_days": 14}
    ),
    "geo_decay": Scorer("geo_decay", 0.3, "search.scorer.geo", {"max_radius_km": 50}),
    "promotion_boost": Scorer("promotion_boost", 1.0, "search.scorer.promotion", {}),
    "popularity": Scorer("popularity", 0.2, "search.scorer.popularity", {}),
}

_runtime_scorers: dict[str, Scorer] = {}


def register_scorer(scorer: Scorer) -> None:
    """Register (or replace) a scorer at runtime."""
    if not isinstance(scorer, Scorer):
        raise TypeError(f"expected Scorer, got {type(scorer)!r}")
    _runtime_scorers[scorer.slug] = scorer


def unregister_scorer(slug: str) -> None:
    """Remove a runtime registration (tests)."""
    _runtime_scorers.pop(slug, None)


def get_scorers() -> dict[str, Scorer]:
    """The effective registry: builtins <- settings overlay <- runtime."""
    from .conf import search_settings

    registry = dict(BUILTIN_SCORERS)
    for slug, dotted in (search_settings.SCORERS or {}).items():
        if dotted is None:
            registry.pop(slug, None)
        else:
            registry[slug] = _resolve_overlay_entry("SCORERS", slug, dotted, Scorer)
    registry.update(_runtime_scorers)
    return registry


# --------------------------------------------------------------------------
# 4. dictionaries
# --------------------------------------------------------------------------

_runtime_dictionaries: dict[str, list[Any]] = {}


def register_dictionary(language: str, source: Any) -> None:
    """Add a synonym/stopword source for *language* at runtime.

    *source* is a dict in the on-disk format, a filesystem path, or a dotted
    path to either.
    """
    _runtime_dictionaries.setdefault(language, []).append(source)
    from . import text

    text.reset_dictionary_cache()


def unregister_dictionaries(language: str | None = None) -> None:
    """Drop runtime dictionary sources (tests)."""
    if language is None:
        _runtime_dictionaries.clear()
    else:
        _runtime_dictionaries.pop(language, None)
    from . import text

    text.reset_dictionary_cache()


def get_dictionary_sources(language: str) -> list[Any]:
    """Extra sources for *language*: settings overlay then runtime."""
    from .conf import search_settings

    sources: list[Any] = []
    configured = (search_settings.DICTIONARIES or {}).get(language)
    if configured is None:
        pass
    elif isinstance(configured, (str, dict)):
        sources.append(configured)
    elif isinstance(configured, Sequence):
        sources.extend(configured)
    sources.extend(_runtime_dictionaries.get(language, []))
    return sources


__all__ = [
    "BUILTIN_FACET_MAPPINGS",
    "BUILTIN_SCORERS",
    "BUILTIN_SOURCES",
    "DEFAULT_FACET_MAPPING",
    "FacetMapping",
    "Scorer",
    "SourceNotRegistered",
    "SourceSpec",
    "defaulted_type_slugs",
    "get_dictionary_sources",
    "get_facet_mapping",
    "get_facet_mappings",
    "get_scorers",
    "get_source",
    "get_sources",
    "register_dictionary",
    "register_facet_mapping",
    "register_scorer",
    "register_source",
    "reset_defaulted_type_slugs",
    "unregister_dictionaries",
    "unregister_facet_mapping",
    "unregister_scorer",
    "unregister_source",
]
