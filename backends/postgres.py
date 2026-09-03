"""The default backend: Postgres FTS + pg_trgm + two GIN structures.

Zero new infrastructure. The corpus is this module's own table, which is the
direct lesson of ``stapel_geo/search/postgres.py:30-34`` — a backend can
only query a corpus its module owns.

Two structures, both GIN, both required (spec §9.1):

1. ``facets jsonb`` + ``jsonb_path_ops`` — the authoritative FILTER
   structure. ``jsonb_path_ops`` over the default ``jsonb_ops`` on purpose:
   2-3x smaller and faster on ``@>``, and ``@>`` is the only operator a
   facet filter ever needs.
2. ``facet_terms_arr text[]`` + ``array_ops`` — the COUNTING structure.
   Remaining-option counts need the document's values unfolded, and over a
   flat array that is one lateral ``unnest`` per candidate; over jsonb it
   would be ``jsonb_each`` + ``jsonb_array_elements`` — two lateral joins
   and a parse per row. Materializing the array once at index time pays for
   itself on every query, which the benchmark measured rather than assumed.

The three Postgres-only columns (``text_vec``, ``facet_terms_arr``,
``category_path_arr``) are added by migration ``0002`` and maintained HERE,
on ``upsert``. Keeping them out of the Django model is what lets the naive
backend run the same suite on SQLite; keeping them maintained by the
backend rather than by a generated column is what lets ``FTS_CONFIGS``
stay a setting instead of being frozen into a migration.

Cost is known, not guessed (``tasks/search-facet-benchmark.md``): counting
is ``O(candidates x terms-per-doc)``, comfortable to ~15k candidates and
degrading superlinearly under concurrency above it. Hence the candidate cap
with a TABLESAMPLE fallback that is live from day one, not "when needed".
"""
from __future__ import annotations

import logging
import re as _re
from decimal import Decimal

from django.db import connection

from ..dto import (
    BackendCapabilities,
    BackendHealth,
    BandSummary,
    FacetPlan,
    FacetResult,
    Hit,
    IndexDocument,
    IndexSettings,
    QueryResult,
    SearchQuery,
)
from . import _shared as shared

logger = logging.getLogger(__name__)

#: A term that can be spliced into a ``to_tsquery`` string as one
#: alternative. Anything else — a space, a slash, a hyphen, any tsquery
#: metacharacter — goes through ``phraseto_tsquery`` as a bound PARAMETER
#: instead, where the text is tokenized rather than parsed as syntax.
_SIMPLE_TERM = _re.compile(r"^[^\W_]+$", _re.UNICODE)


def _tsquery_expression(terms, config: str, params: list) -> str:
    """Build one SQL ``tsquery`` expression for a normalized query.

    Groups are AND-ed (``&&``), a group's expansions OR-ed (``||``). Two
    kinds of expansion exist and they cannot share a rendering:

    - **single-word** members are alternatives inside one ``to_tsquery``
      string, which is what they always were;
    - **multi-word** members («бывший в употреблении», and every hyphenated
      or slashed form such as ``б/у`` and ``second-hand``) become a
      ``phraseto_tsquery`` term.

    Before 0.4.0 every member was concatenated into the ``to_tsquery``
    string after stripping only ``'`` and ``\\``. A multi-word member
    therefore produced ``to_tsquery('(бу | б/у | бывший в употреблении)')``
    — a **syntax error in tsquery**, i.e. a 500 for any query whose language
    dictionary happened to contain a phrase. The shipped ``ru`` dictionary
    has contained one since 0.1.0; nothing reached it because nothing on the
    stand resolved a language until now.

    Rendering the phrase properly rather than dropping it is why this engine
    can now declare ``phrase_synonyms``: ``phraseto_tsquery`` is exactly the
    adjacency the capability names, and it is what removes the standing
    «Синонимы не подставлялись» notice at its source rather than hiding it.
    """
    parts: list[str] = []
    for group in terms:
        members = [term.strip() for term in group if term and term.strip()]
        simple = [term for term in members if _SIMPLE_TERM.match(term)]
        phrases = [term for term in members if not _SIMPLE_TERM.match(term)]
        alternatives: list[str] = []
        if simple:
            alternatives.append("to_tsquery(%s::regconfig, %s)")
            params.append(config)
            params.append(" | ".join(simple))
        for phrase in phrases:
            alternatives.append("phraseto_tsquery(%s::regconfig, %s)")
            params.append(config)
            params.append(phrase)
        if alternatives:
            parts.append("(" + " || ".join(alternatives) + ")")
    if not parts:
        return ""
    return " && ".join(parts)

#: Read paths of ``docs/index.json`` and the symbol in THIS module that
#: answers each (``IDX002``).
READ_PATH_IMPL = {
    "filter:type": "_where",
    "filter:lang": "_where",
    "filter:owner": "_where",
    "filter:category": "_where",
    "filter:facet": "_where",
    "filter:range": "_where",
    "filter:radius": "_where",
    "filter:bbox": "_where",
    "facets:counts": "facets",
    "q": "_text_predicate",
    "sort:newest": "_order_by",
    "sort:price_asc": "_order_by",
    "sort:price_desc": "_order_by",
    "sort:distance": "_order_by",
    "score:popularity": "_score_expression",
    "score:promotion_boost": "_score_expression",
    "geo:prefilter": "_where",
}

_TABLE = "search_document"

#: Earth radius used by the in-SQL haversine. Matches the constant behind
#: ``stapel_geo.geohash.distance_km`` closely enough that the two agree to
#: well under a metre at city scale; the conformance suite compares
#: distances with a tolerance rather than for bit-equality, because two
#: engines computing the same great circle to the last float bit is not a
#: property any contract should promise.
_EARTH_KM = 6371.0


class PostgresBackendUnavailable(RuntimeError):
    """Raised when the configured Postgres backend has no Postgres."""


class PostgresSearchBackend:
    """FTS + trigram + GIN facets over ``search_document``."""

    name = "postgres"

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _require_postgres() -> None:
        """Refuse to run on another vendor — loudly, not by degrading.

        ``stapel-recordings`` and ``stapel-studio`` fall back to
        ``icontains`` off Postgres. For a search library that would be a lie
        told in the shape of a working answer, so this raises and
        ``search.E003`` says the same thing at deploy time. A SQLite
        deployment configures the naive backend and gets an honest
        ``typo_tolerance: False``.
        """
        if connection.vendor != "postgresql":
            raise PostgresBackendUnavailable(
                "PostgresSearchBackend requires a PostgreSQL connection, but the "
                f"default connection vendor is {connection.vendor!r}. Point "
                "STAPEL_SEARCH['BACKEND'] at "
                "stapel_search.backends.naive.NaiveSearchBackend for SQLite."
            )

    @staticmethod
    def has_trigram() -> bool:
        if connection.vendor != "postgresql":
            return False
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            return cursor.fetchone() is not None

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        from ..conf import search_settings

        return BackendCapabilities(
            typo_tolerance=self.has_trigram(),
            facet_counts=True,
            # Exact only up to the cap; above it the answer is sampled and
            # says so in `facet_meta.approximate` + degraded[].
            exact_facet_counts=False,
            # A window count would mean counting the whole candidate set on
            # every page, which is the cost this backend is trying not to
            # pay twice.
            exact_total=False,
            geo_native=False,
            # Postgres synonym dictionaries need files in $SHAREDIR/tsearch_data
            # on the DB server. A library cannot put them there and must not
            # pretend it can, so equivalents are applied as QUERY expansion.
            synonyms_native=False,
            suggest=True,
            # True since 0.4.0. The old `False` said "query-side expansion
            # cannot make a phrase match through a synonym", and that was
            # true of the rendering, not of the engine: a multi-word member
            # spliced into a `to_tsquery` string is a syntax error, so the
            # only thing that could be done with it was to declare it lost.
            # `_tsquery_expression` renders it with `phraseto_tsquery` and
            # OR-s it into the group, which IS the adjacency the capability
            # names. This is also what takes the standing yellow «Синонимы
            # не подставлялись» off every SERP: the shortfall stopped being
            # real, so it stopped being reported.
            phrase_synonyms=True,
            supported_scorers=frozenset(
                {"relevance", "freshness_decay", "geo_decay", "promotion_boost", "popularity"}
            ),
            max_facet_fields=int(search_settings.MAX_FACET_FIELDS),
            max_result_window=int(search_settings.MAX_RESULT_WINDOW),
        )

    def health(self) -> BackendHealth:
        if connection.vendor != "postgresql":
            return BackendHealth(
                name=self.name, reachable=False, detail=f"vendor is {connection.vendor!r}"
            )
        from ..models import SearchDocument

        return BackendHealth(
            name=self.name,
            reachable=True,
            detail="pg_trgm present" if self.has_trigram() else "pg_trgm MISSING",
            documents=SearchDocument.objects.count(),
        )

    # -- write side ---------------------------------------------------------

    def upsert(self, docs: list[IndexDocument]) -> None:
        """Maintain the Postgres-only index structures for these rows.

        The ORM row was already written by the indexer inside the same
        transaction; this fills the three columns Django does not model.
        One statement per batch, values passed as parameters — a document
        title is user input and has no business near string interpolation.
        """
        if not docs:
            return
        self._require_postgres()
        from ..conf import search_settings

        configs = dict(search_settings.FTS_CONFIGS or {})
        fallback = search_settings.FTS_FALLBACK_CONFIG or "simple"

        rows = []
        for doc in docs:
            rows.append(
                (
                    doc.doc_type,
                    doc.doc_key,
                    configs.get(doc.language, fallback),
                    doc.title or "",
                    doc.text_extra or "",
                    doc.body or "",
                    list(doc.facet_terms or ()),
                    [str(p) for p in (doc.category_path or ())],
                )
            )

        sql = f"""
            UPDATE {_TABLE} AS d SET
                text_vec = setweight(to_tsvector(v.cfg::regconfig, v.title), 'A')
                        || setweight(to_tsvector(v.cfg::regconfig, v.extra), 'B')
                        || setweight(to_tsvector(v.cfg::regconfig, v.body), 'C'),
                facet_terms_arr = v.terms,
                category_path_arr = v.path
            FROM (VALUES %s) AS v(doc_type, doc_key, cfg, title, extra, body, terms, path)
            WHERE d.doc_type = v.doc_type AND d.doc_key = v.doc_key
        """
        placeholder = "(%s, %s, %s, %s, %s, %s, %s::text[], %s::text[])"
        values_sql = ", ".join([placeholder] * len(rows))
        params: list = []
        for row in rows:
            params.extend(row)
        with connection.cursor() as cursor:
            cursor.execute(sql % values_sql, params)

    def delete(self, doc_type: str, keys: list[str]) -> None:
        """No-op: removing the ORM row removes the index entry with it."""
        return None

    def clear(self, doc_type: str | None = None) -> None:
        """No-op: the indexer owns the rows; truncating is its job."""
        return None

    def apply_settings(self, doc_type: str, settings: IndexSettings) -> None:
        """No-op: the schema IS the settings, and it is a migration.

        Dictionaries are applied query-side (see ``capabilities()``), so
        there is nothing to push to the engine — which is exactly the
        difference ``synonyms_native=False`` declares.
        """
        return None

    # -- SQL construction ---------------------------------------------------

    def _text_predicate(self, q: SearchQuery, *, trigram: bool) -> tuple[str, list]:
        """The stemmed arm, and the trigram arm when the fallback is on.

        Arm 1 is ``to_tsquery`` over the engine's own morphology: groups are
        AND-ed, a group's curated/transliterated expansions are OR-ed. Arm 2
        runs only when arm 1 came back with fewer hits than
        ``TYPO_FALLBACK_THRESHOLD``, which is what keeps a well-spelled
        query from being widened by fuzz it never asked for.
        """
        from ..conf import search_settings

        if q.text is None or q.text.is_empty:
            return "", []
        config = (search_settings.FTS_CONFIGS or {}).get(
            q.language, search_settings.FTS_FALLBACK_CONFIG or "simple"
        )
        params: list = []
        expression = _tsquery_expression(q.text.terms, config, params)
        if not expression:
            return "", []
        clause = f"d.text_vec @@ ({expression})"
        if trigram and self.has_trigram():
            # The fuzzy arm keeps the AND semantics of the exact one: every
            # term group must still match SOMETHING, it may just match it
            # approximately. Running similarity over the whole query string
            # instead would quietly turn "samsung lenovo" into "either", and
            # a search that widens the meaning of a query when it finds too
            # little is a search that answers a different question.
            threshold = float(search_settings.TRIGRAM_SIMILARITY)
            per_group = []
            for group in q.text.terms:
                alternatives = []
                for term in group:
                    alternatives.append("word_similarity(%s, d.text_plain) >= %s")
                    params.append(term)
                    params.append(threshold)
                if alternatives:
                    per_group.append("(" + " OR ".join(alternatives) + ")")
            if per_group:
                clause = "(" + clause + " OR (" + " AND ".join(per_group) + "))"
        return clause, params

    def _rank_expression(self, q: SearchQuery) -> tuple[str, list]:
        from ..conf import search_settings

        if q.text is None or q.text.is_empty:
            return "0.0", []
        config = (search_settings.FTS_CONFIGS or {}).get(
            q.language, search_settings.FTS_FALLBACK_CONFIG or "simple"
        )
        params: list = []
        expression = _tsquery_expression(q.text.terms, config, params)
        if not expression:
            return "0.0", []
        return f"ts_rank_cd(d.text_vec, ({expression}))", params

    @staticmethod
    def _column(axis: str, q: SearchQuery) -> str:
        """``d.lat``/``d.lon`` as this reader is allowed to read it.

        For an anonymous reader the column is snapped onto the public grid IN
        SQL — ``floor(v * 10**p + 0.5) / 10**p``, the identical arithmetic
        ``_shared.snap_to_grid`` performs in Python, ties toward +infinity in
        both, because two engines disagreeing about which cell a listing sits
        in is precisely the seam defect the conformance suite exists for.

        No index is lost by it: the only predicates that read these columns
        through a snap are haversines, which were never indexable, and the
        indexable lat/lon range in ``_where`` still reads the raw column
        (widened by ``_shared.grid_slack_km``, so it stays a superset).
        """
        raw = f"d.{axis}::double precision"
        if shared.is_precise(getattr(q, "audience", None)):
            return raw
        scale = 10 ** shared.public_precision()
        return f"(floor(d.{axis}::double precision * {scale} + 0.5) / {scale})"

    def _distance_expression(self, q: SearchQuery) -> tuple[str, list]:
        """The distance as the ANSWER states it: floored to the grid quantum.

        This is the expression projected as ``distance_km``, so it is also
        what ``ORDER BY``, the keyset predicate and therefore the cursor
        carry — an anchor finer than the order it resumes would page
        unstably, and an anchor finer than the card is a leak wearing base64.
        The MEASUREMENT that decides membership and scoring is
        :meth:`_measured_distance`.
        """
        expr, params = self._measured_distance(q)
        if shared.is_precise(getattr(q, "audience", None)) or expr.startswith("NULL"):
            return expr, params
        quantum = shared.distance_quantum_km(shared.public_precision())
        if quantum <= 0:  # a precision no grid can express; nothing to floor to
            return expr, params
        return f"(floor(({expr}) / {quantum!r}) * {quantum!r})", params

    def _measured_distance(self, q: SearchQuery) -> tuple[str, list]:
        """Great-circle distance in km, or ``NULL`` when no centre was given."""
        return self._distance_to(q.geo, q)

    @staticmethod
    def _distance_to(geo, q: SearchQuery) -> tuple[str, list]:
        """The same haversine against any centre — the filter's, or the band's."""
        if geo is None or not geo.has_center:
            return "NULL::double precision", []
        lat = PostgresSearchBackend._column("lat", q)
        lon = PostgresSearchBackend._column("lon", q)
        expr = (
            f"(2 * {_EARTH_KM} * asin(sqrt("
            f"power(sin(radians({lat} - %s) / 2), 2)"
            f" + cos(radians(%s)) * cos(radians({lat}))"
            f" * power(sin(radians({lon} - %s) / 2), 2))))"
        )
        return expr, [geo.lat, geo.lat, geo.lon]

    def _near_predicate(self, q: SearchQuery) -> tuple[str, list]:
        """``TRUE`` for a row inside the ``nearby`` band — a WHERE clause, not
        a projection, and that distinction is the whole performance story.

        Sorting the whole table by a band EXPRESSION costs a full evaluation
        before anything can be ordered (measured at ~650ms on a 1M-row
        corpus, against ~1ms for the same page as two banded queries): no
        index can produce band-ordered rows, but each band on its own is an
        ordinary, indexable predicate over the active sort's index. So the
        band never appears in ``ORDER BY``; it selects WHICH query runs.

        Two halves, and both are needed. The covering cell set is the
        INDEXED half: each cell is one ``d.geohash LIKE 'cell%'``, which
        ``search_document_geohash_..._like`` (varchar_pattern_ops) serves as
        a range scan, and their union provably contains the whole radius
        box. The haversine is the EXACT half, so a card labelled «поблизости»
        is never 30km away — a cell is ~39x19.5km and its corner is not
        inside a 25km disc.

        A row with no geohash is `unknown`, not `elsewhere` (the same trap
        ``_where``'s prefilter documents: ``geohash`` is ``default=""`` and a
        source may fill lat/lon without it), so the coarse half admits
        ``d.geohash = ''`` and lets the haversine decide.
        """
        near = q.near
        if near is None or not near.has_center:
            return "", []
        from ..conf import search_settings

        radius = shared.near_radius_km(near)
        params: list = []
        cells = shared.geohash_cells(
            near.lat,
            near.lon,
            radius,
            precision=int(search_settings.NEAR_BAND_CELL_PRECISION),
            max_cells=int(search_settings.NEAR_BAND_MAX_CELLS),
        )
        if cells:
            likes = " OR ".join(["d.geohash LIKE %s"] * len(cells))
            coarse = f"(d.geohash = '' OR {likes})"
            params.extend(cell + "%" for cell in cells)
        else:
            # Above NEAR_BAND_MAX_CELLS the OR-of-prefixes stops paying for
            # itself; the bounding box is the coarser gate for the same disc.
            min_lat, min_lon, max_lat, max_lon = shared.radius_bbox(
                near.lat, near.lon, radius
            )
            lon_clause = (
                "(d.lon >= %s OR d.lon <= %s)"
                if min_lon > max_lon
                else "d.lon BETWEEN %s AND %s"
            )
            coarse = f"(d.lat BETWEEN %s AND %s AND {lon_clause})"
            params.extend([min_lat, max_lat, min_lon, max_lon])
        distance_sql, distance_params = self._distance_to(near, q)
        params.extend(distance_params)
        params.append(radius)
        return f"({coarse} AND ({distance_sql}) <= %s)", params

    def _band_clause(self, q: SearchQuery, band: str) -> tuple[str, list]:
        """The WHERE fragment selecting one band. The ``all`` half is a
        negation that must survive NULL.

        ``NOT (<nearby>)`` is NOT the ``all`` band. One row shape makes the
        predicate indeterminate rather than false: a geohash INSIDE the cell
        cover with ``lat``/``lon`` NULL. The coarse half is then ``TRUE``,
        the haversine is ``NULL``, ``TRUE AND NULL`` is ``NULL``, and ``NOT
        NULL`` is ``NULL`` — so the row matches neither band and vanishes
        from the answer with nothing saying so. (A row with no location at
        all is safe on its own: ``geohash`` is ``NOT NULL DEFAULT ''`` and
        ``'' LIKE 'ucfv%'`` is FALSE, so the negation is TRUE.)

        The shape is not hypothetical. Geohash and coordinates are
        maintained on separate paths — ``Listing.compute_geohash_draft``
        deliberately clears the geohash when the geo service is unreachable,
        "unknown beats wrong" — so a stale geohash outliving cleared
        coordinates produces exactly this row.

        ``COALESCE(<near>, false)`` collapses the unknown into "not nearby"
        before negating, which is the label :func:`_shared.band_of` gives the
        same row. The invariant it buys is machine-checkable and is asserted
        as such: ``count(near) + count(far) == count(unbanded)``, for any
        centre.
        """
        near_sql, params = self._near_predicate(q)
        if not near_sql:
            return "", []
        if band == "nearby":
            return f"COALESCE({near_sql}, false)", params
        return f"NOT COALESCE({near_sql}, false)", params

    @staticmethod
    def _match_count_expression(q: SearchQuery) -> tuple[str, list]:
        """How many of ``q.signals`` this row satisfies, over ``facet_terms_arr``.

        The COUNTING structure answers it, exactly as it answers a facet
        count: one set intersection over the materialized term array, never
        a per-row walk of ``facets`` jsonb in Python. Both sides are sets, so
        a repeated signal counts once — the query is about one thing, not
        two.
        """
        if not q.signals:
            return "0", []
        terms = sorted({f"{slug}={value}" for slug, value in q.signals})
        expression = (
            "cardinality(ARRAY(SELECT unnest(d.facet_terms_arr) "
            "INTERSECT SELECT unnest(%s::text[])))"
        )
        return expression, [terms]

    def _score_expression(self, q: SearchQuery) -> tuple[str, list]:
        """``sum(weight_i * f_i)`` in SQL, over the scorers active for the sort.

        The registry decides membership; ``promotion_boost`` carries only
        ``relevance`` in ``applies_to_sorts``, so an explicit sort cannot
        receive a boost — the invariant is structural here too, not a second
        implementation of the same rule.
        """
        from ..registry import get_scorers

        rank_sql, rank_params = self._rank_expression(q)
        distance_sql, distance_params = self._measured_distance(q)

        parts: list[str] = []
        params: list = []
        for scorer in get_scorers().values():
            if q.sort not in scorer.applies_to_sorts:
                continue
            weight = float(scorer.weight)
            if scorer.slug == "relevance":
                parts.append(f"({weight} * {rank_sql})")
                params.extend(rank_params)
            elif scorer.slug == "promotion_boost":
                parts.append(f"({weight} * LEAST(5.0, GREATEST(-1.0, d.boost)))")
            elif scorer.slug == "popularity":
                parts.append(
                    f"({weight} * (GREATEST(0, d.popularity)::double precision "
                    "/ (GREATEST(0, d.popularity) + 10.0)))"
                )
            elif scorer.slug == "freshness_decay":
                half_life = float(scorer.params.get("half_life_days") or 14)
                parts.append(
                    f"({weight} * COALESCE(power(0.5, GREATEST(0, EXTRACT(EPOCH FROM "
                    f"(now() - d.published_at)) / 86400.0) / {half_life}), 0.0))"
                )
            elif scorer.slug == "geo_decay":
                max_radius = float(scorer.params.get("max_radius_km") or 50)
                parts.append(
                    f"({weight} * COALESCE(GREATEST(0.0, 1.0 - ({distance_sql}) "
                    f"/ {max_radius}), 0.0))"
                )
                params.extend(distance_params)
        if not parts:
            return "0.0", []
        return " + ".join(parts), params

    def _where(self, q: SearchQuery, *, trigram: bool, skip_facet: str | None = None):
        """Every predicate of the query, as SQL plus parameters.

        Named as one function on purpose: this is the single place that
        decides what "a candidate" means, and the facet counter calls it
        with ``skip_facet`` to get the drill-down candidate set (the counted
        slug's own filter removed) rather than re-deriving the rule.
        """
        clauses = ["d.doc_type = %s", "d.visible"]
        params: list = [q.doc_type]

        if q.language_filter:
            clauses.append("d.language = %s")
            params.append(q.language_filter)
        if q.owner_key:
            clauses.append("d.owner_key = %s")
            params.append(q.owner_key)
        if q.category_path:
            depth = len(q.category_path)
            clauses.append(f"d.category_path_arr[1:{depth}] = %s::text[]")
            params.append([str(p) for p in q.category_path])
        for slug, values in (q.facets or {}).items():
            if slug == skip_facet:
                continue
            clauses.append("d.facet_terms_arr && %s::text[]")
            params.append(shared.facet_terms_for(slug, values))
        core_ranges, attribute_ranges = shared.split_ranges(q.ranges)
        for field, spec in core_ranges:
            # A column comparison, not the side-table semi-join: the number
            # is on the document. NULL fails both comparisons, which is the
            # wanted meaning — an unpriced listing is not a cheap one.
            if spec.lower is not None:
                clauses.append(f"d.{field} >= %s")
                params.append(spec.lower)
            if spec.upper is not None:
                clauses.append(f"d.{field} <= %s")
                params.append(spec.upper)
        for spec in attribute_ranges:
            sub = ["n.document_id = d.id", "n.slug = %s"]
            params.append(spec.slug)
            if spec.lower is not None:
                sub.append("n.value >= %s")
                params.append(spec.lower)
            if spec.upper is not None:
                sub.append("n.value <= %s")
                params.append(spec.upper)
            clauses.append(
                "EXISTS (SELECT 1 FROM search_number n WHERE " + " AND ".join(sub) + ")"
            )

        geo = q.geo
        if geo is not None:
            # A coarse prefilter reads the STORED column while the exact half
            # measures the reader's snapped position, so it is widened by the
            # furthest snapping can move a point. Zero for a precise reader,
            # and zero for a rectangle, which `grid_aligned_bbox` has already
            # made a union of whole cells.
            slack = shared.query_slack_km(q)
            prefix = shared.geohash_prefix(geo, slack_km=slack)
            if prefix:
                # Indexed coarse prefilter. A geohash cell is a lat/lon
                # rectangle, so a common prefix of the box's four corners is
                # a cell that provably contains the whole box — no border
                # document can fall out of the candidate set.
                #
                # A document with NO geohash is `unknown`, not `elsewhere`.
                # `geohash` is `blank=True, default=""` on SearchDocument, and
                # a source is free to fill lat/lon and leave it empty — which
                # is exactly what a host does when nothing has stamped the
                # column yet. Plain `LIKE 'ucf%'` then excludes every such
                # document, and because this clause sits in front of an EXACT
                # lat/lon box test that would have answered correctly, the
                # failure is silent and inverted: the SMALLER the radius, the
                # tighter the box, the longer the common prefix, the fewer
                # results — down to zero for a city-sized search over a corpus
                # whose coordinates are all present and correct. A widened
                # search then "fixes" it, which is the opposite of how anyone
                # debugs a search.
                #
                # The prefilter is an OPTIMISATION over the box below it. An
                # optimisation that removes correct answers is a defect, so it
                # only ever narrows documents that carry the column it reads.
                clauses.append("(d.geohash = '' OR d.geohash LIKE %s)")
                params.append(prefix + "%")
            box = shared.bbox_of(geo, slack_km=slack)
            if box is not None:
                min_lat, min_lon, max_lat, max_lon = box
                clauses.append("d.lat IS NOT NULL AND d.lon IS NOT NULL")
                clauses.append("d.lat BETWEEN %s AND %s")
                params.extend([min_lat, max_lat])
                if min_lon > max_lon:
                    clauses.append("(d.lon >= %s OR d.lon <= %s)")
                    params.extend([min_lon, max_lon])
                else:
                    clauses.append("d.lon BETWEEN %s AND %s")
                    params.extend([min_lon, max_lon])
            if geo.is_bbox and not shared.is_precise(getattr(q, "audience", None)):
                # The indexed range above is a SUPERSET: it compares the
                # stored column, and a point exactly on a cell boundary is
                # inside a box whose cells all exclude it — one row's worth of
                # sub-cell information, which is one row's worth too much.
                # The exact half asks the same question of the snapped point,
                # which is the only position this reader may resolve.
                lat_sql, lon_sql = self._column("lat", q), self._column("lon", q)
                clauses.append(f"{lat_sql} BETWEEN %s AND %s")
                params.extend([geo.min_lat, geo.max_lat])
                if geo.crosses_antimeridian:
                    clauses.append(f"({lon_sql} >= %s OR {lon_sql} <= %s)")
                else:
                    clauses.append(f"{lon_sql} BETWEEN %s AND %s")
                params.extend([geo.min_lon, geo.max_lon])
            if geo.has_center and geo.radius_km is not None:
                distance_sql, distance_params = self._measured_distance(q)
                clauses.append(f"({distance_sql}) <= %s")
                params.extend(distance_params)
                params.append(float(geo.radius_km))

        text_sql, text_params = self._text_predicate(q, trigram=trigram)
        if text_sql:
            clauses.append(text_sql)
            params.extend(text_params)

        return " AND ".join(clauses), params

    #: Sort name -> the SELECT alias its ordering key is projected under.
    #: Ordering and the keyset predicate both reference the ALIAS, never the
    #: expression: a scored expression carries parameters, and repeating it
    #: three times in one statement means repeating its parameters three
    #: times in the right order — a placeholder-counting exercise that is
    #: wrong the first time somebody adds a scorer. The alias appears once.
    _SORT_ALIAS = {
        "relevance": "score",
        "newest": "published_at",
        "price_asc": "price_base",
        "price_desc": "price_base",
        "distance": "distance_km",
    }

    _DESCENDING_SORTS = frozenset({"relevance", "newest", "price_desc"})

    def _order_by(self, q: SearchQuery, *, anchored: bool) -> tuple[str, str, list]:
        """``(order_sql, keyset_sql, keyset_params)`` INSIDE one band.

        NULLs sort last in BOTH directions — "no price" is neither the
        cheapest nor the dearest. The tie-break is always ``doc_key``
        ascending: without a total order the cursor is not stable and a user
        paging a feed sees the same row twice.

        The band is deliberately absent from this ``ORDER BY``. Bands are
        executed as two ordered queries concatenated (see
        :meth:`_near_predicate`), so each band's page is an ordinary keyset
        read the sort's own index can serve. ``anchored`` is False when a
        page has already crossed into this band and must start at its
        beginning rather than at the cursor, which belongs to the band
        behind it.
        """
        alias = self._SORT_ALIAS.get(q.sort, "published_at")
        descending = q.sort in self._DESCENDING_SORTS
        direction = "DESC" if descending else "ASC"
        _, matches_lead = shared.leading_keys(q)

        leading = ["t.match_count DESC"] if matches_lead else []
        order_sql = ", ".join(
            leading + [f"t.{alias} {direction} NULLS LAST", "t.doc_key ASC"]
        )

        if q.cursor is None or not anchored:
            return order_sql, "", []

        _, matches, value = shared.split_sort_value(q.cursor.sort_value)
        if value is None:
            # Past the NULL frontier already: only NULLs remain, ordered by key.
            tail_sql = f"(t.{alias} IS NULL AND t.doc_key > %s)"
            tail_params = [q.cursor.doc_key]
        else:
            comparator = "<" if descending else ">"
            cast = self._cursor_value(q.sort, value)
            tail_sql = (
                f"((t.{alias} {comparator} %s) OR (t.{alias} = %s AND t.doc_key > %s) "
                f"OR t.{alias} IS NULL)"
            )
            tail_params = [cast, cast, q.cursor.doc_key]

        if not matches_lead or matches is None:
            return order_sql, tail_sql, tail_params

        # Lexicographic "strictly after the anchor" over (match_count DESC,
        # sort, doc_key), written out rather than as a ROW() comparison: the
        # sort column is NULLS LAST and a row comparison cannot say that.
        keyset_sql = (
            "((t.match_count < %s) OR (t.match_count = %s AND " + tail_sql + "))"
        )
        return order_sql, keyset_sql, [matches, matches] + tail_params

    @staticmethod
    def _cursor_value(sort: str, value):
        if sort in ("price_asc", "price_desc"):
            return Decimal(str(value))
        if sort == "newest":
            from django.utils.dateparse import parse_datetime

            return parse_datetime(value)
        return float(value)

    # -- read side ----------------------------------------------------------

    def _run(self, q: SearchQuery, *, trigram: bool) -> tuple[list[Hit], bool]:
        """One page. With banding on, two ordered reads concatenated.

        ``nearby`` and ``all`` are separate, individually indexable queries;
        a page is ``nearby``'s rows followed by ``all``'s, and a page that
        runs ``nearby`` dry simply fills its remainder from ``all``. The reader sees one ``items`` list and one cursor,
        so the boundary is invisible from outside — which is the point: a
        band is a heading, and a heading does not end a page.
        """
        band_leads, _ = shared.leading_keys(q)
        if not band_leads:
            rows = self._read_band(q, band="", trigram=trigram, limit=q.limit + 1)
            return self._hits(q, rows[: q.limit], band=""), len(rows) > q.limit

        resume, _, _ = shared.split_sort_value(q.cursor.sort_value) if q.cursor else (0, None, None)
        hits: list[Hit] = []
        if (resume or 0) == 0:
            near = self._read_band(
                q, band="nearby", trigram=trigram, limit=q.limit + 1, anchored=True
            )
            hits.extend(self._hits(q, near, band="nearby"))
            if len(hits) > q.limit:
                return hits[: q.limit], True
            # ``nearby`` is exhausted: the rest of this page comes from
            # ``all``, read from ITS beginning — the cursor that got us
            # here anchors the band behind us and means nothing here.
            far = self._read_band(
                q, band="all", trigram=trigram, limit=q.limit + 1 - len(hits), anchored=False
            )
        else:
            far = self._read_band(
                q, band="all", trigram=trigram, limit=q.limit + 1, anchored=True
            )
        hits.extend(self._hits(q, far, band="all"))
        return hits[: q.limit], len(hits) > q.limit

    def _read_band(
        self,
        q: SearchQuery,
        *,
        band: str,
        trigram: bool,
        limit: int,
        anchored: bool = True,
    ) -> list[tuple]:
        """Raw rows of one band, ordered, keyset-anchored, capped at *limit*."""
        if limit <= 0:
            return []
        where_sql, where_params = self._where(q, trigram=trigram)
        score_sql, score_params = self._score_expression(q)
        distance_sql, distance_params = self._distance_expression(q)
        match_sql, match_params = self._match_count_expression(q)
        band_sql, band_params = self._band_clause(q, band) if band else ("", [])
        order_sql, keyset_sql, keyset_params = self._order_by(q, anchored=anchored)

        # Parameter order follows the textual order of the statement, and the
        # inner SELECT comes first. Each parameterized expression is written
        # exactly once, which is the point of the subquery.
        params = list(score_params) + list(distance_params) + list(match_params)
        params += list(where_params) + list(band_params)
        params += list(keyset_params)
        params.append(limit)

        inner_where = where_sql + (f" AND ({band_sql})" if band_sql else "")
        outer_where = f"WHERE {keyset_sql}" if keyset_sql else ""
        sql = f"""
            SELECT t.doc_key, t.score, t.distance_km, t.published_at, t.price_base,
                   t.match_count
              FROM (
                SELECT d.doc_key,
                       ({score_sql}) AS score,
                       ({distance_sql}) AS distance_km,
                       d.published_at, d.price_base,
                       ({match_sql}) AS match_count
                  FROM {_TABLE} d
                 WHERE {inner_where}
              ) t
             {outer_where}
             ORDER BY {order_sql}
             LIMIT %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _hits(self, q: SearchQuery, rows, *, band: str) -> list[Hit]:
        composite = any(shared.leading_keys(q))
        hits = []
        for doc_key, score, distance, published_at, price_base, matches in rows:
            if q.sort == "relevance":
                sort_value = round(float(score or 0.0), 6)
            elif q.sort == "newest":
                sort_value = published_at.isoformat() if published_at else None
            elif q.sort in ("price_asc", "price_desc"):
                sort_value = None if price_base is None else str(price_base)
            elif q.sort == "distance":
                sort_value = None if distance is None else round(float(distance), 6)
            else:  # pragma: no cover
                sort_value = None
            matches = int(matches or 0)
            if composite:
                sort_value = shared.banded_sort_value(band, matches, sort_value)
            hits.append(
                Hit(
                    key=doc_key,
                    score=round(float(score or 0.0), 6),
                    # Already on the reader's own grid: `_distance_expression`
                    # floors it in SQL, so ORDER BY, the keyset and this value
                    # are the same number. The old `round(..., 2)` here was
                    # ten METRES — three orders of magnitude finer than the
                    # position it was computed from.
                    distance_km=None if distance is None else round(float(distance), 6),
                    sort_value=sort_value,
                    band=band,
                    match_count=matches,
                )
            )
        return hits

    def query(self, q: SearchQuery) -> QueryResult:
        self._require_postgres()
        from ..conf import search_settings

        hits, has_next = self._run(q, trigram=False)
        degraded: list[str] = []
        trigram = False
        if (
            q.text is not None
            and not q.text.is_empty
            and len(hits) < int(search_settings.TYPO_FALLBACK_THRESHOLD)
        ):
            if self.has_trigram():
                hits, has_next = self._run(q, trigram=True)
                trigram = True
            else:
                degraded.append("typo_tolerance")
        # `phrase_synonyms` is NOT reported here. It is derivable from
        # `capabilities()` alone, and `services._degradations` already
        # derives it — from the same condition, so the answer carried it
        # twice on every query with text. One owner, and the one that can
        # see whether this particular query had a multi-word member to lose.

        # Counted over the SAME arm the hits came from. Counting the exact
        # arm behind a fuzzy page is how `count: 0` ends up printed over
        # four visible cards: the typo fallback answers from
        # `word_similarity`, and the tsquery that found nothing is not the
        # question this page answered.
        total, lower_bound = self._candidate_count(q, trigram=trigram)
        return QueryResult(
            hits=tuple(hits),
            total=total,
            exact_total=not lower_bound,
            total_is_lower_bound=lower_bound,
            has_next=has_next,
            has_prev=q.cursor is not None,
            degraded=tuple(dict.fromkeys(degraded)),
            bands=self._band_counts(q, trigram=trigram),
        )

    def _band_counts(self, q: SearchQuery, *, trigram: bool) -> tuple[BandSummary, ...]:
        """Per-band counts: two capped ``count(*)``s over the band predicates.

        The same predicates the two banded reads use, so the number over a
        heading is the number of rows scrolling under it — and capped
        exactly as :meth:`_candidate_count` is capped, because counting the
        whole corpus to label two headings is the cost this backend exists
        to avoid. At the cap the count is a floor and says so: a capped
        count presented as a count is a wrong number with a confident face,
        and per band that is worse than in the total.
        """
        band_leads, _ = shared.leading_keys(q)
        if not band_leads:
            return ()
        from ..conf import search_settings

        cap = int(search_settings.FACET_CANDIDATE_CAP)
        where_sql, where_params = self._where(q, trigram=trigram)
        counts = {}
        for band in ("nearby", "all"):
            band_sql, band_params = self._band_clause(q, band)
            sql = (
                f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} d "
                f"WHERE {where_sql} AND ({band_sql}) LIMIT %s) s"
            )
            with connection.cursor() as cursor:
                cursor.execute(sql, list(where_params) + list(band_params) + [cap + 1])
                counts[band] = int(cursor.fetchone()[0])
        return (
            BandSummary(
                id="nearby",
                count=min(counts["nearby"], cap),
                count_is_lower_bound=counts["nearby"] > cap,
                radius_km=shared.near_radius_km(q.near),
            ),
            BandSummary(
                id="all",
                count=min(counts["all"], cap),
                count_is_lower_bound=counts["all"] > cap,
            ),
        )

    def _candidate_count(self, q: SearchQuery, *, trigram: bool) -> tuple[int, bool]:
        """``(count, is_lower_bound)`` for the candidate set.

        Counting the whole candidate set on every page is the cost this
        backend exists to avoid, so the count stops at the cap + 1. Below
        the cap the number is a real ``count(*)`` and is exact; at the cap
        it is a floor — "at least cap+1" — and says so, because a capped
        count reported as a count is a wrong number with a confident face.
        """
        from ..conf import search_settings

        cap = int(search_settings.FACET_CANDIDATE_CAP)
        where_sql, where_params = self._where(q, trigram=trigram)
        sql = f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} d WHERE {where_sql} LIMIT %s) s"
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [cap + 1])
            count = int(cursor.fetchone()[0])
        if count > cap:
            return cap + 1, True
        return count, False

    def facets(self, q: SearchQuery, plan: FacetPlan) -> FacetResult:
        """Remaining-option counts, one candidate set per counted slug.

        Drill-down semantics mean N active facet filters cost N+1 candidate
        sets — an honest consequence, which is why ``MAX_FACET_FIELDS`` is a
        closed switch rather than advice. Above ``FACET_CANDIDATE_CAP`` the
        counts come from ``TABLESAMPLE SYSTEM`` and are scaled, and the
        answer carries ``approximate: true``: an empty panel is worse than
        an approximate one (verdict §18.5).
        """
        self._require_postgres()
        from ..conf import search_settings

        cap = int(search_settings.FACET_CANDIDATE_CAP)
        counts: dict[str, dict[str, int]] = {}
        approximate = False
        max_candidates = 0

        for slug in plan.slugs:
            drilled = q.without_facet(slug)
            where_sql, where_params = self._where(drilled, trigram=False, skip_facet=slug)
            candidates = self._count_candidates(where_sql, where_params, cap)
            max_candidates = max(max_candidates, candidates)
            if candidates > cap:
                approximate = True
                counts[slug] = self._sampled_counts(slug, where_sql, where_params, cap)
            else:
                counts[slug] = self._exact_counts(slug, where_sql, where_params)

        return FacetResult(
            counts=counts,
            approximate=approximate,
            candidates=max_candidates,
            degraded=("exact_facet_counts",) if approximate else (),
        )

    def category_counts(self, q: SearchQuery, *, limit: int) -> list[tuple[tuple[str, ...], int]]:
        """OPTIONAL verb: which categories *q*'s candidate set is made of.

        One ``GROUP BY category_path_arr`` over :meth:`_where` — the same
        single place that decides what "a candidate" means, and the same
        ``trigram=False`` arm :meth:`facets` counts through, so the plan is
        drawn from exactly the set that will be counted.

        Reuses :meth:`_category_groups`, which 0.10.4 already wrote for
        ``suggest_categories``. The difference is the input: that verb
        answers a bare TEXT query for the type-ahead and applies its own
        typo-widening ladder, while this one takes the whole
        :class:`SearchQuery` — category filter, facet filters, ranges, geo
        box — because it is describing a page rather than nominating one.
        """
        self._require_postgres()
        return self._category_groups(q, trigram=False, limit=limit)

    @staticmethod
    def _count_candidates(where_sql: str, where_params: list, cap: int) -> int:
        sql = (
            f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} d WHERE {where_sql} LIMIT %s) s"
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [cap + 1])
            return int(cursor.fetchone()[0])

    @staticmethod
    def _exact_counts(slug: str, where_sql: str, where_params: list) -> dict[str, int]:
        sql = f"""
            WITH cand AS (
                SELECT d.facet_terms_arr AS terms FROM {_TABLE} d WHERE {where_sql}
            )
            SELECT t.term, count(*) AS n
              FROM cand, unnest(cand.terms) AS t(term)
             WHERE t.term LIKE %s
             GROUP BY t.term
             ORDER BY n DESC, t.term ASC
             LIMIT 200
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [f"{slug}=%"])
            rows = cursor.fetchall()
        offset = len(slug) + 1
        return {term[offset:]: int(n) for term, n in rows}

    @staticmethod
    def _sampled_counts(
        slug: str, where_sql: str, where_params: list, cap: int
    ) -> dict[str, int]:
        """Scaled ``TABLESAMPLE SYSTEM`` counts for an over-cap candidate set.

        The sample percentage is derived from the corpus size so that the
        expected sampled candidate count lands near the cap. Numbers are
        scaled back up and rounded; the caller has already been told they
        are approximate, so a rounded estimate is a stated estimate, not a
        number pretending to be exact.
        """
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {_TABLE}")
            corpus = max(1, int(cursor.fetchone()[0]))
        percent = max(0.1, min(100.0, 100.0 * cap / corpus))
        scale = 100.0 / percent
        sql = f"""
            WITH cand AS (
                SELECT d.facet_terms_arr AS terms
                  FROM {_TABLE} d TABLESAMPLE SYSTEM ({percent})
                 WHERE {where_sql}
            )
            SELECT t.term, count(*) AS n
              FROM cand, unnest(cand.terms) AS t(term)
             WHERE t.term LIKE %s
             GROUP BY t.term
             ORDER BY n DESC, t.term ASC
             LIMIT 200
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [f"{slug}=%"])
            rows = cursor.fetchall()
        offset = len(slug) + 1
        return {term[offset:]: int(round(int(n) * scale)) for term, n in rows}

    def suggest(
        self, doc_type: str, prefix: str, *, limit: int, scope: SearchQuery | None = None
    ) -> list[str]:
        """Title prefixes out of the index — never out of a query log.

        No query log is kept (spec §15): it is a privacy decision before it
        is a product one, and on day one there would be nothing in it.
        """
        self._require_postgres()
        from ..text import fold

        folded = fold(prefix or "").strip()
        if not folded:
            return []
        clauses = ["d.doc_type = %s", "d.visible", "d.text_plain LIKE %s"]
        params: list = [doc_type, folded + "%"]
        if scope is not None and scope.language_filter:
            clauses.append("d.language = %s")
            params.append(scope.language_filter)
        sql = f"""
            SELECT DISTINCT d.title FROM {_TABLE} d
             WHERE {' AND '.join(clauses)}
             ORDER BY d.title
             LIMIT %s
        """
        params.append(limit)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [row[0] for row in cursor.fetchall()]

    def suggest_categories(
        self, doc_type: str, query: str, *, language: str, limit: int
    ) -> list[tuple[tuple[str, ...], int]]:
        """OPTIONAL verb: the categories whose DOCUMENTS match *query*.

        The goods-driven half of the type-ahead (see ``suggest.py``): when
        no category NAME matches, ``(category path, matching document
        count)`` pairs from HERE become the suggestion rows. The candidate
        set is :meth:`_where` — the single place that decides what "a
        candidate" means — over the same normalized query the SERP runs, so
        the count on the row is the count ``?q=…&category=…`` will show
        (``test_a_goods_row_count_is_the_serp_count`` compares the two
        ends). One aggregate: ``GROUP BY category_path_arr`` over the
        matching set, busiest paths first, the array itself breaking ties
        so the answer is stable across plans.

        Two promises, reconciled (0.10.4; both conformance scenarios pin it):

        - WHICH categories may be offered is the STRICT predicate's answer
          alone. The trigram arm used to nominate destinations, and on a
          live stand a brand-word typo (one transposition off a real title)
          got an unrelated category as the confident top suggestion —
          ``goods_suggestions_do_not_guess``. A query the strict predicate
          cannot match yields no goods rows; the SERP's own typo tolerance
          still catches the buyer on Enter.
        - The COUNT on an offered row is the count the tap will show —
          ``suggest_categories`` (the scenario). The tap runs :meth:`query`
          with the category filter, and query() widens through the trigram
          arm below ``TYPO_FALLBACK_THRESHOLD``; so each strictly-nominated
          path is re-counted by exactly query()'s decision procedure for
          that (text, category) request: keep the strict count at or above
          the threshold, otherwise take the trigram arm's count for the
          path (one grouped aggregate for all such paths). A path whose
          faithful count lands on zero is dropped — a promise of zero goods
          is not a destination.
        """
        self._require_postgres()
        from ..conf import search_settings
        from ..text import normalize_query

        text = normalize_query(query, language)
        if text.is_empty:
            return []
        q = SearchQuery(doc_type=doc_type, language=language, text=text)
        strict = self._category_groups(q, trigram=False, limit=limit)
        if not strict:
            return []
        threshold = int(search_settings.TYPO_FALLBACK_THRESHOLD)
        needs_widened = [path for path, count in strict if count < threshold]
        if not needs_widened or not self.has_trigram():
            return strict
        widened = dict(self._category_groups(q, trigram=True, limit=max(limit, len(strict))))
        pairs = []
        for path, count in strict:
            faithful = widened.get(path, 0) if count < threshold else count
            if faithful > 0:
                pairs.append((path, faithful))
        pairs.sort(key=lambda pair: (-pair[1], pair[0]))
        return pairs

    def _category_groups(
        self, q: SearchQuery, *, trigram: bool, limit: int
    ) -> list[tuple[tuple[str, ...], int]]:
        where_sql, where_params = self._where(q, trigram=trigram)
        sql = f"""
            SELECT d.category_path_arr, count(*) AS n
              FROM {_TABLE} d
             WHERE {where_sql}
             GROUP BY d.category_path_arr
             ORDER BY n DESC, d.category_path_arr ASC
             LIMIT %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [limit])
            rows = cursor.fetchall()
        return [
            (tuple(str(segment) for segment in path), int(n))
            for path, n in rows
            if path
        ]


__all__ = ["READ_PATH_IMPL", "PostgresBackendUnavailable", "PostgresSearchBackend"]
