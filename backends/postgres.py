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
from decimal import Decimal

from django.db import connection

from ..dto import (
    BackendCapabilities,
    BackendHealth,
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
            # Query-side expansion cannot make a phrase match through a
            # synonym; declared rather than discovered.
            phrase_synonyms=False,
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
        groups = []
        for group in q.text.terms:
            safe = [t.replace("'", "").replace("\\", "") for t in group if t.strip()]
            safe = [t for t in safe if t]
            if safe:
                groups.append("(" + " | ".join(safe) + ")")
        if not groups:
            return "", []
        tsquery = " & ".join(groups)
        clause = "d.text_vec @@ to_tsquery(%s::regconfig, %s)"
        params: list = [config, tsquery]
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
        groups = []
        for group in q.text.terms:
            safe = [t.replace("'", "").replace("\\", "") for t in group if t.strip()]
            safe = [t for t in safe if t]
            if safe:
                groups.append("(" + " | ".join(safe) + ")")
        if not groups:
            return "0.0", []
        return "ts_rank_cd(d.text_vec, to_tsquery(%s::regconfig, %s))", [
            config,
            " & ".join(groups),
        ]

    def _distance_expression(self, q: SearchQuery) -> tuple[str, list]:
        """Great-circle distance in km, or ``NULL`` when no centre was given."""
        if q.geo is None or not q.geo.has_center:
            return "NULL::double precision", []
        expr = (
            f"(2 * {_EARTH_KM} * asin(sqrt("
            "power(sin(radians(d.lat::double precision - %s) / 2), 2)"
            " + cos(radians(%s)) * cos(radians(d.lat::double precision))"
            " * power(sin(radians(d.lon::double precision - %s) / 2), 2))))"
        )
        return expr, [q.geo.lat, q.geo.lat, q.geo.lon]

    def _score_expression(self, q: SearchQuery) -> tuple[str, list]:
        """``sum(weight_i * f_i)`` in SQL, over the scorers active for the sort.

        The registry decides membership; ``promotion_boost`` carries only
        ``relevance`` in ``applies_to_sorts``, so an explicit sort cannot
        receive a boost — the invariant is structural here too, not a second
        implementation of the same rule.
        """
        from ..registry import get_scorers

        rank_sql, rank_params = self._rank_expression(q)
        distance_sql, distance_params = self._distance_expression(q)

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
        for spec in q.ranges:
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
            prefix = shared.geohash_prefix(geo)
            if prefix:
                # Indexed coarse prefilter. A geohash cell is a lat/lon
                # rectangle, so a common prefix of the box's four corners is
                # a cell that provably contains the whole box — no border
                # document can fall out of the candidate set.
                clauses.append("d.geohash LIKE %s")
                params.append(prefix + "%")
            box = shared.bbox_of(geo)
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
            if geo.has_center and geo.radius_km is not None:
                distance_sql, distance_params = self._distance_expression(q)
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

    def _order_by(self, q: SearchQuery) -> tuple[str, str, list]:
        """``(order_sql, keyset_sql, keyset_params)``, over the SELECT aliases.

        NULLs sort last in BOTH directions — "no price" is neither the
        cheapest nor the dearest. The tie-break is always ``doc_key``
        ascending: without a total order the cursor is not stable and a user
        paging a feed sees the same row twice.
        """
        alias = self._SORT_ALIAS.get(q.sort, "published_at")
        descending = q.sort in self._DESCENDING_SORTS
        direction = "DESC" if descending else "ASC"
        order_sql = f"t.{alias} {direction} NULLS LAST, t.doc_key ASC"

        if q.cursor is None:
            return order_sql, "", []

        value = q.cursor.sort_value
        if value is None:
            # Past the NULL frontier already: only NULLs remain, ordered by key.
            return order_sql, f"(t.{alias} IS NULL AND t.doc_key > %s)", [q.cursor.doc_key]

        comparator = "<" if descending else ">"
        cast = self._cursor_value(q.sort, value)
        keyset_sql = (
            f"((t.{alias} {comparator} %s) OR (t.{alias} = %s AND t.doc_key > %s) "
            f"OR t.{alias} IS NULL)"
        )
        return order_sql, keyset_sql, [cast, cast, q.cursor.doc_key]

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
        where_sql, where_params = self._where(q, trigram=trigram)
        score_sql, score_params = self._score_expression(q)
        distance_sql, distance_params = self._distance_expression(q)
        order_sql, keyset_sql, keyset_params = self._order_by(q)

        # Parameter order follows the textual order of the statement, and the
        # inner SELECT comes first. Each parameterized expression is written
        # exactly once, which is the point of the subquery.
        params = list(score_params) + list(distance_params) + list(where_params)
        params += list(keyset_params)
        params.append(q.limit + 1)

        outer_where = f"WHERE {keyset_sql}" if keyset_sql else ""
        sql = f"""
            SELECT t.doc_key, t.score, t.distance_km, t.published_at, t.price_base
              FROM (
                SELECT d.doc_key,
                       ({score_sql}) AS score,
                       ({distance_sql}) AS distance_km,
                       d.published_at, d.price_base
                  FROM {_TABLE} d
                 WHERE {where_sql}
              ) t
             {outer_where}
             ORDER BY {order_sql}
             LIMIT %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        has_next = len(rows) > q.limit
        rows = rows[: q.limit]
        hits = []
        for doc_key, score, distance, published_at, price_base in rows:
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
            hits.append(
                Hit(
                    key=doc_key,
                    score=round(float(score or 0.0), 6),
                    distance_km=None if distance is None else round(float(distance), 2),
                    sort_value=sort_value,
                )
            )
        return hits, has_next

    def query(self, q: SearchQuery) -> QueryResult:
        self._require_postgres()
        from ..conf import search_settings

        hits, has_next = self._run(q, trigram=False)
        degraded: list[str] = []
        if (
            q.text is not None
            and not q.text.is_empty
            and len(hits) < int(search_settings.TYPO_FALLBACK_THRESHOLD)
        ):
            if self.has_trigram():
                hits, has_next = self._run(q, trigram=True)
            else:
                degraded.append("typo_tolerance")
        if q.text is not None and not q.text.is_empty:
            degraded.append("phrase_synonyms")

        total = self._estimate_total(q)
        return QueryResult(
            hits=tuple(hits),
            total=total,
            exact_total=False,
            has_next=has_next,
            has_prev=q.cursor is not None,
            degraded=tuple(dict.fromkeys(degraded)),
        )

    def _estimate_total(self, q: SearchQuery) -> int:
        """Candidate count, capped: the number is an estimate and says so.

        Counting the whole candidate set on every page is the cost this
        backend exists to avoid, so the count stops at the cap + 1 and
        ``exact_total: false`` travels with it.
        """
        from ..conf import search_settings

        cap = int(search_settings.FACET_CANDIDATE_CAP)
        where_sql, where_params = self._where(q, trigram=False)
        sql = f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} d WHERE {where_sql} LIMIT %s) s"
        with connection.cursor() as cursor:
            cursor.execute(sql, list(where_params) + [cap + 1])
            return int(cursor.fetchone()[0])

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


__all__ = ["READ_PATH_IMPL", "PostgresBackendUnavailable", "PostgresSearchBackend"]
