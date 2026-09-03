"""``backend_conformance`` — the suite every engine must pass, including yours.

stapel-geo has two interchangeable search backends and **no** conformance
suite: its only cross-backend test asserts that the two stubs raise
``NotImplementedError``, and the real Postgres and Redis backends are
tested separately, by different fixtures, overlapping in three scenarios.
For two engines that are meant to be swapped by one settings key that is a
hole, and it is the hole this module refuses to reproduce — which is why
this file was written BEFORE the second backend, not after (spec §17.3).

Public and importable, so a third-party backend runs the same scenarios::

    from stapel_search.testing import SCENARIOS, harness

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
    def test_my_backend(scenario, db):
        with harness("my_backend") as ctx:
            scenario.run(ctx)

The release rule this encodes: **a new backend without a green conformance
run does not merge.** Differences between engines are legitimate only
through ``capabilities()`` — a scenario is skipped when the matching
capability is ``False``, and it FAILS when the capability is ``True`` and
the behaviour diverges. A backend cannot pass by claiming less than it
does, either: several scenarios assert the *presence* of behaviour that a
declared capability promises.
"""
from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from .dto import GeoFilter, RangeFilter, SearchDocumentInput, SearchQuery
from .registry import SourceSpec, register_source, unregister_source

DOC_TYPE = "conformance"

#: Luxembourg City, and a point ~4.4km north of it.
CENTER = (49.6116, 6.1319)
NEAR = (49.6516, 6.1319)
FAR = (50.9375, 6.9603)  # Cologne, ~190km


class ConformanceSkip(Exception):
    """Raised by a scenario whose capability the backend does not claim."""


def _mapper(payload: dict) -> SearchDocumentInput:
    """Trivial mapper: the corpus is already in document shape.

    Unknown keys are dropped rather than exploding — a source Function is
    free to serve more than the index consumes (and stapel-listings does:
    `key` and `seq` ride along with every export row).
    """
    fields = {f.name for f in dataclasses.fields(SearchDocumentInput)}
    return SearchDocumentInput(**{k: v for k, v in payload.items() if k in fields})


CONFORMANCE_SOURCE = SourceSpec(
    doc_type=DOC_TYPE,
    mapper=_mapper,
    content_function="conformance.documents",
    export_function="conformance.export",
    signals=("conformance.changed",),
    key_fields=("key",),
    visible_statuses=frozenset({"published"}),
)


def _document(**overrides) -> SearchDocumentInput:
    base = dict(
        doc_type=DOC_TYPE,
        doc_key="0",
        status="published",
        language="ru",
        owner_key="u1",
        category_id="phones",
        category_path=("electronics", "phones"),
        title="",
        body="",
        text_extra=(),
        features={},
        price_base=None,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        lat=None,
        lon=None,
        geohash="",
        card={},
        seq=1,
    )
    base.update(overrides)
    return SearchDocumentInput(**base)


def _feature(type_slug: str, value):
    return {"type": type_slug, "value": value}


def corpus() -> list[SearchDocumentInput]:
    """The fixed corpus every scenario reasons about.

    Small and hand-shaped rather than generated: each row exists to make one
    assertion possible, and a reader can hold all of it in their head while
    reading a failure.
    """
    from stapel_geo import geohash as gh

    return [
        _document(
            doc_key="1",
            title="Apple iPhone 13 Pro",
            body="Отличные телефоны в идеальном состоянии",
            text_extra=("128 ГБ",),
            features={
                "brand": _feature("string", "apple"),
                "color": _feature("select", ["red"]),
                "year": _feature("int", 2015),
                "tree": {"type": "hierarchical_select", "value": ["a", "b", "c"]},
            },
            price_base=Decimal("500.00"),
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            lat=Decimal(str(CENTER[0])),
            lon=Decimal(str(CENTER[1])),
            geohash=gh.encode(CENTER[0], CENTER[1], precision=12),
            card={"title": "Apple iPhone 13 Pro", "price": "500.00"},
        ),
        _document(
            doc_key="2",
            title="Samsung Galaxy",
            body="самсунг телефон",
            features={
                "brand": _feature("string", "samsung"),
                "color": _feature("select", ["blue", "red"]),
                "year": _feature("int", 2014),
            },
            price_base=Decimal("300.00"),
            published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            lat=Decimal(str(NEAR[0])),
            lon=Decimal(str(NEAR[1])),
            geohash=gh.encode(NEAR[0], NEAR[1], precision=12),
            card={"title": "Samsung Galaxy"},
        ),
        _document(
            doc_key="3",
            title="Ноутбук Lenovo",
            body="мощный ноутбук для работы",
            category_id="laptops",
            category_path=("electronics", "laptops"),
            features={
                "brand": _feature("string", "lenovo"),
                "year": _feature("int", 2020),
            },
            price_base=None,
            published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            lat=Decimal(str(FAR[0])),
            lon=Decimal(str(FAR[1])),
            geohash=gh.encode(FAR[0], FAR[1], precision=12),
            card={"title": "Ноутбук Lenovo"},
        ),
        _document(
            doc_key="4",
            title="Antimeridian buoy",
            body="floating near the date line",
            owner_key="u2",
            category_path=("marine",),
            features={"brand": _feature("string", "acme")},
            price_base=Decimal("10.00"),
            published_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            lat=Decimal("0.5"),
            lon=Decimal("179.900000"),
            geohash=gh.encode(0.5, 179.9, precision=12),
            card={"title": "Antimeridian buoy"},
        ),
        _document(
            doc_key="5",
            title="Draft never published",
            status="draft",
            body="should never be findable",
            features={"brand": _feature("string", "apple")},
            price_base=Decimal("1.00"),
        ),
    ]


@dataclass
class Context:
    """What a scenario is handed."""

    backend: object
    capabilities: object

    def query(self, **overrides) -> SearchQuery:
        """A query for these scenarios — PRECISE unless one asks otherwise.

        ``SearchQuery.audience`` defaults to ``anonymous``, which puts every
        geo answer on the ~1.1km public grid. That default is the fail-closed
        one and it stays, but a conformance scenario asking "do these two
        engines compute the same great circle" must ask it of the arithmetic,
        not of the rounding that hides four decimal places of it. So the
        harness asks as staff, and the grid gets scenarios of its own
        (``public_grid_*``) which every engine is held to just the same.
        """
        from stapel_attributes import visibility

        params = dict(
            doc_type=DOC_TYPE,
            language="",
            sort="newest",
            limit=20,
            audience=visibility.AUDIENCE_STAFF,
        )
        params.update(overrides)
        return SearchQuery(**params)

    def keys(self, q: SearchQuery) -> list[str]:
        return [hit.key for hit in self.backend.query(q).hits]

    def text(self, raw: str, lang: str = "ru"):
        from .text import normalize_query

        return normalize_query(raw, lang)

    def require(self, capability: str) -> None:
        """Skip when the engine does not claim *capability*.

        The only legitimate way for two backends to differ. A scenario is
        never skipped because it is inconvenient — only because the engine
        said, in ``capabilities()``, that it cannot do the thing.
        """
        if not getattr(self.capabilities, capability, False):
            raise ConformanceSkip(capability)

    def reindex(self, documents=None) -> None:
        from .services import index_documents

        index_documents(DOC_TYPE, documents if documents is not None else corpus())


@contextlib.contextmanager
def harness():
    """Register the conformance source, load the corpus, clean up after."""
    register_source(CONFORMANCE_SOURCE)
    try:
        from .backends import get_backend

        backend = get_backend()
        backend.clear(DOC_TYPE)
        from .models import SearchDocument

        SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
        # The production order: engine settings first, documents second.
        # An engine told about its filterable attributes only after the
        # documents arrived is an engine that has to reindex them, which is
        # why `search_rebuild --apply-settings` exists in that order too.
        from .services import apply_settings

        apply_settings(DOC_TYPE)
        context = Context(backend=backend, capabilities=backend.capabilities())
        context.reindex()
        yield context
    finally:
        try:
            from .backends import get_backend

            get_backend().clear(DOC_TYPE)
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass
        unregister_source(DOC_TYPE)


@dataclass(frozen=True)
class Scenario:
    name: str
    run: Callable[[Context], None]
    about: str = ""


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


def _s_type_isolation(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query())) == {"1", "2", "3", "4"}
    other = ctx.query(doc_type="not-a-corpus")
    assert ctx.backend.query(other).hits == ()


def _s_visibility(ctx: Context) -> None:
    # Key 5 is `draft`: never findable, though it was handed to the indexer.
    assert "5" not in ctx.keys(ctx.query())


def _s_empty_index(ctx: Context) -> None:
    from .models import SearchDocument

    ctx.backend.clear(DOC_TYPE)
    SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
    result = ctx.backend.query(ctx.query())
    assert result.hits == ()
    assert result.total == 0
    assert result.has_next is False
    ctx.reindex()


def _s_delete(ctx: Context) -> None:
    from .services import remove_documents

    remove_documents(DOC_TYPE, ["2"])
    assert "2" not in ctx.keys(ctx.query())
    assert "1" in ctx.keys(ctx.query())
    ctx.reindex()


def _s_upsert_idempotent(ctx: Context) -> None:
    before = ctx.keys(ctx.query())
    ctx.reindex()
    ctx.reindex()
    assert ctx.keys(ctx.query()) == before


def _s_owner_filter(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(owner_key="u2"))) == {"4"}
    assert set(ctx.keys(ctx.query(owner_key="u1"))) == {"1", "2", "3"}


def _s_language_filter(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(language_filter="ru"))) == {"1", "2", "3", "4"}
    assert ctx.keys(ctx.query(language_filter="de")) == []
    # No explicit language means no corpus predicate at all.
    assert set(ctx.keys(ctx.query())) == {"1", "2", "3", "4"}


def _s_category_rollup(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(category_path=("electronics",)))) == {"1", "2", "3"}
    assert set(ctx.keys(ctx.query(category_path=("electronics", "laptops")))) == {"3"}
    assert ctx.keys(ctx.query(category_path=("nonexistent",))) == []


def _s_facet_filter(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(facets={"brand": ["apple"]}))) == {"1"}
    # OR within a slug.
    assert set(ctx.keys(ctx.query(facets={"brand": ["apple", "samsung"]}))) == {"1", "2"}
    # AND between slugs.
    assert ctx.keys(ctx.query(facets={"brand": ["apple"], "color": ["blue"]})) == []


def _s_facet_multivalue(ctx: Context) -> None:
    # Key 2 carries both red and blue; a select is a multi-term facet.
    assert set(ctx.keys(ctx.query(facets={"color": ["red"]}))) == {"1", "2"}
    assert set(ctx.keys(ctx.query(facets={"color": ["blue"]}))) == {"2"}


def _s_facet_path_rollup(ctx: Context) -> None:
    # hierarchical_select stores every prefix, so an ancestor finds the leaf.
    assert set(ctx.keys(ctx.query(facets={"tree": ["a"]}))) == {"1"}
    assert set(ctx.keys(ctx.query(facets={"tree": ["a/b"]}))) == {"1"}
    assert set(ctx.keys(ctx.query(facets={"tree": ["a/b/c"]}))) == {"1"}
    assert ctx.keys(ctx.query(facets={"tree": ["b"]})) == []


def _s_range_inclusive(ctx: Context) -> None:
    q = ctx.query(ranges=(RangeFilter(slug="year", lower=Decimal(2015)),))
    keys = set(ctx.keys(q))
    assert "1" in keys, "2015.. must include the 2015 document"
    assert "2" not in keys, "2015.. must exclude the 2014 document"


def _s_range_both_ends(ctx: Context) -> None:
    q = ctx.query(
        ranges=(RangeFilter(slug="year", lower=Decimal(2014), upper=Decimal(2015)),)
    )
    assert set(ctx.keys(q)) == {"1", "2"}


def _s_range_open_upper(ctx: Context) -> None:
    q = ctx.query(ranges=(RangeFilter(slug="year", upper=Decimal(2014)),))
    assert set(ctx.keys(q)) == {"2"}


def _s_core_range_price(ctx: Context) -> None:
    """``r.price`` filters on the document's own column, not on an attribute.

    No engine may serve this through ``SearchNumber``: nothing writes a
    ``price`` row there, so a backend that forgets the split answers zero
    for every bound — the exact silence a live classified stand shipped.
    """
    q = ctx.query(ranges=(RangeFilter(slug="price", lower=Decimal("100"), upper=Decimal("400")),))
    assert set(ctx.keys(q)) == {"2"}, "100..400 selects the 300 and nothing else"

    q = ctx.query(ranges=(RangeFilter(slug="price", upper=Decimal("100")),))
    assert set(ctx.keys(q)) == {"4"}, "..100 selects the 10"

    # Key 3 has no price. An open lower bound must not adopt it.
    q = ctx.query(ranges=(RangeFilter(slug="price", lower=Decimal("0")),))
    assert "3" not in set(ctx.keys(q)), "an unpriced document is not a cheap one"

    # The two axes compose, and neither shadows the other.
    q = ctx.query(
        ranges=(
            RangeFilter(slug="price", lower=Decimal("100")),
            RangeFilter(slug="year", lower=Decimal("2015")),
        )
    )
    assert set(ctx.keys(q)) == {"1"}


def _s_facet_counts(ctx: Context) -> None:
    ctx.require("facet_counts")
    from .dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    result = ctx.backend.facets(ctx.query(), plan)
    assert result.counts["brand"]["apple"] == 1
    assert result.counts["brand"]["samsung"] == 1


def _s_facet_drilldown(ctx: Context) -> None:
    """The assertion the whole facet design exists for.

    With ``brand=apple`` selected, counting ``brand`` must still report the
    other brands — otherwise the panel shows N for the chosen value and 0
    for every neighbour, and the user cannot change their mind.
    """
    ctx.require("facet_counts")
    from .dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    result = ctx.backend.facets(ctx.query(facets={"brand": ["apple"]}), plan)
    assert result.counts["brand"].get("apple") == 1
    assert result.counts["brand"].get("samsung") == 1, (
        "a selected facet must not zero its neighbours"
    )


def _s_facet_counts_respect_other_filters(ctx: Context) -> None:
    ctx.require("facet_counts")
    from .dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    result = ctx.backend.facets(ctx.query(category_path=("electronics", "laptops")), plan)
    assert result.counts["brand"].get("lenovo") == 1
    assert "apple" not in result.counts["brand"]


def _s_exact_facet_counts(ctx: Context) -> None:
    ctx.require("exact_facet_counts")
    from .dto import FacetPlan

    plan = FacetPlan(slugs=("brand",), kinds={"brand": "term"})
    result = ctx.backend.facets(ctx.query(), plan)
    assert result.approximate is False


def _s_text_title(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(text=ctx.text("Samsung", "")))) == {"2"}


def _s_text_body_only(ctx: Context) -> None:
    keys = set(ctx.keys(ctx.query(text=ctx.text("работы", "ru"), language="ru")))
    assert keys == {"3"}, "a word only in the body must find the document"


def _s_text_extra(ctx: Context) -> None:
    assert set(ctx.keys(ctx.query(text=ctx.text("128", "")))) == {"1"}


def _s_text_negative(ctx: Context) -> None:
    assert ctx.keys(ctx.query(text=ctx.text("nonexistentword", ""))) == []


def _s_text_and_semantics(ctx: Context) -> None:
    # Two terms are AND-ed: no document has both.
    assert ctx.keys(ctx.query(text=ctx.text("Samsung Lenovo", ""))) == []


def _s_typo_tolerance(ctx: Context) -> None:
    ctx.require("typo_tolerance")
    keys = set(ctx.keys(ctx.query(text=ctx.text("Samsng", ""))))
    assert "2" in keys, "a declared typo_tolerance must actually tolerate a typo"


def _s_synonym_expansion(ctx: Context) -> None:
    # Curated equivalents are applied by the shared normalizer, so BOTH
    # engines find the Cyrillic body text from the Latin query term.
    keys = set(ctx.keys(ctx.query(text=ctx.text("samsung", "ru"), language="ru")))
    assert "2" in keys


def _s_synonym_cross_script(ctx: Context) -> None:
    """The direction a Russian buyer actually types, on a Latin catalogue.

    ``_s_synonym_expansion`` above covers Latin query -> Cyrillic text, which
    is the easy half and was never broken. The half measured broken on a live
    board is this one: the corpus is a catalogue of Latin brand names, the
    buyer types the brand as it *sounds*, and «айфон» found 2 documents where
    ``iphone`` found 15.

    Two of these three are unreachable by any letter table — ``transliterate
    ("эпл")`` is ``epl`` and ``transliterate("айфон")`` is ``ayfon``, neither
    of which is a substring of anything — so passing this scenario is a claim
    about the dictionary layer, not about the transliterator. It is a
    conformance scenario rather than a unit test because a normalizer that
    expands into an engine that ignores the expansion answers ``2`` just the
    same, which is precisely how the defect survived: the expansion was
    correct and invisible.
    """
    for typed, expected in (("айфон", "1"), ("эпл", "1"), ("самсунг", "2")):
        keys = set(ctx.keys(ctx.query(text=ctx.text(typed, "ru"), language="ru")))
        assert expected in keys, f"{typed!r} did not reach document {expected}"

    # The negative, in the same scenario so no engine can pass the reach
    # without also passing the restraint: a Cyrillic word no group claims
    # expands to itself and its script twin, and neither is in the corpus.
    assert ctx.keys(ctx.query(text=ctx.text("эпоксидка", "ru"), language="ru")) == []


def _s_sort_newest(ctx: Context) -> None:
    assert ctx.keys(ctx.query(sort="newest")) == ["3", "2", "4", "1"]


def _s_sort_price_asc_nulls_last(ctx: Context) -> None:
    keys = ctx.keys(ctx.query(sort="price_asc"))
    assert keys == ["4", "2", "1", "3"], "NULL price sorts last ascending"


def _s_sort_price_desc_nulls_last(ctx: Context) -> None:
    keys = ctx.keys(ctx.query(sort="price_desc"))
    assert keys == ["1", "2", "4", "3"], "NULL price sorts last descending too"


def _s_tiebreak_is_stable(ctx: Context) -> None:
    first = ctx.keys(ctx.query(sort="newest"))
    second = ctx.keys(ctx.query(sort="newest"))
    assert first == second


def _s_geo_radius(ctx: Context) -> None:
    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=10)
    keys = set(ctx.keys(ctx.query(geo=near)))
    assert keys == {"1", "2"}, "documents within 10km, and nothing else"
    tight = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=1)
    assert set(ctx.keys(ctx.query(geo=tight))) == {"1"}


def _s_geo_excludes_coordless(ctx: Context) -> None:
    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=10)
    assert "5" not in ctx.keys(ctx.query(geo=near))


def _s_geo_distance_reported(ctx: Context) -> None:
    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=10)
    hits = {hit.key: hit for hit in ctx.backend.query(ctx.query(geo=near)).hits}
    assert hits["1"].distance_km is not None
    assert hits["1"].distance_km < 0.5
    assert 3.0 < hits["2"].distance_km < 6.0


def _s_sort_distance(ctx: Context) -> None:
    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=500)
    keys = ctx.keys(ctx.query(geo=near, sort="distance"))
    assert keys[:3] == ["1", "2", "3"]


def _s_bbox(ctx: Context) -> None:
    box = GeoFilter(min_lat=49.0, min_lon=6.0, max_lat=50.0, max_lon=7.0)
    assert set(ctx.keys(ctx.query(geo=box))) == {"1", "2"}


def _s_bbox_antimeridian(ctx: Context) -> None:
    """``min_lon > max_lon`` crosses +/-180 — the contract from stapel-geo."""
    box = GeoFilter(min_lat=-10.0, min_lon=179.0, max_lat=10.0, max_lon=-179.0)
    assert set(ctx.keys(ctx.query(geo=box))) == {"4"}
    # The same numbers the other way round are an ordinary box that spans
    # almost the whole planet the long way, and must NOT single out key 4.
    wide = GeoFilter(min_lat=-10.0, min_lon=-179.0, max_lat=10.0, max_lon=179.0)
    assert "4" not in ctx.keys(ctx.query(geo=wide))


#: Two positions near opposite corners of ONE public cell, 1.2km apart. Any
#: geo answer that can tell them apart is finer than the card.
_CELL_PROBES = ((49.6060, 6.1260), (49.6146, 6.1346))


def _s_public_grid_distance_is_quantized(ctx: Context) -> None:
    """A stranger's distance is a multiple of the grid quantum — everywhere.

    The one rule three engines have to agree on, because two of them used to
    round it to two decimals (ten metres) and one did not round it at all.
    """
    from stapel_attributes import visibility

    from .backends import _shared as shared

    quantum = shared.distance_quantum_km(shared.public_precision())
    near = GeoFilter(lat=CENTER[0], lon=CENTER[1], radius_km=500)
    q = ctx.query(geo=near, audience=visibility.ANONYMOUS)
    reported = [
        hit.distance_km for hit in ctx.backend.query(q).hits if hit.distance_km is not None
    ]
    assert reported, "the corpus has rows inside 500km"
    for distance in reported:
        steps = distance / quantum
        assert abs(steps - round(steps)) < 1e-6, (distance, quantum)


def _s_public_grid_hides_the_pin(ctx: Context) -> None:
    """Two positions inside one cell are ONE answer, to every engine.

    The property the trilateration and bisection suites are written against,
    asked of the engine directly: move the row anywhere inside its published
    cell and no geo answer moves with it.
    """
    import dataclasses

    from stapel_attributes import visibility

    from .backends import _shared as shared
    from .query import parse_geo
    from .services import index_documents

    precision = shared.public_precision()
    step = 10.0**-precision
    base = corpus()[0]
    centre = (CENTER[0] + 3.0, CENTER[1] + 3.0)
    near = GeoFilter(lat=centre[0], lon=centre[1], radius_km=500)
    # A quarter-cell box around the FIRST probe: it holds one of the two
    # positions and not the other, until the box is snapped to whole cells.
    box = parse_geo(
        {"bbox": "49.6050,6.1250,49.6100,6.1300"}, audience=visibility.ANONYMOUS
    )
    assert step > 0

    def observe() -> tuple:
        q = ctx.query(geo=near, audience=visibility.ANONYMOUS)
        hits = {hit.key: hit.distance_km for hit in ctx.backend.query(q).hits}
        inside = set(
            ctx.keys(ctx.query(geo=box, audience=visibility.ANONYMOUS))
        )
        return hits.get("1"), "1" in inside

    from decimal import Decimal

    from stapel_geo import geohash as gh

    seen = set()
    for lat, lon in _CELL_PROBES:
        assert (
            shared.snap_to_grid(lat, precision),
            shared.snap_to_grid(lon, precision),
        ) == (
            shared.snap_to_grid(CENTER[0], precision),
            shared.snap_to_grid(CENTER[1], precision),
        ), "the probes must stay inside one cell"
        index_documents(
            DOC_TYPE,
            [
                dataclasses.replace(
                    base,
                    lat=Decimal(str(lat)),
                    lon=Decimal(str(lon)),
                    geohash=gh.encode(lat, lon, precision=12),
                    seq=base.seq + 100,
                )
            ],
        )
        seen.add(observe())
    # Put the row back where the rest of the suite expects it.
    index_documents(DOC_TYPE, [dataclasses.replace(base, seq=base.seq + 200)])
    assert len(seen) == 1, f"the answer moved with the pin: {seen}"


def _s_public_grid_bbox_is_whole_cells(ctx: Context) -> None:
    """The rectangle an anonymous caller draws is snapped to whole cells.

    Asked through the parser, because that is where it happens and where all
    three engines inherit it from.
    """
    from stapel_attributes import visibility

    from .backends import _shared as shared
    from .query import parse_geo

    precision = shared.public_precision()
    step = 10.0**-precision
    params = {"bbox": "49.61155,6.13185,49.61165,6.13195"}
    tight = parse_geo(params, audience=visibility.AUDIENCE_STAFF)
    assert tight.max_lat - tight.min_lat < step
    grid = parse_geo(params, audience=visibility.ANONYMOUS)
    assert grid.max_lat - grid.min_lat >= step - 1e-9
    assert grid.max_lon - grid.min_lon >= step - 1e-9
    assert grid.min_lat <= tight.min_lat and grid.max_lat >= tight.max_lat


def _s_boost_moves_relevance(ctx: Context) -> None:
    from .services import apply_signal

    q = ctx.query(text=ctx.text("телефон", "ru"), language="ru", sort="relevance")
    apply_signal({"doc_type": DOC_TYPE, "doc_key": "2", "boost": 5.0}, event_id="c-boost")
    boosted = ctx.keys(q)
    assert boosted[0] == "2", "a boost must move a document under sort=relevance"


def _s_boost_does_not_move_explicit_sort(ctx: Context) -> None:
    from .services import apply_signal

    apply_signal({"doc_type": DOC_TYPE, "doc_key": "3", "boost": 5.0}, event_id="c-boost2")
    keys = ctx.keys(ctx.query(sort="price_asc"))
    assert keys == ["4", "2", "1", "3"], (
        "an explicit sort must never receive a promotional boost"
    )


def _s_popularity_signal(ctx: Context) -> None:
    from .services import apply_signal

    q = ctx.query(text=ctx.text("телефон", "ru"), language="ru", sort="relevance")
    before = {hit.key: hit.score for hit in ctx.backend.query(q).hits}
    apply_signal(
        {"doc_type": DOC_TYPE, "doc_key": "1", "popularity": 1000}, event_id="c-pop"
    )
    after = {hit.key: hit.score for hit in ctx.backend.query(q).hits}
    assert after.get("1", 0) > before.get("1", 0)


def _s_promotion_expiry(ctx: Context) -> None:
    from .services import apply_signal, expire_signals

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    apply_signal(
        {"doc_type": DOC_TYPE, "doc_key": "1", "promoted": True, "boost": 3.0, "expires_at": past},
        event_id="c-expire",
    )
    assert expire_signals() >= 1
    from .models import SearchDocument

    row = SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1")
    assert row.promoted is False
    assert row.boost == 0.0


def _s_cursor_pages_cover_everything(ctx: Context) -> None:
    seen: list[str] = []
    q = ctx.query(sort="newest", limit=2)
    for _ in range(5):
        result = ctx.backend.query(q)
        seen.extend(hit.key for hit in result.hits)
        if not result.has_next or not result.hits:
            break
        from .dto import Cursor

        last = result.hits[-1]
        q = ctx.query(
            sort="newest",
            limit=2,
            cursor=Cursor(sort_value=last.sort_value, doc_key=last.key, offset=len(seen)),
        )
    assert seen == ["3", "2", "4", "1"]
    assert len(seen) == len(set(seen)), "keyset paging must not repeat a row"


def _s_cursor_stable_under_insert(ctx: Context) -> None:
    """A row inserted between pages must not shift the page boundary.

    This is why the cursor is a keyset anchor and not an offset: with an
    offset, one insertion makes the reader see one row twice and miss
    another, and nothing in the response says so.
    """
    from .dto import Cursor

    q = ctx.query(sort="newest", limit=2)
    first = ctx.backend.query(q)
    page_one = [hit.key for hit in first.hits]

    ctx.reindex(
        [
            _document(
                doc_key="6",
                title="Inserted mid-pagination",
                features={"brand": _feature("string", "acme")},
                published_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
                price_base=Decimal("42.00"),
            )
        ]
    )

    last = first.hits[-1]
    second = ctx.backend.query(
        ctx.query(
            sort="newest",
            limit=2,
            cursor=Cursor(sort_value=last.sort_value, doc_key=last.key, offset=2),
        )
    )
    page_two = [hit.key for hit in second.hits]
    assert not set(page_one) & set(page_two), "no row may appear on both pages"


def _s_exact_total(ctx: Context) -> None:
    ctx.require("exact_total")
    result = ctx.backend.query(ctx.query())
    assert result.exact_total is True
    assert result.total == 4


def _s_count_never_contradicts_hits(ctx: Context) -> None:
    """No engine may report fewer matches than the page it just returned.

    Every backend, no capability gate: ``total: 0`` beside non-empty hits is
    not an approximation, it is a contradiction — and the storefront printed
    it as «Примерно 0 объявлений» over four visible cards. The typo query is
    here on purpose: the fuzzy arm is where a backend is tempted to count
    one question and answer another.
    """
    for label, q in (
        ("plain", ctx.query(limit=2)),
        ("relevance", ctx.query(sort="relevance", text=ctx.text("Samsng", ""), limit=2)),
    ):
        result = ctx.backend.query(q)
        if not result.hits:
            continue
        assert result.total is not None, f"{label}: a page of hits is not an unknown count"
        assert result.total >= len(result.hits), (
            f"{label}: total={result.total} under {len(result.hits)} visible hits"
        )
        assert not (result.exact_total and result.total_is_lower_bound), (
            f"{label}: a count cannot be exact AND a lower bound"
        )


def _s_suggest(ctx: Context) -> None:
    ctx.require("suggest")
    titles = ctx.backend.suggest(DOC_TYPE, "Apple", limit=5)
    assert any(title.startswith("Apple") for title in titles)


def _s_suggest_empty_prefix(ctx: Context) -> None:
    ctx.require("suggest")
    assert isinstance(ctx.backend.suggest(DOC_TYPE, "", limit=5), list)


def _s_suggest_categories_from_goods(ctx: Context) -> None:
    """The OPTIONAL tenth verb, held to the naive reference semantics.

    ``suggest_categories`` is not in ``VERBS`` (``backends/base.py``): an
    engine without it is skipped here and degrades loudly at the service
    layer. An engine WITH it must answer the same pairs the naive walk
    answers over this corpus — the count is the SERP's count for the tap the
    row invites, which is the whole value of the row.
    """
    fn = getattr(ctx.backend, "suggest_categories", None)
    if fn is None:
        raise ConformanceSkip("suggest_categories")
    from .text import normalize_query

    pairs = fn(DOC_TYPE, "iphone", language="ru", limit=5)
    assert pairs, "the corpus holds an iPhone; the goods must answer"
    assert ("electronics", "phones") in [path for path, _count in pairs]
    # The invariant is NOT a fixed number — a typo-tolerant engine may
    # legitimately widen «iphone» to more documents than the naive walk
    # (Postgres' trigram arm reaches «телефон» here, and so does its SERP).
    # The invariant is that each pair's count is the count the tap will
    # find: the engine's OWN query() total for the same text and category.
    for path, count in pairs:
        answer = ctx.backend.query(
            ctx.query(
                language="ru",
                text=normalize_query("iphone", "ru"),
                category_path=path,
            )
        )
        assert count == answer.total, path
    # The two honest silences: no query, and no matching goods at all.
    assert fn(DOC_TYPE, "", language="ru", limit=5) == []
    assert fn(DOC_TYPE, "квадрокоптер", language="ru", limit=5) == []


def _s_category_counts(ctx: Context) -> None:
    """The OPTIONAL eleventh verb, held to the same naive reference.

    ``category_counts`` is what the facet plan is drawn from when the
    queried category owns no axes — a branch, or a text query naming no
    category at all (D175). Two invariants, and the second is the one that
    makes the plan honest: the aggregate describes the QUERY's candidate
    set, not the corpus, so a category filter narrows it exactly as it
    narrows the page.
    """
    fn = getattr(ctx.backend, "category_counts", None)
    if fn is None:
        raise ConformanceSkip("category_counts")

    whole = dict(fn(ctx.query(), limit=10))
    assert whole == {
        ("electronics", "phones"): 2,
        ("electronics", "laptops"): 1,
        ("marine",): 1,
    }, whole
    # Busiest first, the path breaking ties — an order a plan can trust.
    assert [path for path, _ in fn(ctx.query(), limit=10)][0] == ("electronics", "phones")

    branch = dict(fn(ctx.query(category_path=("electronics",)), limit=10))
    assert branch == {("electronics", "phones"): 2, ("electronics", "laptops"): 1}, branch

    leaf = dict(fn(ctx.query(category_path=("marine",)), limit=10))
    assert leaf == {("marine",): 1}, leaf

    assert fn(ctx.query(category_path=("nonexistent",)), limit=10) == []


def _s_goods_suggestions_do_not_guess(ctx: Context) -> None:
    """A near-miss query yields NO goods pairs — a misspelling is the SERP's
    business to widen, never a destination to promise.

    The live incident (a classified stand): a brand word that strictly
    matched nothing brushed real titles through the typo arm, and the
    dropdown offered an unrelated category as a confident destination with
    a confident count. The SERP may widen a near-miss — the page can say
    "showing results for a similar spelling" — but a suggestion row has no
    room for that caveat: it is a promise, so it may only be built from the
    strict predicate. «gaalxy» below is one transposition off a corpus
    title; a strict text predicate misses it, a trigram arm brushes it.
    """
    fn = getattr(ctx.backend, "suggest_categories", None)
    if fn is None:
        raise ConformanceSkip("suggest_categories")
    pairs = fn(DOC_TYPE, "gaalxy", language="ru", limit=5)
    assert pairs == [], (
        f"a near-miss spelling produced goods suggestions {pairs!r} — "
        "the typo widening belongs to the SERP, not to a promised destination"
    )


def _s_capabilities_are_declared(ctx: Context) -> None:
    caps = ctx.capabilities
    assert isinstance(caps.max_result_window, int) and caps.max_result_window > 0
    assert isinstance(caps.max_facet_fields, int) and caps.max_facet_fields > 0
    assert "relevance" in caps.supported_scorers


def _s_health(ctx: Context) -> None:
    status = ctx.backend.health()
    assert status.name
    assert status.reachable is True


def _s_normalized_query_is_engine_independent(ctx: Context) -> None:
    """The dictionary half must be byte-identical for every engine.

    A divergence here IS the seam defect this suite exists for: two engines
    that stem differently are a known trade-off, two engines that expand
    synonyms differently are a bug nobody can see from either side.
    """
    from .text import normalize_query

    a = normalize_query("Айфон б/у продам", "ru")
    b = normalize_query("Айфон б/у продам", "ru")
    assert a == b
    assert "продам" in a.dropped_stopwords
    assert any("iphone" in group for group in a.terms)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("type_isolation", _s_type_isolation, "another doc_type never leaks in"),
    Scenario("visibility", _s_visibility, "status decides membership, not the event"),
    Scenario("empty_index", _s_empty_index, "an empty corpus answers, it does not crash"),
    Scenario("delete", _s_delete, "removal takes the document out of the answer"),
    Scenario("upsert_idempotent", _s_upsert_idempotent, "re-indexing changes nothing"),
    Scenario("owner_filter", _s_owner_filter, "the seller's own listings"),
    Scenario("language_filter", _s_language_filter, "language scopes the corpus"),
    Scenario("category_rollup", _s_category_rollup, "a parent finds its descendants"),
    Scenario("facet_filter", _s_facet_filter, "AND between slugs, OR within one"),
    Scenario("facet_multivalue", _s_facet_multivalue, "a select matches any of its values"),
    Scenario("facet_path_rollup", _s_facet_path_rollup, "path prefixes roll up"),
    Scenario("range_inclusive", _s_range_inclusive, "2015.. includes 2015, excludes 2014"),
    Scenario("range_both_ends", _s_range_both_ends, "a closed range"),
    Scenario("range_open_upper", _s_range_open_upper, "..2014 is an open lower bound"),
    Scenario(
        "core_range_price",
        _s_core_range_price,
        "r.price filters the document's own column, and composes with attribute ranges",
    ),
    Scenario("facet_counts", _s_facet_counts, "counts equal the candidate set"),
    Scenario("facet_drilldown", _s_facet_drilldown, "a selection does not zero neighbours"),
    Scenario(
        "facet_counts_respect_other_filters",
        _s_facet_counts_respect_other_filters,
        "other filters still narrow the counted set",
    ),
    Scenario("exact_facet_counts", _s_exact_facet_counts, "no sampling below the cap"),
    Scenario("text_title", _s_text_title, "a title word finds the document"),
    Scenario("text_body_only", _s_text_body_only, "a body-only word finds it too"),
    Scenario("text_extra", _s_text_extra, "an attribute chip is searchable"),
    Scenario("text_negative", _s_text_negative, "an absent word finds nothing"),
    Scenario("text_and_semantics", _s_text_and_semantics, "terms are AND-ed"),
    Scenario("typo_tolerance", _s_typo_tolerance, "a declared typo tolerance is real"),
    Scenario("synonym_expansion", _s_synonym_expansion, "curated equivalents expand"),
    Scenario(
        "synonym_cross_script",
        _s_synonym_cross_script,
        "a Cyrillic query reaches a Latin catalogue, and only where it should",
    ),
    Scenario("sort_newest", _s_sort_newest, "publication order"),
    Scenario("sort_price_asc_nulls_last", _s_sort_price_asc_nulls_last, "NULL is not cheapest"),
    Scenario("sort_price_desc_nulls_last", _s_sort_price_desc_nulls_last, "NULL is not dearest"),
    Scenario("tiebreak_is_stable", _s_tiebreak_is_stable, "the same query, the same order"),
    Scenario("geo_radius", _s_geo_radius, "inside r=10, outside r=1"),
    Scenario("geo_excludes_coordless", _s_geo_excludes_coordless, "no coordinates, no claim"),
    Scenario("geo_distance_reported", _s_geo_distance_reported, "the distance comes back"),
    Scenario("sort_distance", _s_sort_distance, "nearest first"),
    Scenario("bbox", _s_bbox, "a plain rectangle"),
    Scenario("bbox_antimeridian", _s_bbox_antimeridian, "min_lon > max_lon crosses +/-180"),
    Scenario(
        "public_grid_distance_is_quantized",
        _s_public_grid_distance_is_quantized,
        "a stranger's distance is a multiple of the grid quantum",
    ),
    Scenario(
        "public_grid_hides_the_pin",
        _s_public_grid_hides_the_pin,
        "two positions in one cell are one answer",
    ),
    Scenario(
        "public_grid_bbox_is_whole_cells",
        _s_public_grid_bbox_is_whole_cells,
        "a stranger's rectangle is snapped to whole cells",
    ),
    Scenario("boost_moves_relevance", _s_boost_moves_relevance, "promotion works"),
    Scenario(
        "boost_does_not_move_explicit_sort",
        _s_boost_does_not_move_explicit_sort,
        "and only under relevance",
    ),
    Scenario("popularity_signal", _s_popularity_signal, "a signal raises the score"),
    Scenario("promotion_expiry", _s_promotion_expiry, "a paid slot really ends"),
    Scenario(
        "cursor_pages_cover_everything",
        _s_cursor_pages_cover_everything,
        "paging visits every row exactly once",
    ),
    Scenario(
        "cursor_stable_under_insert",
        _s_cursor_stable_under_insert,
        "an insertion between pages does not duplicate a row",
    ),
    Scenario("exact_total", _s_exact_total, "a declared exact total is exact"),
    Scenario(
        "count_never_contradicts_hits",
        _s_count_never_contradicts_hits,
        "no engine reports fewer matches than the page shows",
    ),
    Scenario("suggest", _s_suggest, "prefix suggestions come from the index"),
    Scenario("suggest_empty_prefix", _s_suggest_empty_prefix, "an empty prefix is not a crash"),
    Scenario(
        "suggest_categories",
        _s_suggest_categories_from_goods,
        "the optional goods-driven verb answers the SERP's own counts",
    ),
    Scenario(
        "category_counts",
        _s_category_counts,
        "the optional aggregate the facet plan is drawn from",
    ),
    Scenario(
        "goods_suggestions_do_not_guess",
        _s_goods_suggestions_do_not_guess,
        "a near-miss spelling promises no destination",
    ),
    Scenario("capabilities_are_declared", _s_capabilities_are_declared, "the seam is described"),
    Scenario("health", _s_health, "the engine reports itself"),
    Scenario(
        "normalized_query_is_engine_independent",
        _s_normalized_query_is_engine_independent,
        "the dictionary half is identical everywhere",
    ),
)

#: Backwards-friendly alias: the spec names this ``backend_conformance``.
backend_conformance = SCENARIOS


def run_all(context: Context) -> dict[str, str]:
    """Run every scenario against *context*; ``{name: "ok"|"skip:<cap>"}``.

    For callers outside pytest (a smoke check in a deployment pipeline).
    """
    results: dict[str, str] = {}
    for scenario in SCENARIOS:
        try:
            scenario.run(context)
        except ConformanceSkip as skip:
            results[scenario.name] = f"skip:{skip}"
        else:
            results[scenario.name] = "ok"
    return results


__all__ = [
    "CONFORMANCE_SOURCE",
    "DOC_TYPE",
    "SCENARIOS",
    "ConformanceSkip",
    "Context",
    "Scenario",
    "backend_conformance",
    "corpus",
    "harness",
    "run_all",
]
