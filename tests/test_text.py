"""The dictionary layer: folding, rewrites, stopwords, synonyms, translit."""
from __future__ import annotations

import pytest

from stapel_search.text import (
    fold,
    lint_dictionary,
    load_dictionary,
    normalize_query,
    transliterate,
)


def test_folding_is_case_and_diacritic_insensitive():
    assert fold("Ünïcödé") == "unicode"
    assert fold("ЁЛКА") == "елка", "e-with-diaeresis folds to e: users type both"
    assert fold("iPhone") == "iphone"


def test_stopwords_are_dropped_from_the_query_only():
    result = normalize_query("продам айфон срочно", "ru")
    assert set(result.dropped_stopwords) == {"продам", "срочно"}
    assert result.flat_terms == ("айфон",)


def test_a_rewrite_may_expand_into_several_words():
    result = normalize_query("стиралка", "ru")
    assert result.flat_terms == ("стиральная", "машина")


def test_curated_equivalents_expand_symmetrically():
    forward = normalize_query("iphone", "ru").terms[0]
    backward = normalize_query("айфон", "ru").terms[0]
    assert "айфон" in forward
    assert "iphone" in backward


def test_transliteration_is_single_script_only():
    assert transliterate("телефон") == "telefon"
    assert transliterate("laptop") == "лаптоп"
    # Mixed scripts are ambiguous, so the algorithm declines rather than guesses.
    assert transliterate("iphone13про") is None


def test_a_curated_term_is_never_additionally_transliterated():
    """Curation beats the algorithm — noise is the cost of transliteration."""
    group = normalize_query("iphone", "ru").terms[0]
    assert "ипхоне" not in group, "the curated group wins outright"


def test_transliteration_is_language_conditioned():
    from django.test import override_settings

    result = normalize_query("ноутбуки", "ru")
    assert any(term.startswith("noutbuk") for term in result.terms[0])

    with override_settings(STAPEL_SEARCH={"TRANSLITERATE": {"ru": False}}):
        off = normalize_query("ноутбуки", "ru")
        assert off.terms[0] == ("ноутбуки",)


def test_the_term_cap_counts_expanded_terms():
    from django.test import override_settings

    with override_settings(STAPEL_SEARCH={"MAX_QUERY_TERMS": 3}):
        result = normalize_query("iphone samsung xiaomi huawei", "ru")
        assert sum(len(group) for group in result.terms) <= 3


def test_normalization_is_deterministic_and_engine_independent():
    a = normalize_query("Айфон Б/У продам", "ru")
    b = normalize_query("айфон б/у ПРОДАМ", "ru")
    assert a.terms == b.terms
    assert a.dropped_stopwords == b.dropped_stopwords


def test_an_unknown_language_normalizes_without_a_dictionary():
    result = normalize_query("hello world", "xx")
    assert result.flat_terms == ("hello", "world")
    assert result.dropped_stopwords == ()


def test_the_shipped_dictionaries_are_clean():
    for language in ("ru", "en"):
        assert lint_dictionary(language) == [], language


def test_the_linter_catches_a_term_in_two_groups():
    from stapel_search.registry import register_dictionary

    register_dictionary(
        "xx",
        {
            "version": 1,
            "equivalents": [["alpha", "beta"], ["beta", "gamma"]],
            "stopwords": [],
            "rewrites": {},
        },
    )
    problems = lint_dictionary("xx")
    assert any("two" not in p and "group" in p for p in problems), problems


def test_the_linter_catches_a_word_that_is_both_stopword_and_synonym():
    from stapel_search.registry import register_dictionary

    register_dictionary(
        "yy",
        {
            "version": 1,
            "equivalents": [["cheap", "budget"]],
            "stopwords": ["cheap"],
            "rewrites": {},
        },
    )
    problems = lint_dictionary("yy")
    assert any("stopword" in p for p in problems), problems


def test_the_linter_catches_a_rewrite_cycle():
    from stapel_search.registry import register_dictionary

    register_dictionary(
        "zz",
        {"version": 1, "equivalents": [], "stopwords": [], "rewrites": {"a": "b", "b": "a"}},
    )
    assert any("cycle" in p for p in lint_dictionary("zz")), lint_dictionary("zz")


def test_a_registered_dictionary_merges_with_the_builtin():
    from stapel_search.registry import register_dictionary

    register_dictionary("ru", {"version": 9, "equivalents": [["велик", "велосипед"]]})
    dictionary = load_dictionary("ru")
    assert dictionary.is_curated("велик")
    assert dictionary.is_curated("айфон"), "the builtin groups survive the merge"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ()),
        ("   ", ()),
        ("!!!", ()),
    ],
)
def test_an_empty_query_is_empty_not_an_error(raw, expected):
    assert normalize_query(raw, "ru").terms == expected
