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
        allow_null=True,
        help_text=(
            "Great-circle distance from the searched centre, in km. For an "
            "ANONYMOUS reader it is measured from the same ~1.1km grid point "
            "the card publishes and floored to that grid's quantum (the "
            "cell's diagonal, ~1.574km): a distance finer than the position "
            "it came from is the position, three requests away. The listing's "
            "own owner, staff and the service transport get the exact number. "
            "Coarse is enough for what it drives — a card saying \u00ab12 "
            "\u043a\u043c\u00bb does not need metres — and it never "
            "overstates proximity, being floored rather than rounded."
        ),
    )
    band = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "`nearby` | `all` | `\"\"`. Which partition this row sits in, "
            "present only under `geo_mode=rank`. A LABEL, never a filter: "
            "`all` carries every remaining row, including rows with no "
            "coordinates at all, so a query never returns fewer results "
            "because of distance. `\"\"` means the request ranked by "
            "proximity but gave no centre, which is not an error."
        ),
    )
    match_count = serializers.IntegerField(
        required=False,
        help_text=(
            "How many of the filters the QUERY's own words produced this row "
            "satisfies — including the soft ones that were not applied as "
            "filters. Orders rows within a band, strongest first. Present only "
            "when the query produced signals."
        ),
    )
    card = serializers.DictField(
        help_text=(
            "Stored row fields, so a result page costs one query. For an "
            "ANONYMOUS reader the card never carries full-precision "
            "coordinates, on every path and whatever the flags: a key naming "
            "half a pair (`lat`, `latitude`, `lon`, `lng`, `*_lat`\u2026) is "
            "rewritten onto the ~1.1km public grid from the row's own "
            "columns, and a key carrying a position this module cannot turn "
            "into an area (`geohash`, `location`, `coordinates`\u2026) is "
            "removed rather than truncated \u2014 two differently-aligned "
            "areas around one point intersect down to a sliver. "
            "`geo_precision_km` says how wide the area is: draw a CIRCLE, "
            "never a marker. Under `geo_mode=rank` the pair is ADDED even to "
            "a card that carried no position at all."
        )
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
    dropped_filters = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "`f.<slug>`/`r.<slug>` filters this answer did NOT apply, because "
            "the category marks the slug non-public (`FeatureDef.visibility` "
            "is `owner` or `staff`). A hidden value is not indexed and is not "
            "filterable: letting `?f.vin=<value>` through would make the index "
            "an exact-match oracle over an identifier. The answer is wider "
            "than what was asked for, and says so here rather than silently."
        ),
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
    plan = serializers.CharField(
        help_text=(
            "Where the plan came from. `category` — the queried category's "
            "own authored schema. `evidence` — the categories the CANDIDATE "
            "SET actually contains, used when that schema did not fill "
            "MAX_FACET_FIELDS, which is every branch category and every text "
            "query (`categories.features` resolves own + ANCESTOR-inherited "
            "features, so a branch owns no axes; its leaves do)."
        )
    )
    withheld = serializers.ListField(
        child=serializers.DictField(),
        help_text=(
            "`{slug, coverage, candidates}` for groups that were counted and "
            "then withheld because they describe too little of the result "
            "set (FACET_MIN_COVERAGE). Present so a panel can say «3 filters "
            "apply to too few of these» instead of «no filters» — the second "
            "is false whenever this list is not empty. Only slugs the "
            "evidence plan borrowed from another category are ever here, and "
            "never one the reader has already filtered on."
        ),
    )
    categories = serializers.ListField(
        child=serializers.DictField(),
        help_text=(
            "`{category, count}` — the categories this answer's candidate "
            "set is made of, busiest first, `category` being the same "
            "slash-joined id path the `category` filter takes. The evidence "
            "the plan was drawn from, and what a panel needs to offer the "
            "CATEGORY itself as the first filter on a text search. Empty "
            "when the plan is the queried category's own."
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


class BandSummarySerializer(serializers.Serializer):
    """One partition of the answer — a heading, never a filter over it."""

    id = serializers.ChoiceField(choices=["nearby", "all"])
    count = serializers.IntegerField(
        allow_null=True,
        help_text="How many rows are in this band. `null` when the engine "
        "cannot say. The two counts add up to the top-level `count`, which is "
        "the machine-checkable form of 'nothing is hidden by distance'.",
    )
    count_is_lower_bound = serializers.BooleanField(
        help_text="True when `count` is a floor (a capped count). Render 'N+'."
    )
    radius_km = serializers.FloatField(
        required=False,
        help_text="Present on `nearby` only: the edge, in km, the band was cut "
        "at. `all` has no edge — it is everything else, which is the promise.",
    )


class ExtractedFilterSerializer(serializers.Serializer):
    """One filter the query's own words produced, ready to be replayed."""

    slug = serializers.CharField()
    value = serializers.CharField()
    label = serializers.CharField(allow_blank=True)
    value_label = serializers.CharField(allow_blank=True)
    method = serializers.ChoiceField(choices=["exact", "translit", "alias", "vector"])
    confidence = serializers.FloatField()
    span = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="`[start, end)` into the RAW query, so a UI can underline "
        "the words that became this chip.",
    )
    param = serializers.CharField(
        help_text="The literal query parameter this filter IS — e.g. "
        "`f.color=krasnyy`. Re-send it verbatim to KEEP the chip, omit it to "
        "remove it, and send `qu=off` alongside so the server does not "
        "extract it again. Nothing about extraction is remembered "
        "server-side, which is why the parameter has to be complete.",
    )
    applied = serializers.BooleanField(
        help_text="True when this filter actually narrowed the answer. A "
        "false one still contributed to each row's `match_count`.",
    )


class QueryUnderstandingSerializer(serializers.Serializer):
    """What a free-text query turned out to be ABOUT, beside its words."""

    filters = ExtractedFilterSerializer(many=True)
    category_path = serializers.ListField(child=serializers.CharField())
    category_confidence = serializers.FloatField()
    residual = serializers.CharField(
        allow_blank=True,
        help_text="The query with every extracted span removed — the text the "
        "engine still had to match. Empty is normal and correct: «красный» is "
        "entirely a filter, and the answer is then the filters' set.",
    )
    degraded = serializers.ListField(child=serializers.CharField())


class SearchResponseSerializer(serializers.Serializer):
    """The query envelope: AnchorPagination's keys, plus what search owes."""

    items = SearchItemSerializer(many=True)
    bands = BandSummarySerializer(
        many=True,
        required=False,
        help_text=(
            "Per-band counts in render order — `nearby` («Объявления "
            "поблизости») then `all` («Все объявления»). Present only under "
            "`geo_mode=rank`; empty when no centre was given. The rows "
            "themselves carry `band`; this is the summary a heading needs. "
            "The top-level `count` remains the WHOLE matching total, never "
            "the nearby one."
        ),
    )
    query_understanding = QueryUnderstandingSerializer(
        required=False,
        help_text=(
            "What the query's words became. Absent entirely while "
            "QUERY_UNDERSTANDING is off or `qu=off` was sent."
        ),
    )
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


class CategorySuggestionSerializer(serializers.Serializer):
    """One destination in the dropdown, ready to render and ready to follow."""

    id = serializers.IntegerField(
        help_text="Category id. A `listings`-graded row derives it from the "
        "path's leaf segment.",
    )
    slug = serializers.CharField(
        allow_blank=True,
        help_text="Category slug. Empty on a `listings`-graded row: no comm "
        "Function in the fleet resolves a path id to its slug or name yet.",
    )
    name = serializers.CharField(
        help_text="The category's own display name. On a `listings`-graded "
        "row this is the leaf id as a truthful segment — render such rows by "
        "their `count` instead.",
    )
    path = serializers.ListField(
        child=serializers.CharField(),
        help_text="Display names root->leaf, e.g. ['Мужская одежда', 'Шорты']. "
        "This is what distinguishes three categories that share a name. "
        "`listings`-graded rows carry the id segments here.",
    )
    category = serializers.CharField(
        help_text="The ancestry as ids joined with '/'. Pass it verbatim as the "
        "`category` parameter of /query — do not re-join path segments yourself.",
    )
    count = serializers.IntegerField(
        help_text="Live listings a buyer would see under this category, "
        "descendants included — the same number the SERP reports for it. On a "
        "`listings`-graded row: how many of them match the typed query, which "
        "is the count a `?q=…&category=…` tap will show.",
    )
    count_scope = serializers.ChoiceField(
        choices=["category", "query_in_category"],
        help_text="What `count` counted. `category`: everything live under "
        "this category, the typed text ignored — the row is a PLACE. "
        "`query_in_category`: only the documents matching the typed text, "
        "which is what a `listings`-graded row is about. The two are "
        "different numbers about different pages, which is why the row says "
        "which one it is instead of leaving a storefront to guess.",
    )
    query = serializers.DictField(
        child=serializers.CharField(),
        help_text="The /query parameters this row's `count` was computed for "
        "— send them VERBATIM (plus your own `type`/`lang`/paging) when the "
        "buyer follows the row. Always carries `category`; carries `q` only "
        "when `count_scope` is `query_in_category`. Assembling these "
        "yourself re-opens the defect this field exists to close: a place "
        "row followed with the typed text opens an empty page while its "
        "count promised stock.",
    )
    depth = serializers.IntegerField(help_text="Number of segments in `path`.")
    match = serializers.ChoiceField(
        choices=["exact", "prefix", "word", "substring", "listings", "vector"],
        help_text="How the row earned its place: the four name grades from "
        "`categories.suggest`, or `listings` — a goods-driven row, offered "
        "because documents matching the query live there even though no "
        "category name says the word. Ranking: strong grades "
        "(exact/prefix/word) and `listings` sort as one class above "
        "`substring`; within a class, stocked before empty, then grade, then "
        "count desc, then depth, then name. `vector` rows — embedding-space "
        "neighbours offered when nothing first-class matched — always sit "
        "below every deterministic row, ordered by similarity.",
    )


class SuggestResponseSerializer(serializers.Serializer):
    categories = CategorySuggestionSerializer(
        many=True,
        help_text="Destinations, ranked by match class (strong name grades and "
        "goods-driven rows above substring), then stocked before empty, then "
        "grade, then live listing count desc, then depth, then name.",
    )
    terms = serializers.ListField(
        child=serializers.CharField(), help_text="Title prefixes from the index."
    )
    items = serializers.ListField(
        child=serializers.CharField(),
        help_text="Deprecated alias of `terms`, kept for one minor.",
    )
    language = serializers.CharField(
        help_text="Which dictionary answered — the same resolution /query reports."
    )
    degraded = serializers.ListField(
        child=serializers.CharField(),
        help_text="What this answer could not do: `category_suggestions` (no "
        "provider for category names), `category_rollup` (no ancestry, so counts "
        "would read 0), `category_listing_suggestions` (no name matched and the "
        "configured engine does not implement the optional goods-driven verb, "
        "or it failed).",
    )
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
    "BandSummarySerializer",
    "ExtractedFilterSerializer",
    "FacetMetaSerializer",
    "HealthResponseSerializer",
    "QueryUnderstandingSerializer",
    "RankingResponseSerializer",
    "ReindexRequestSerializer",
    "ReindexResponseSerializer",
    "ScorerSerializer",
    "SearchItemSerializer",
    "SearchResponseSerializer",
    "CategorySuggestionSerializer",
    "SuggestResponseSerializer",
]
