"""Query understanding: what a query is ABOUT, beside the words it is made of.

«красные штаны» is not two words to match — it is a colour filter and one
word of text. This module turns the first half into
:class:`~stapel_search.dto.ExtractedFilter` rows and hands the second half
back as ``residual``, and it does so against the option space of the
RESOLVED category: a global scan over 1225 vocabulary-backed features and
815k terms is not a query-path operation, and the plan already narrows the
space to the axes this category actually has.

Four rungs, strongest first, one slug claimed at most once:

1. **exact** — a folded token equals a folded option caption, or equals an
   option code. On the live stand an inline ``select`` writes
   ``{"label": "Красный", "value": "krasnyy"}``, so the code IS the
   transliterated slug of the caption and the singular lands here.
2. **translit** — the token transliterates onto the code. This is what
   answers a plan whose captions never arrived and a reader typing the
   other script.
3. **vocabulary** — a ``ref_select`` addresses a level nobody may
   enumerate, so the term is resolved through the comm Function named by
   ``UNDERSTANDING_MATCH_FUNCTION``, which runs stapel-vocabularies' own
   exact/prefix/vector ladder. Reused, never reimplemented here.
4. **vector** — morphology. ``text.py`` stems nothing by design (architect
   §9: stemming belongs to the engine), so «красные» does not fold onto
   «красный» and no table maps it there either. The embedding space does.

Two disciplines hold the rung honest. A hit whose similarity the provider
did not STATE is refused outright — an invented number would make a filter
that silently narrows the answer, and a wrong applied filter is
indistinguishable from an empty catalogue. And a hit below
``UNDERSTANDING_VECTOR_FLOOR`` is not dropped either: it survives as a
SIGNAL (``applied=False``) that ranks through ``Hit.match_count`` without
excluding a row.

Nothing here may raise on the query path. Every seam failure — the vector
layer off, no comm responder, a provider that throws — appends a marker to
``Extraction.degraded`` and the remaining rungs carry on, the same contract
the rest of this module keeps (``vector_suggestions``, ``category_names``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .dto import ExtractedFilter, Extraction
from .text import fold, token_spans, transliterate

logger = logging.getLogger(__name__)

#: Vector corpus of feature OPTIONS — captions a reader might type toward,
#: with ``payload {"slug", "value"}`` (or a ``slug=value`` key). Registered
#: in ``VECTOR_CORPORA`` by the composite that knows the category schema,
#: the ``SOURCES`` precedent: this module names the kind and knows nothing
#: about who fills it.
OPTION_CORPUS_KIND = "facet_option"

#: Grades :class:`ExtractedFilter` documents. A vocabulary provider that
#: reports something else (``prefix``, ``trigram``) is not lying, but this
#: module cannot vouch for the grade, so it collapses to the soft one.
_METHODS = frozenset({"exact", "translit", "alias", "vector"})

#: Longest term the vocabulary rung will try as one phrase. Phone models
#: run to three words («айфон 17 про»); beyond that the window costs more
#: comm round trips than it earns.
_MATCH_WINDOW = 3
#: Hard ceiling on vocabulary round trips per query — a query is not a form.
_MATCH_CALL_BUDGET = 8
#: Hard ceiling on tokens probed against the embedding space. Each probe is
#: an embedding round trip on a cold cache (``vector.service.embed_query``).
_VECTOR_PROBE_LIMIT = 6

_VECTOR_DEGRADED = "understanding_vector"
_MATCH_DEGRADED = "understanding_match"


@dataclass(frozen=True)
class _Token:
    """One word of the RAW query, with the offsets a UI underlines."""

    raw: str
    folded: str
    start: int
    end: int


@dataclass
class _Candidate:
    slug: str
    value: str
    value_label: str
    method: str
    confidence: float
    first: int
    last: int


def extract(
    q: str,
    *,
    language: str,
    plan: Any,
    category_path: tuple[str, ...] = (),
    vector: Callable[..., tuple[list[dict], str | None]] | None = None,
    match: Callable[[str, dict], Any] | None = None,
) -> Extraction:
    """What *q* turns into against *plan*'s option space.

    *vector* and *match* are the two seams, injected: they default to
    ``vector.service.similar`` and the fleet's comm ``call``, which is what
    lets the ladder be tested without an embedder, a bus or a database.

    With ``QUERY_UNDERSTANDING`` off the answer is an EMPTY extraction —
    no filters and no residual, so a caller that forgot the flag falls back
    to the query it already has rather than searching for nothing.
    """
    from .conf import search_settings

    if not _truthy(search_settings.QUERY_UNDERSTANDING):
        return Extraction()
    tokens = _tokens(q)
    if not tokens:
        return Extraction(category_path=tuple(category_path))

    options = _option_space(plan)
    degraded: list[str] = []
    claimed_slugs: set[str] = set()
    claimed_tokens: set[int] = set()
    found: list[_Candidate] = []

    # Rung-major, not token-major: a translit accident must never take a
    # token a later exact hit would have claimed outright.
    for rung in (_exact_rung, _translit_rung):
        for candidate in rung(tokens, options, claimed_slugs, claimed_tokens):
            _claim(candidate, found, claimed_slugs, claimed_tokens)

    for candidate in _alias_rung(
        tokens, options, claimed_slugs, claimed_tokens, language
    ):
        _claim(candidate, found, claimed_slugs, claimed_tokens)

    for candidate in _vocabulary_rung(
        q, tokens, plan, claimed_slugs, claimed_tokens, language, match, degraded
    ):
        _claim(candidate, found, claimed_slugs, claimed_tokens)

    for candidate in _vector_rung(
        tokens, options, claimed_slugs, claimed_tokens, language, vector, degraded
    ):
        _claim(candidate, found, claimed_slugs, claimed_tokens)

    filters = _finalize(found, tokens)
    return Extraction(
        filters=filters,
        # The scope, echoed so an answer can say what the extraction was
        # read against. This module does not RESOLVE a category — the
        # caller does, before it can build a plan at all — so it states no
        # confidence in one.
        category_path=tuple(category_path),
        residual=_residual(q, found, tokens),
        degraded=tuple(degraded),
    )


# --------------------------------------------------------------------------
# the query, cut up
# --------------------------------------------------------------------------


def _tokens(q: str) -> list[_Token]:
    """Words of *q* with raw offsets — the same cut ``text.tokenize`` makes.

    The regex is shared rather than copied: a module that split differently
    from the normalizer would underline words the engine never saw.
    """
    return [
        _Token((q or "")[start:end], folded, start, end)
        for folded, start, end in token_spans(q or "")
    ]


def _is_bare_number(token: _Token) -> bool:
    """A token that is only digits — never a filter by itself.

    Measured on a 20k-listing eval corpus: «айфон 17» sent the bare «17» into
    the option space and it matched, at confidence 1.0, on
    ``screen_diagonal``, ``rim_diameter``, ``residual_tread`` and three more
    — six columns that have a value called 17 and nothing to do with a phone.
    Recall on that query went from 1.00 to 0.00, which is the whole failure
    mode in one number: a wrong applied filter is indistinguishable from an
    empty catalogue.

    A numeral is meaningful only next to the word it qualifies, so it stays
    available to the vocabulary rung — «айфон 17» is still resolved as a
    PHRASE — and is refused only as a filter standing on its own.
    """
    return token.folded.isdigit()


def _option_space(plan: Any) -> dict[str, dict[str, str]]:
    """``{slug: {value: caption}}`` for the plan's CLOSED sets.

    Closed is the whole qualification: an open set's value is whatever a
    seller typed, and matching a query word against that is how a filter
    starts narrowing an answer on a coincidence. Captions come from the
    plan too, so a slug whose host never threaded the schema through still
    answers on its codes.
    """
    closed = getattr(plan, "closed_options", None) or {}
    labels = getattr(plan, "option_labels", None) or {}
    hidden = set(getattr(plan, "hidden", None) or ())
    space: dict[str, dict[str, str]] = {}
    for slug, values in closed.items():
        if slug in hidden or not values:
            continue
        captions = labels.get(slug) or {}
        space[str(slug)] = {
            str(value): str(captions.get(value, "")) for value in values
        }
    return space


# --------------------------------------------------------------------------
# the rungs
# --------------------------------------------------------------------------


def _exact_rung(
    tokens: list[_Token],
    options: dict[str, dict[str, str]],
    claimed_slugs: set[str],
    claimed_tokens: set[int],
):
    from .conf import search_settings

    confidence = float(search_settings.UNDERSTANDING_EXACT_CONFIDENCE)
    for index, token in enumerate(tokens):
        if index in claimed_tokens or _is_bare_number(token):
            continue
        for slug, values in options.items():
            if slug in claimed_slugs:
                continue
            for value, caption in values.items():
                if token.folded == fold(value) or (
                    caption and token.folded == fold(caption)
                ):
                    yield _Candidate(
                        slug, value, caption, "exact", confidence, index, index
                    )
                    break


def _translit_rung(
    tokens: list[_Token],
    options: dict[str, dict[str, str]],
    claimed_slugs: set[str],
    claimed_tokens: set[int],
):
    """The single-script counterpart of a token against the option CODE.

    Codes are written in one script and readers type in the other, and an
    option code IS the transliterated slug of its caption on this catalogue
    («Красный» / ``krasnyy``), so a letter table reaches it exactly.

    A letter table and nothing more: :func:`text.transliterate` is GOST-like
    and knows no brands — «сяоми» comes out ``syaomi``, never ``xiaomi``.
    That case belongs to :func:`_alias_rung` below, which reads the curated
    equivalents.
    """
    from .conf import search_settings

    confidence = float(search_settings.UNDERSTANDING_TRANSLIT_CONFIDENCE)
    for index, token in enumerate(tokens):
        if index in claimed_tokens or _is_bare_number(token):
            continue
        counterpart = transliterate(token.raw)
        if not counterpart:
            continue
        for slug, values in options.items():
            if slug in claimed_slugs:
                continue
            for value, caption in values.items():
                if counterpart == fold(value):
                    yield _Candidate(
                        slug, value, caption, "translit", confidence, index, index
                    )
                    break


def _alias_rung(
    tokens: list[_Token],
    options: dict[str, dict[str, str]],
    claimed_slugs: set[str],
    claimed_tokens: set[int],
    language: str,
):
    """The curated equivalents — the only rung that reaches a phonetic brand.

    A Russian buyer types a brand as it SOUNDS, and neither a letter table
    nor the embedding space gets there. Measured on this stand's own corpus
    (LaBSE, 73,664 vocabulary labels): «сяоми»~«Xiaomi» is 0.738 while
    «сяоми»~«Сом» — a fish — is 0.856, so the vector rung ranks the wrong
    answer FIRST and no floor can separate them. «бош»~«Bosch» is 0.658.
    The dictionary already carries these groups for exactly this reason;
    this rung is what lets extraction read them.

    Confidence is the translit confidence, not 1.0: an equivalents group is
    curated by a human and exact within itself, but it is a claim about
    language rather than a string comparison.
    """
    from .conf import search_settings
    from .text import load_dictionary

    confidence = float(search_settings.UNDERSTANDING_TRANSLIT_CONFIDENCE)
    try:
        dictionary = load_dictionary(language)
    except Exception:  # a missing/broken dictionary must not fail a query
        logger.debug("no dictionary for %r; alias rung skipped", language)
        return

    for index, token in enumerate(tokens):
        if index in claimed_tokens or _is_bare_number(token):
            continue
        expansions = {fold(form) for form in dictionary.expansions_for(token.folded)}
        # `expansions_for` echoes the term itself; the exact rung already had
        # that one, so an echo alone is not an alias hit.
        expansions.discard(token.folded)
        if not expansions:
            continue
        for slug, values in options.items():
            if slug in claimed_slugs:
                continue
            hit = None
            for value, caption in values.items():
                if fold(value) in expansions or (
                    caption and fold(caption) in expansions
                ):
                    hit = (value, caption)
                    break
            if hit is not None:
                yield _Candidate(
                    slug, hit[0], hit[1], "alias", confidence, index, index
                )
                break


def _vocabulary_rung(
    q: str,
    tokens: list[_Token],
    plan: Any,
    claimed_slugs: set[str],
    claimed_tokens: set[int],
    language: str,
    match: Callable[[str, dict], Any] | None,
    degraded: list[str],
):
    """Resolve unclaimed phrases inside the plan's vocabulary levels.

    A level holds tens of thousands of terms and the plan holds only its
    ADDRESS (``vocabulary_refs``), which is exactly why the term is sent
    out to be resolved instead of pulled in to be scanned. Phrases are
    tried longest-first so «айфон 17» beats «айфон».

    Every phrase is tried as TYPED and again through the curated
    equivalents, because this is where brands live — `brand` and `model`
    are `ref_select` on this catalogue, all 704 of them — and a phonetic
    brand reaches its term by no other route. Measured here: «сяоми» is
    0.738 from «Xiaomi» in the embedding space and 0.856 from «Сом», so
    the alias is not an optimisation, it is the difference between the
    right filter and a fish.
    """
    from .conf import search_settings

    refs = getattr(plan, "vocabulary_refs", None) or {}
    wanted = [slug for slug in refs if slug not in claimed_slugs]
    if not wanted:
        return
    seeker = match or _comm_match
    floor = float(search_settings.UNDERSTANDING_VECTOR_FLOOR)
    name = str(search_settings.UNDERSTANDING_MATCH_FUNCTION)
    budget = _MATCH_CALL_BUDGET

    for slug in wanted:
        vocabulary, level = refs[slug]
        found = False
        for first, last in _phrases(tokens, claimed_tokens):
            if found or budget <= 0:
                break
            typed = q[tokens[first].start : tokens[last].end]
            for text, via_alias in _phrase_forms(tokens, first, last, typed, language):
                if budget <= 0:
                    return
                budget -= 1
                try:
                    answer = seeker(
                        name,
                        {
                            "vocabulary": vocabulary,
                            "level": level,
                            "text": text,
                            "language": language,
                            "min_score": floor,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - query path never raises
                    logger.warning("%s unavailable: %s", name, exc)
                    _degrade(degraded, _MATCH_DEGRADED)
                    return
                hit = _read_match(answer)
                if not hit:
                    continue
                value = _first_string(hit, ("value", "code", "key", "slug"))
                score = _score(hit)
                if not value:
                    continue
                if score is None:
                    # Same refusal as the vector rung: a filter this module
                    # cannot put a number on is a filter it cannot defend.
                    logger.warning(
                        "%s answered a hit with no score — refusing it", name
                    )
                    _degrade(degraded, _MATCH_DEGRADED)
                    continue
                stated = str(hit.get("method") or "").strip().lower()
                method = stated if stated in _METHODS else "vector"
                # The span is what the reader TYPED, whichever form matched.
                yield _Candidate(
                    slug,
                    value,
                    _first_string(hit, ("label", "name", "text")),
                    "alias" if via_alias else method,
                    score,
                    first,
                    last,
                )
                found = True
                break


def _phrase_forms(
    tokens: list[_Token], first: int, last: int, typed: str, language: str
) -> list[tuple[str, bool]]:
    """The phrase as typed, then once more through the curated equivalents.

    Each token is swapped for the group member written in the OTHER script,
    so «сяоми редми» is also asked as «xiaomi redmi». At most one rewrite:
    the point is to reach the catalogue's spelling, not to enumerate a
    group's cross product.
    """
    from .text import load_dictionary

    forms: list[tuple[str, bool]] = [(typed, False)]
    try:
        dictionary = load_dictionary(language)
    except Exception:  # noqa: BLE001 - a missing dictionary is not an error here
        return forms

    swapped, changed = [], False
    for token in tokens[first : last + 1]:
        alias = _latin_alias(dictionary, token.folded)
        if alias:
            swapped.append(alias)
            changed = True
        else:
            swapped.append(token.folded)
    if changed:
        forms.append((" ".join(swapped), True))
    return forms


def _latin_alias(dictionary: Any, term: str) -> str:
    """The Latin-script member of *term*'s equivalents group, if any.

    Catalogue terms are written the way the vendor writes them — «Xiaomi»,
    «Timberland» — so the Latin member is the one a vocabulary can match.
    """
    if term.isascii():
        return ""
    for form in dictionary.expansions_for(term):
        folded = fold(form)
        if folded != term and folded.isascii():
            return folded
    return ""


def _vector_rung(
    tokens: list[_Token],
    options: dict[str, dict[str, str]],
    claimed_slugs: set[str],
    claimed_tokens: set[int],
    language: str,
    vector: Callable[..., tuple[list[dict], str | None]] | None,
    degraded: list[str],
):
    """The morphology net, over tokens nothing deterministic claimed.

    The provider's own floor admits a neighbour; ``UNDERSTANDING_VECTOR_FLOOR``
    decides only whether it FILTERS. Passing the high floor down instead
    would delete the signal population the eval measures.
    """
    unclaimed = [
        index
        for index in range(len(tokens))
        if index not in claimed_tokens and not _is_bare_number(tokens[index])
    ][:_VECTOR_PROBE_LIMIT]
    if not unclaimed or not options:
        return
    seeker = vector
    if seeker is None:
        from .vector.service import enabled, similar

        if not enabled():
            # The rung had work and could not do it. Silence here is what
            # makes a half-understood query look fully understood.
            _degrade(degraded, _VECTOR_DEGRADED)
            return
        seeker = similar

    for index in unclaimed:
        if len(claimed_slugs) >= len(options):
            return
        try:
            hits, shortfall = seeker(
                OPTION_CORPUS_KIND,
                tokens[index].raw,
                language=language,
                limit=_VECTOR_PROBE_LIMIT,
                floor=None,
            )
        except Exception as exc:  # noqa: BLE001 - the query path never raises
            logger.warning("vector option lookup failed: %s", exc)
            _degrade(degraded, _VECTOR_DEGRADED)
            return
        if shortfall:
            _degrade(degraded, _VECTOR_DEGRADED)
            return
        for hit in hits or []:
            resolved = _resolve_option(hit, options)
            if not resolved:
                continue
            slug, value = resolved
            if slug in claimed_slugs:
                continue
            score = _score(hit)
            if score is None:
                logger.warning(
                    "vector option hit stated no similarity — refusing it"
                )
                _degrade(degraded, _VECTOR_DEGRADED)
                continue
            yield _Candidate(
                slug, value, options[slug].get(value, ""), "vector", score, index, index
            )
            break


# --------------------------------------------------------------------------
# seams
# --------------------------------------------------------------------------


def _comm_match(name: str, payload: dict) -> Any:
    from stapel_core.comm import call

    return call(name, payload)


def _resolve_option(
    hit: Any, options: dict[str, dict[str, str]]
) -> tuple[str, str] | None:
    """``(slug, value)`` a corpus hit addresses, INSIDE the resolved plan.

    Three shapes are read, in falling order of explicitness: a payload
    naming both, a ``slug=value`` key (the facet term form this module
    counts in), and a bare value that only one slug in the plan owns. A hit
    the plan does not own is not an error — the corpus is global and the
    option space is this category's.
    """
    if not isinstance(hit, dict):
        return None
    payload = hit.get("payload") or {}
    slug = str(payload.get("slug") or "") if isinstance(payload, dict) else ""
    value = str(payload.get("value") or "") if isinstance(payload, dict) else ""
    if not (slug and value):
        key = str(hit.get("key") or "")
        if "=" in key:
            slug, _, value = key.partition("=")
        elif key:
            value = key
    if not value:
        return None
    if slug:
        return (slug, value) if value in options.get(slug, {}) else None
    owners = [name for name, values in options.items() if value in values]
    return (owners[0], value) if len(owners) == 1 else None


def _read_match(answer: Any) -> dict | None:
    """The one hit inside a ``vocabularies.match`` answer, shape-tolerantly."""
    if not isinstance(answer, dict):
        return None
    hit = answer.get("match")
    if hit is None:
        candidates = answer.get("matches") or answer.get("results")
        hit = candidates[0] if isinstance(candidates, list) and candidates else None
    if hit is None and any(k in answer for k in ("value", "code", "key")):
        hit = answer
    return hit if isinstance(hit, dict) else None


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def _claim(
    candidate: _Candidate,
    found: list[_Candidate],
    claimed_slugs: set[str],
    claimed_tokens: set[int],
) -> None:
    if candidate.slug in claimed_slugs:
        return
    if any(index in claimed_tokens for index in range(candidate.first, candidate.last + 1)):
        return
    claimed_slugs.add(candidate.slug)
    claimed_tokens.update(range(candidate.first, candidate.last + 1))
    found.append(candidate)


def _finalize(
    found: list[_Candidate], tokens: list[_Token]
) -> tuple[ExtractedFilter, ...]:
    """Chips in query order, with the cap spent on the strongest first.

    ``UNDERSTANDING_MAX_FILTERS`` bounds what NARROWS the answer, not what
    was understood: past the cap a chip stays, demoted to a signal. Spending
    the budget by confidence rather than by word order is the difference
    between demoting the weakest guess and demoting the last word typed.
    """
    from .conf import search_settings

    cap = int(search_settings.UNDERSTANDING_MAX_FILTERS)
    floor = float(search_settings.UNDERSTANDING_VECTOR_FLOOR)
    ranked = sorted(found, key=lambda c: (-c.confidence, c.first))
    applied: set[int] = set()
    for candidate in ranked:
        if len(applied) >= cap:
            break
        if candidate.confidence >= floor:
            applied.add(id(candidate))
    return tuple(
        ExtractedFilter(
            slug=candidate.slug,
            value=candidate.value,
            value_label=candidate.value_label,
            method=candidate.method,
            confidence=candidate.confidence,
            span=(tokens[candidate.first].start, tokens[candidate.last].end),
            param=f"f.{candidate.slug}={candidate.value}",
            applied=id(candidate) in applied,
        )
        for candidate in sorted(found, key=lambda c: c.first)
    )


def _residual(q: str, found: list[_Candidate], tokens: list[_Token]) -> str:
    """*q* minus every extracted span, whitespace collapsed.

    Signals are cut out too: a word that became a chip has been read, and
    leaving it in the text arm makes the engine match it a second time.
    """
    if not found:
        return " ".join((q or "").split())
    spans = sorted(
        (tokens[c.first].start, tokens[c.last].end) for c in found
    )
    kept: list[str] = []
    cursor = 0
    for start, end in spans:
        kept.append(q[cursor:start])
        cursor = end
    kept.append(q[cursor:])
    return " ".join("".join(kept).split())


def _phrases(tokens: list[_Token], claimed: set[int]):
    """Unclaimed contiguous ``(first, last)`` windows, longest first."""
    runs: list[list[int]] = []
    current: list[int] = []
    for index in range(len(tokens)):
        if index in claimed:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(index)
    if current:
        runs.append(current)
    windows: list[tuple[int, int]] = []
    for run in runs:
        for size in range(min(_MATCH_WINDOW, len(run)), 0, -1):
            for offset in range(0, len(run) - size + 1):
                windows.append((run[offset], run[offset + size - 1]))
    return windows


def _score(hit: dict) -> float | None:
    """The similarity a provider STATED, or ``None`` — never a stand-in."""
    for key in ("similarity", "score", "confidence"):
        value = hit.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value))
        except (TypeError, ValueError):
            continue
    return None


def _first_string(hit: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _degrade(degraded: list[str], marker: str) -> None:
    if marker not in degraded:
        degraded.append(marker)


def _truthy(flag: Any) -> bool:
    if isinstance(flag, str):
        return flag.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(flag)


__all__ = ["OPTION_CORPUS_KIND", "extract"]
