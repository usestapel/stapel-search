"""Response shapes, declared — so ``docs/schema.json`` describes reality.

These serializers are documentation-first: the service layer already
produces plain dicts (it is reachable from the comm Function without DRF at
all), and these declare that shape to drf-spectacular so the OpenAPI
artifact is a contract instead of a shrug. An endpoint whose schema says
"unknown object" is an endpoint a generated client cannot type, and this
package's whole argument is that a promise nobody can check is not a
promise.
"""
from rest_framework import serializers


class SearchItemSerializer(serializers.Serializer):
    """One result row."""

    key = serializers.CharField(help_text="The source's own document key.")
    score = serializers.FloatField(help_text="Ranking score; 0 under an explicit sort.")
    promoted = serializers.BooleanField(
        help_text=(
            "Whether this result is promoted. Present on EVERY item under EVERY "
            "sort, including when false — a mandatory marking (DSA Art. 26), not "
            "an optional field."
        )
    )
    distance_km = serializers.FloatField(
        allow_null=True, help_text="Great-circle distance from the searched centre."
    )
    card = serializers.DictField(
        help_text="Stored row fields, so a result page costs one query."
    )


class FacetMetaSerializer(serializers.Serializer):
    approximate = serializers.BooleanField(
        help_text="True when counts came from a sample because the candidate set "
        "exceeded FACET_CANDIDATE_CAP."
    )
    candidates = serializers.IntegerField(help_text="Size of the largest counted set.")
    counted = serializers.ListField(child=serializers.CharField())
    skipped = serializers.ListField(
        child=serializers.CharField(),
        help_text="Plan slugs dropped at MAX_FACET_FIELDS — reported, not vanished.",
    )
    core_ranges = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "Range slugs that address a core document column rather than an "
            "attribute (`r.price`). Offer them as filters unconditionally: "
            "they exist for every document in every category, which is why "
            "they are not in the category's own plan."
        ),
    )


class FacetLabelsSerializer(serializers.Serializer):
    """Captions for one slug's option codes."""

    translatable = serializers.BooleanField(
        help_text=(
            "True when `values` holds translation KEYS to run through the "
            "catalogue; false when it holds literal captions. The reader "
            "cannot tell by looking — `b.apple` and `Б/у` are both strings."
        )
    )
    values = serializers.DictField(child=serializers.CharField())


class SearchResponseSerializer(serializers.Serializer):
    """The query envelope: AnchorPagination's keys, plus what search owes."""

    items = SearchItemSerializer(many=True)
    facets = serializers.DictField(
        child=serializers.DictField(child=serializers.IntegerField()),
        help_text="{slug: {value: count}}, counted with the slug's own filter removed.",
    )
    facet_labels = serializers.DictField(
        child=FacetLabelsSerializer(),
        help_text=(
            "{slug: {translatable, values: {value: caption}}} for slugs whose "
            "options are inline in the category schema. Absent for a "
            "vocabulary-backed slug: its level lives outside the schema and "
            "the plan will not invent a caption it has not read."
        ),
    )
    facet_meta = FacetMetaSerializer()
    next_anchor = serializers.CharField(allow_null=True)
    prev_anchor = serializers.CharField(allow_null=True)
    has_next = serializers.BooleanField()
    has_prev = serializers.BooleanField()
    count = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "How many documents match. NEVER 0 beside a non-empty items[] — the "
            "answer may not claim fewer matches than the page shows. `null` means "
            "the engine cannot say, and is rendered as no count at all."
        ),
    )
    count_is_lower_bound = serializers.BooleanField(
        help_text=(
            "True when `count` is a floor: at least this many match, possibly "
            "more (a capped count, a window-truncated engine answer). Render "
            "'N+', never 'N'."
        )
    )
    exact_total = serializers.BooleanField(
        help_text=(
            "True when `count` is exact for THIS answer — equivalent to "
            "`count is not null and not count_is_lower_bound`. Per answer, not "
            "per engine: an engine without a guaranteed exact total still counts "
            "a small candidate set exactly."
        )
    )
    degraded = serializers.ListField(
        child=serializers.CharField(),
        help_text="What the configured engine could not do for this query.",
    )
    backend = serializers.CharField()
    language = serializers.CharField(
        help_text=(
            "The language whose dictionary and analyzer configuration answered "
            "— `lang`, else Accept-Language, else DEFAULT_LANGUAGE. When the "
            "fallback is wrong the synonym layer silently does not apply, and "
            "this field is the only place the answer says so."
        )
    )
    sort = serializers.CharField()
    took_ms = serializers.IntegerField()


class SuggestResponseSerializer(serializers.Serializer):
    items = serializers.ListField(child=serializers.CharField())
    backend = serializers.CharField()


class ScorerSerializer(serializers.Serializer):
    slug = serializers.CharField()
    weight = serializers.FloatField()
    description_key = serializers.CharField()
    description = serializers.CharField()
    params = serializers.DictField()
    applies_to_sorts = serializers.ListField(child=serializers.CharField())
    active = serializers.BooleanField(
        help_text="False when the configured engine cannot evaluate this parameter."
    )
    inactive_reason = serializers.CharField(allow_blank=True)


class RankingResponseSerializer(serializers.Serializer):
    """The P2B Art. 5 disclosure, generated from the scorer registry."""

    doc_type = serializers.CharField(allow_blank=True)
    backend = serializers.CharField(allow_blank=True)
    scorers = ScorerSerializer(many=True)
    notes = serializers.ListField(child=serializers.CharField())


class HealthResponseSerializer(serializers.Serializer):
    backend = serializers.CharField()
    reachable = serializers.BooleanField()
    detail = serializers.CharField(allow_blank=True)
    documents = serializers.IntegerField(allow_null=True, required=False)
    capabilities = serializers.DictField(required=False)
    types = serializers.ListField(child=serializers.CharField())
    lag_seconds = serializers.IntegerField(allow_null=True, required=False)
    stale_reason = serializers.CharField(allow_blank=True, required=False)


class ReindexRequestSerializer(serializers.Serializer):
    doc_type = serializers.CharField()
    keys = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Re-pull exactly these keys; omit to rebuild the whole type.",
    )


class ReindexResponseSerializer(serializers.Serializer):
    doc_type = serializers.CharField()
    indexed = serializers.IntegerField()
    removed = serializers.IntegerField()
    skipped_stale = serializers.IntegerField()
    skipped_duplicate = serializers.IntegerField()


__all__ = [
    "FacetMetaSerializer",
    "HealthResponseSerializer",
    "RankingResponseSerializer",
    "ReindexRequestSerializer",
    "ReindexResponseSerializer",
    "ScorerSerializer",
    "SearchItemSerializer",
    "SearchResponseSerializer",
    "SuggestResponseSerializer",
]
