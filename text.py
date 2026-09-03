"""Query normalization and the dictionary layer — identical for every engine.

The division of labour is a verdict, not a preference (architect §9):

- **this module does dictionary work only** — rewrites, stopword removal,
  symmetric synonym groups, transliteration;
- **morphology belongs to the engine** — the ``russian`` tsvector config,
  Meilisearch's analyzer. Nothing here stems anything.

The conformance suite asserts both backends receive a byte-identical
:class:`~stapel_search.dto.NormalizedQuery`. A divergence at that point is
precisely the seam defect the suite exists to catch, so the normalizer is
one function with no backend parameter.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .dto import NormalizedQuery

logger = logging.getLogger(__name__)

DICTIONARY_DIR = Path(__file__).resolve().parent / "dictionaries"

_TOKEN_RE = re.compile(r"[^\W_]+(?:[/\-][^\W_]+)*", re.UNICODE)

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# --------------------------------------------------------------------------
# folding
# --------------------------------------------------------------------------


def fold(value: str) -> str:
    """Case-fold and strip diacritics — the ``text_plain`` normal form.

    Done in Python rather than through the Postgres ``unaccent`` extension
    so that indexing and querying fold identically on every backend and on
    every deployment, whether or not a DBA installed a contrib module. The
    Cyrillic ``ё`` is folded to ``е`` explicitly: NFD does decompose it, but
    users type both and expect one result set.
    """
    if not value:
        return ""
    lowered = unicodedata.normalize("NFC", value.casefold()).replace("ё", "е")
    decomposed = unicodedata.normalize("NFD", lowered)
    kept: list[str] = []
    base = ""
    for ch in decomposed:
        if unicodedata.combining(ch):
            # Cyrillic diacritics are NOT decoration. NFD decomposes "й"
            # into "и" + breve, and dropping the breve merges two different
            # letters — "мой" would fold into "мои" and a Russian corpus
            # would quietly lose distinctions its readers rely on. Latin
            # diacritics really are decoration for search purposes, so they
            # still go.
            if "Ѐ" <= base <= "ӿ":
                kept.append(ch)
            continue
        base = ch
        kept.append(ch)
    return unicodedata.normalize("NFC", "".join(kept))


def tokenize(value: str) -> list[str]:
    """Split into folded word tokens, keeping in-word ``/`` and ``-``."""
    return [fold(match.group(0)) for match in _TOKEN_RE.finditer(value or "")]


def token_spans(value: str) -> list[tuple[str, int, int]]:
    """:func:`tokenize`, each token with its offsets in the ORIGINAL string.

    Same cut as ``tokenize`` — one regex, one definition of a word — so a
    caller that has to point back at what the reader typed (to underline the
    words a filter came from) cannot drift from the words the normalizer
    actually saw.
    """
    return [
        (fold(match.group(0)), match.start(), match.end())
        for match in _TOKEN_RE.finditer(value or "")
    ]


# --------------------------------------------------------------------------
# transliteration
# --------------------------------------------------------------------------

#: Deterministic, GOST-like. Longest source sequences first so that
#: ``shch``/``щ`` wins over ``sh``/``ш``.
_RU_TO_LAT = (
    ("щ", "shch"), ("ш", "sh"), ("ч", "ch"), ("ц", "ts"), ("ж", "zh"),
    ("ю", "yu"), ("я", "ya"), ("ё", "e"), ("э", "e"), ("й", "y"),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"),
    ("е", "e"), ("з", "z"), ("и", "i"), ("к", "k"), ("л", "l"),
    ("м", "m"), ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"),
    ("с", "s"), ("т", "t"), ("у", "u"), ("ф", "f"), ("х", "h"),
    ("ы", "y"), ("ъ", ""), ("ь", ""),
)

_LAT_TO_RU = (
    ("shch", "щ"), ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("ts", "ц"),
    ("zh", "ж"), ("yu", "ю"), ("ya", "я"), ("ph", "ф"), ("th", "т"),
    ("ck", "к"), ("kh", "х"), ("ee", "и"), ("oo", "у"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"),
    ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"),
    ("z", "з"),
)


def _apply(table: Iterable[tuple[str, str]], term: str) -> str:
    out: list[str] = []
    i = 0
    pairs = list(table)
    while i < len(term):
        for src, dst in pairs:
            if term.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(term[i])
            i += 1
    return "".join(out)


#: Latin vowels, for the one ambiguity the reverse table cannot carry alone.
_LAT_VOWELS = frozenset("aeiou")


def _to_cyrillic(term: str) -> str:
    """Latin -> Cyrillic, with the terminal ``y`` resolved by position.

    GOST sends BOTH ``й`` and ``ы`` to ``y``, so the reverse direction is
    genuinely ambiguous and a single table has to pick one. Picking ``й``
    unconditionally — which is what shipped through 0.6.0 — turns the most
    common shape a Russian noun has when typed in Latin, the plural ``-y``,
    into a word that exists in no corpus: ``shorty`` reached ``шортй`` and
    found nothing, on the SERP and in the dropdown alike, with an empty
    result set as the only symptom.

    Position resolves it, because the two letters do not appear in the same
    place: ``ы`` follows a consonant, ``й`` follows a vowel.

    - ``...yy``  -> ``...ый``   (``krasnyy`` -> ``красный``)
    - ``<cons>y`` -> ``<cons>ы`` (``shorty`` -> ``шорты``)
    - ``<vowel>y`` -> ``<vowel>й`` (``moy`` -> ``мой``)

    Only word-final positions are touched; a medial ``y`` keeps the table's
    ``й`` (``mayka`` -> ``майка``).
    """
    if term.endswith("yy"):
        # Already Cyrillic — `_apply` copies unmapped characters verbatim.
        term = term[:-2] + "ый"
    elif term.endswith("y") and len(term) > 1 and term[-2] not in _LAT_VOWELS:
        term = term[:-1] + "ы"
    return _apply(_LAT_TO_RU, term)


def transliterate(term: str) -> str | None:
    """The single-script counterpart of *term*, or ``None`` if mixed/neither.

    Algorithmic transliteration is noisy, which is why a curated
    ``equivalents`` group always wins: a term that already belongs to a
    group is never additionally transliterated (spec §12).

    One layer, one place. Both the SERP's query normalization and the
    type-ahead's category matching arrive here, because a search box that
    finds one thing while typing and another after Enter is worse than one
    that finds nothing.
    """
    folded = fold(term)
    has_cyrillic = bool(_CYRILLIC_RE.search(folded))
    has_latin = bool(_LATIN_RE.search(folded))
    if has_cyrillic and not has_latin:
        return _apply(_RU_TO_LAT, folded) or None
    if has_latin and not has_cyrillic:
        return _to_cyrillic(folded) or None
    return None


# --------------------------------------------------------------------------
# dictionaries
# --------------------------------------------------------------------------


class Dictionary:
    """The loaded, merged dictionary for one language."""

    __slots__ = ("language", "version", "equivalents", "rewrites", "stopwords", "_group_of")

    def __init__(
        self,
        language: str,
        version: int = 0,
        equivalents: tuple[tuple[str, ...], ...] = (),
        rewrites: dict[str, str] | None = None,
        stopwords: frozenset[str] = frozenset(),
    ) -> None:
        self.language = language
        self.version = version
        self.equivalents = equivalents
        self.rewrites = dict(rewrites or {})
        self.stopwords = stopwords
        self._group_of: dict[str, tuple[str, ...]] = {}
        for group in equivalents:
            for member in group:
                self._group_of[member] = group

    def expansions_for(self, term: str) -> tuple[str, ...]:
        """*term* plus its curated group members, deduplicated, term first."""
        group = self._group_of.get(term)
        if not group:
            return (term,)
        rest = tuple(m for m in group if m != term)
        return (term, *rest)

    def is_curated(self, term: str) -> bool:
        return term in self._group_of


_EMPTY_DICTIONARY = Dictionary("")

_dictionary_cache: dict[str, Dictionary] = {}


def reset_dictionary_cache() -> None:
    """Forget loaded dictionaries (settings changed, tests, lint runs)."""
    _dictionary_cache.clear()


def _load_raw(source: Any) -> dict:
    """One dictionary source -> its raw dict."""
    if isinstance(source, dict):
        return source
    if isinstance(source, (str, Path)):
        text = str(source)
        path = Path(text)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        from django.utils.module_loading import import_string

        value = import_string(text)
        if callable(value) and not isinstance(value, dict):
            value = value()
        if not isinstance(value, dict):
            raise TypeError(f"dictionary source {text!r} did not resolve to a dict")
        return value
    raise TypeError(f"unsupported dictionary source: {source!r}")


def _merge(base: dict, extra: dict) -> dict:
    merged = {
        "version": max(int(base.get("version") or 0), int(extra.get("version") or 0)),
        "equivalents": list(base.get("equivalents") or []) + list(extra.get("equivalents") or []),
        "rewrites": {**(base.get("rewrites") or {}), **(extra.get("rewrites") or {})},
        "stopwords": list(base.get("stopwords") or []) + list(extra.get("stopwords") or []),
    }
    return merged


def load_dictionary(language: str) -> Dictionary:
    """Builtin ``dictionaries/<lang>.json`` merged with registered sources."""
    language = (language or "").lower()
    if language in _dictionary_cache:
        return _dictionary_cache[language]

    raw: dict = {"version": 0, "equivalents": [], "rewrites": {}, "stopwords": []}
    builtin = DICTIONARY_DIR / f"{language}.json"
    if builtin.exists():
        raw = _merge(raw, json.loads(builtin.read_text(encoding="utf-8")))

    from .registry import get_dictionary_sources

    for source in get_dictionary_sources(language):
        raw = _merge(raw, _load_raw(source))

    # Groups fold on the way in, so a curated entry and a user's typing meet
    # in the same normal form.
    groups: list[tuple[str, ...]] = []
    for group in raw["equivalents"]:
        members = tuple(dict.fromkeys(fold(m) for m in group if m))
        if len(members) > 1:
            groups.append(members)

    dictionary = Dictionary(
        language=language,
        version=int(raw.get("version") or 0),
        equivalents=tuple(groups),
        rewrites={fold(k): fold(v) for k, v in (raw.get("rewrites") or {}).items()},
        stopwords=frozenset(fold(w) for w in (raw.get("stopwords") or []) if w),
    )
    _dictionary_cache[language] = dictionary
    return dictionary


def lint_dictionary(language: str, *, max_term_chars: int = 160) -> list[str]:
    """Problems in a language's merged dictionary, as human-readable lines.

    Driven by ``manage.py search_dictionary_lint``: a dictionary is contract
    data, and contract data with a cycle in it silently changes what users
    can find.
    """
    dictionary = load_dictionary(language)
    problems: list[str] = []

    seen: dict[str, int] = {}
    for i, group in enumerate(dictionary.equivalents):
        for member in group:
            if member in seen and seen[member] != i:
                problems.append(
                    f"term {member!r} appears in equivalents group {seen[member]} and {i} — "
                    "expansion would depend on load order"
                )
            seen[member] = i
            if len(member) > max_term_chars:
                problems.append(f"term {member!r} is longer than {max_term_chars} chars")
            if member in dictionary.stopwords:
                problems.append(
                    f"term {member!r} is both a stopword and an equivalents member — "
                    "it would be removed before it could expand"
                )

    for src, dst in dictionary.rewrites.items():
        hops = 0
        current = dst
        while current in dictionary.rewrites and hops < 10:
            current = dictionary.rewrites[current]
            hops += 1
            if current == src:
                problems.append(f"rewrite cycle involving {src!r}")
                break
        if src in dictionary.stopwords:
            problems.append(f"rewrite source {src!r} is also a stopword — the rewrite is dead")
    return problems


# --------------------------------------------------------------------------
# the normalizer
# --------------------------------------------------------------------------


def normalize_query(q: str, lang: str = "") -> NormalizedQuery:
    """Dictionary-normalize *q* for *lang*.

    Order matters and is fixed: fold -> tokenize -> rewrite -> drop
    stopwords -> expand curated groups -> transliterate the terms no group
    claimed. Stopwords are removed from the QUERY only; they are never
    removed from the index, because a corpus indexed without them can only
    be repaired by reindexing everything.
    """
    from .conf import search_settings

    dictionary = load_dictionary(lang) if lang else _EMPTY_DICTIONARY
    translit_on = bool((search_settings.TRANSLITERATE or {}).get(lang, False))
    max_terms = int(search_settings.MAX_QUERY_TERMS)

    tokens: list[str] = []
    for token in tokenize(q):
        rewritten = dictionary.rewrites.get(token, token)
        # A rewrite may expand into several words ("стиралка" -> "стиральная
        # машина"), so re-tokenize its result rather than trusting it to be
        # one term.
        tokens.extend(tokenize(rewritten) if rewritten != token else [token])

    dropped: list[str] = []
    terms: list[tuple[str, ...]] = []
    for token in tokens:
        if token in dictionary.stopwords:
            dropped.append(token)
            continue
        expansions = list(dictionary.expansions_for(token))
        if translit_on and not dictionary.is_curated(token):
            other = transliterate(token)
            if other and other not in expansions:
                expansions.append(other)
        terms.append(tuple(expansions))

    # The cap counts EXPANDED terms: a three-word query against a dictionary
    # with big groups is what actually reaches the engine.
    total = sum(len(group) for group in terms)
    if total > max_terms:
        kept: list[tuple[str, ...]] = []
        budget = max_terms
        for group in terms:
            if len(group) > budget:
                if budget > 0:
                    kept.append(group[:budget])
                    budget = 0
                break
            kept.append(group)
            budget -= len(group)
        terms = kept

    return NormalizedQuery(
        raw=q or "",
        terms=tuple(terms),
        dropped_stopwords=tuple(dropped),
    )


def index_text(title: str, text_extra: str, body: str) -> str:
    """The ``text_plain`` value: everything, folded, one space-joined string."""
    return " ".join(part for part in (fold(title), fold(text_extra), fold(body)) if part)


__all__ = [
    "DICTIONARY_DIR",
    "Dictionary",
    "fold",
    "index_text",
    "lint_dictionary",
    "load_dictionary",
    "normalize_query",
    "reset_dictionary_cache",
    "tokenize",
    "transliterate",
]
