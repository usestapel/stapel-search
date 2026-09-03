"""Query understanding: the words of a query become filters.

The case the module exists for is «красные штаны». The colour is an inline
``select`` whose option value IS the transliterated slug of its label
(``{"label": "Красный", "value": "krasnyy"}``), so the singular folds onto
it deterministically — and the PLURAL does not, because nothing in
``text.py`` stems anything by design. That gap is the whole reason a vector
rung exists, and the reason the rung is capped, floored and refusable: a
suggestion a human reads and ignores may be wrong, a filter that silently
narrows the answer may not.

Every seam is INJECTED here — no embedder, no comm bus, no database. What
is under test is the ladder, the spans, the cap and the degradation, none
of which needs a network to be true.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from stapel_search.dto import FacetPlan
from stapel_search.understanding import OPTION_CORPUS_KIND, extract

on = override_settings(STAPEL_SEARCH={"QUERY_UNDERSTANDING": True})

#: The live stand's shape: an inline `select` whose values are the
#: transliterated slugs of their own captions, plus a `ref_select` pointing
#: at a vocabulary level nobody may enumerate.
PLAN = FacetPlan(
    slugs=("color", "condition", "model"),
    kinds={"color": "term", "condition": "term", "model": "term"},
    closed_options={"color": ("krasnyy", "sinyy"), "condition": ("novyy", "b-u")},
    option_labels={
        "color": {"krasnyy": "Красный", "sinyy": "Синий"},
        "condition": {"novyy": "Новый", "b-u": "Б/у"},
    },
    vocabulary_refs={"model": ("phone-models", "model")},
)


def no_vector(kind, q, **kwargs):
    """A vector seam that is up and honestly has nothing."""
    return [], None


def no_match(name, payload):
    return {}


def scoring_vector(space):
    """A fake ``similar``: ``{token: hit}``, everything else empty."""
    calls = []

    def _similar(kind, q, **kwargs):
        calls.append((kind, q, kwargs))
        hit = space.get(q.strip().casefold())
        return ([hit] if hit else []), None

    _similar.calls = calls
    return _similar


# ─── the deterministic rungs ────────────────────────────────────────────


@on
def test_exact_rung_folds_the_caption():
    out = extract("красный", language="ru", plan=PLAN, vector=no_vector, match=no_match)

    (chip,) = out.filters
    assert (chip.slug, chip.value) == ("color", "krasnyy")
    assert chip.method == "exact"
    assert chip.confidence == 1.0
    assert chip.applied is True
    assert chip.param == "f.color=krasnyy"
    assert chip.value_label == "Красный"
    assert chip.span == (0, 7)
    assert out.residual == ""
    assert out.degraded == ()


@on
def test_exact_rung_never_pays_the_vector_seam():
    """A claimed token is not probed: an embedding is a round trip."""
    vector = scoring_vector({})
    extract("красный", language="ru", plan=PLAN, vector=vector, match=no_match)
    assert vector.calls == []


@on
def test_translit_rung_both_directions():
    """The captions are missing, so only the codes can answer."""
    latin = FacetPlan(slugs=("color",), closed_options={"color": ("krasnyy",)})
    cyrillic = FacetPlan(slugs=("color",), closed_options={"color": ("красный",)})

    typed_cyrillic = extract(
        "красный", language="ru", plan=latin, vector=no_vector, match=no_match
    )
    typed_latin = extract(
        "krasnyy", language="ru", plan=cyrillic, vector=no_vector, match=no_match
    )

    for out, value in ((typed_cyrillic, "krasnyy"), (typed_latin, "красный")):
        (chip,) = out.filters
        assert (chip.slug, chip.value, chip.method) == ("color", value, "translit")
        assert chip.confidence == pytest.approx(0.95)
        assert chip.applied is True


@on
def test_one_slug_is_claimed_at_most_once():
    out = extract(
        "красный синий", language="ru", plan=PLAN, vector=no_vector, match=no_match
    )
    assert [chip.value for chip in out.filters] == ["krasnyy"]
    assert out.residual == "синий"


# ─── the vector rung: morphology ────────────────────────────────────────


@on
def test_vector_rung_carries_the_plural():
    """«красные» stems to nothing here by design — the net catches it."""
    vector = scoring_vector(
        {
            "красные": {
                "key": "color=krasnyy",
                "text": "Красный",
                "payload": {"slug": "color", "value": "krasnyy"},
                "similarity": 0.91,
            }
        }
    )
    out = extract(
        "красные штаны", language="ru", plan=PLAN, vector=vector, match=no_match
    )

    (chip,) = out.filters
    assert (chip.slug, chip.value, chip.method) == ("color", "krasnyy", "vector")
    assert chip.confidence == pytest.approx(0.91)
    assert chip.applied is True
    assert chip.span == (0, 7)
    assert out.residual == "штаны"
    assert [call[0] for call in vector.calls] == [OPTION_CORPUS_KIND] * 2


@on
def test_vector_hit_below_the_floor_is_a_signal_not_a_filter():
    vector = scoring_vector(
        {
            "красноватые": {
                "key": "color=krasnyy",
                "text": "Красный",
                "payload": {"slug": "color", "value": "krasnyy"},
                "similarity": 0.70,
            }
        }
    )
    out = extract(
        "красноватые штаны", language="ru", plan=PLAN, vector=vector, match=no_match
    )

    (chip,) = out.filters
    assert chip.applied is False
    assert chip.confidence == pytest.approx(0.70)
    assert chip.method == "vector"


@on
def test_vector_hit_without_a_stated_score_is_refused():
    """An invented number is worse than no filter — this lib refuses both."""
    vector = scoring_vector(
        {
            "красные": {
                "key": "color=krasnyy",
                "text": "Красный",
                "payload": {"slug": "color", "value": "krasnyy"},
            }
        }
    )
    out = extract(
        "красные штаны", language="ru", plan=PLAN, vector=vector, match=no_match
    )

    assert out.filters == ()
    assert "understanding_vector" in out.degraded
    assert out.residual == "красные штаны"


@on
def test_vector_hit_outside_the_resolved_plan_is_ignored():
    """The corpus is global; the option space is the category's."""
    vector = scoring_vector(
        {
            "красные": {
                "key": "paint=krasnyy",
                "text": "Красный",
                "payload": {"slug": "paint", "value": "krasnyy"},
                "similarity": 0.99,
            }
        }
    )
    out = extract(
        "красные штаны", language="ru", plan=PLAN, vector=vector, match=no_match
    )
    assert out.filters == ()
    assert out.degraded == ()


# ─── the vocabulary rung: 815k terms nobody may enumerate ───────────────


@on
def test_ref_select_goes_through_the_match_function():
    payloads = []

    def match(name, payload):
        payloads.append((name, payload))
        if payload["text"].casefold() != "айфон 17":
            return {"match": None}
        return {
            "match": {
                "value": "iphone-17",
                "label": "iPhone 17",
                "score": 0.93,
                "method": "exact",
            }
        }

    out = extract("айфон 17", language="ru", plan=PLAN, vector=no_vector, match=match)

    (chip,) = out.filters
    assert (chip.slug, chip.value) == ("model", "iphone-17")
    assert chip.value_label == "iPhone 17"
    assert chip.method == "exact"
    assert chip.confidence == pytest.approx(0.93)
    assert chip.param == "f.model=iphone-17"
    assert chip.span == (0, 8)
    assert out.residual == ""

    name, sent = payloads[0]
    assert name == "vocabularies.match"
    assert sent["vocabulary"] == "phone-models"
    assert sent["level"] == "model"
    assert sent["language"] == "ru"
    assert "min_score" in sent


@on
def test_match_hit_without_a_stated_score_is_refused():
    def match(name, payload):
        return {"match": {"value": "iphone-17", "label": "iPhone 17"}}

    out = extract("айфон 17", language="ru", plan=PLAN, vector=no_vector, match=match)
    assert out.filters == ()
    assert "understanding_match" in out.degraded


# ─── degradation: nothing on the query path may raise ───────────────────


@on
def test_a_raising_vector_seam_degrades_and_keeps_the_deterministic_work():
    def vector(kind, q, **kwargs):
        raise RuntimeError("embedder down")

    out = extract(
        "красный ботинки", language="ru", plan=PLAN, vector=vector, match=no_match
    )

    (chip,) = out.filters
    assert (chip.slug, chip.method) == ("color", "exact")
    assert "understanding_vector" in out.degraded
    assert out.residual == "ботинки"


@on
def test_a_raising_match_seam_degrades():
    def match(name, payload):
        raise RuntimeError("comm down")

    out = extract("красный айфон", language="ru", plan=PLAN, vector=no_vector, match=match)

    assert [chip.slug for chip in out.filters] == ["color"]
    assert "understanding_match" in out.degraded


@on
def test_a_vector_shortfall_is_reported_not_swallowed():
    def vector(kind, q, **kwargs):
        return [], "vector_suggestions"

    out = extract("ботинки", language="ru", plan=PLAN, vector=vector, match=no_match)
    assert out.filters == ()
    assert "understanding_vector" in out.degraded


@on
def test_default_seams_are_safe_without_a_bus_or_an_embedder():
    """No injection, no vector layer, no responder: an answer all the same."""
    out = extract("красный айфон", language="ru", plan=PLAN)

    assert [chip.slug for chip in out.filters] == ["color"]
    assert "understanding_match" in out.degraded
    assert "understanding_vector" in out.degraded


# ─── spans, residual, the cap, the flag ─────────────────────────────────


@on
def test_spans_index_the_raw_query():
    q = "  КРАСНЫЙ   Штаны "
    out = extract(q, language="ru", plan=PLAN, vector=no_vector, match=no_match)

    (chip,) = out.filters
    assert chip.span == (2, 9)
    assert q[chip.span[0] : chip.span[1]] == "КРАСНЫЙ"
    assert out.residual == "Штаны"


@override_settings(
    STAPEL_SEARCH={"QUERY_UNDERSTANDING": True, "UNDERSTANDING_MAX_FILTERS": 1}
)
def test_the_cap_demotes_rather_than_drops():
    out = extract(
        "красный новый", language="ru", plan=PLAN, vector=no_vector, match=no_match
    )

    assert [chip.slug for chip in out.filters] == ["color", "condition"]
    assert [chip.applied for chip in out.filters] == [True, False]


def test_the_flag_off_extracts_nothing():
    out = extract(
        "красные штаны", language="ru", plan=PLAN, vector=no_vector, match=no_match
    )
    assert out.filters == ()
    assert out.residual == ""
    assert out.degraded == ()


@on
def test_an_empty_query_is_an_empty_extraction():
    out = extract("   ", language="ru", plan=PLAN, vector=no_vector, match=no_match)
    assert out.filters == ()
    assert out.residual == ""


@on
def test_a_plan_with_no_option_space_keeps_the_whole_query_as_text():
    """An unresolved category is not an error — it is a query with no axes."""
    out = extract(
        "красные  штаны", language="ru", plan=FacetPlan(), vector=no_vector, match=no_match
    )
    assert out.filters == ()
    assert out.residual == "красные штаны"
    assert out.degraded == ()


@on
def test_the_scope_rides_on_the_answer():
    out = extract(
        "красный",
        language="ru",
        plan=PLAN,
        category_path=("46", "48"),
        vector=no_vector,
        match=no_match,
    )
    assert out.category_path == ("46", "48")


# ─── the alias rung: the only route to a phonetic brand ──────────────────


@on
def test_alias_rung_reaches_an_option_the_letter_table_cannot():
    """«бу» is not a transliteration of ``b-u`` — it is a curated equivalent.

    ``text.transliterate("бу")`` is ``"bu"``, which is not the option code,
    so without the dictionary this option is unreachable from the word a
    reader actually types.
    """
    from stapel_search.text import transliterate

    assert transliterate("бу") != "b-u"

    answer = extract("бу", language="ru", plan=PLAN, vector=no_vector, match=no_match)
    hit = {f.slug: f for f in answer.filters}["condition"]
    assert (hit.value, hit.method, hit.applied) == ("b-u", "alias", True)
    assert hit.param == "f.condition=b-u"


@on
def test_the_vocabulary_rung_asks_again_in_the_catalogues_own_script():
    """«сяоми» must reach «Xiaomi», and only the equivalents get it there.

    Measured on this stand: «сяоми»~«Xiaomi» is 0.738 in the embedding
    space while «сяоми»~«Сом» is 0.856, so the vector rung ranks a fish
    first. The curated group is the difference between the right filter
    and the wrong one.
    """
    asked = []

    def match(name, payload):
        asked.append(payload["text"])
        if payload["text"] == "xiaomi":
            return {"value": "xiaomi", "label": "Xiaomi", "score": 0.99,
                    "method": "exact"}
        return {}

    answer = extract("сяоми", language="ru", plan=PLAN,
                     vector=no_vector, match=match)

    # Asked as typed FIRST, then through the group — never only the alias.
    assert asked[0] == "сяоми"
    assert "xiaomi" in asked

    hit = {f.slug: f for f in answer.filters}["model"]
    assert (hit.value, hit.method, hit.applied) == ("xiaomi", "alias", True)
    # The span is what the reader typed, not the form that matched.
    assert hit.span == (0, 5)


@on
def test_a_latin_query_is_not_asked_twice():
    """No group to apply means no second round trip — the budget is real."""
    asked = []

    def match(name, payload):
        asked.append(payload["text"])
        return {}

    extract("zzzz", language="ru", plan=PLAN, vector=no_vector, match=match)
    assert asked == ["zzzz"]


# ─── a bare numeral is not a filter ──────────────────────────────────────


@on
def test_a_bare_numeral_never_becomes_a_filter_on_its_own():
    """«айфон 17» must not filter on a column that happens to have a 17.

    Measured on a 20k eval corpus: the bare «17» matched at confidence 1.0
    on `screen_diagonal`, `rim_diameter`, `residual_tread` and three more,
    and recall on the query fell from 1.00 to 0.00. A wrong applied filter
    is indistinguishable from an empty catalogue.
    """
    plan = FacetPlan(
        slugs=("screen_diagonal", "rim_diameter"),
        kinds={"screen_diagonal": "term", "rim_diameter": "term"},
        closed_options={"screen_diagonal": ("17",), "rim_diameter": ("17",)},
        option_labels={"screen_diagonal": {"17": "17"}, "rim_diameter": {"17": "17"}},
    )
    answer = extract("айфон 17", language="ru", plan=plan,
                     vector=no_vector, match=no_match)
    assert answer.filters == ()
    # And it is still TEXT, so the engine can still match it.
    assert "17" in answer.residual


@on
def test_the_numeral_still_reaches_a_vocabulary_phrase():
    """The guard refuses a numeral STANDING ALONE, not «айфон 17» as a term."""
    seen = []

    def match(name, payload):
        seen.append(payload["text"])
        if payload["text"].casefold() == "айфон 17":
            return {"value": "iphone-17", "label": "iPhone 17", "score": 0.98,
                    "method": "exact"}
        return {}

    answer = extract("айфон 17", language="ru", plan=PLAN,
                     vector=no_vector, match=match)
    hit = {f.slug: f for f in answer.filters}["model"]
    assert (hit.value, hit.applied) == ("iphone-17", True)
    assert "айфон 17" in seen
