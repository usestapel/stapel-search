"""Ranking disclosure, generated from the registry that does the ranking.

P2B Art. 5 obliges a marketplace to describe the main parameters
determining ranking. The usual way that obligation is met is a paragraph
copied into terms of service, which drifts away from the code the first
time somebody changes a weight. Here the disclosure is *rendered from
:data:`stapel_search.registry.BUILTIN_SCORERS` and its overlays*, served at
``GET /search/api/v1/ranking`` and emitted to ``docs/ranking.json`` under
the same drift gate as every other contract artifact. It cannot disagree
with the behaviour, because it is the behaviour, formatted.

Two honesty rules the generator enforces:

- a scorer the configured backend does not support is listed with
  ``active: false`` — a disclosure that lies about which parameters are in
  effect is worse than none;
- promotion is called promotion. ``promoted`` is on every result item under
  every sort (DSA Art. 26), and the disclosure states that an explicit sort
  receives no promotional boost, which is a module invariant rather than a
  setting.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

#: The English source text for each scorer's ``description_key``. Real
#: rendering goes through i18n; this is the fallback and the artifact text.
SCORER_DESCRIPTIONS = {
    "search.scorer.relevance": (
        "Textual match between the query and the listing's title, attribute "
        "chips and description, with the title weighted highest."
    ),
    "search.scorer.freshness": (
        "Recency of publication: a listing's contribution halves every "
        "half_life_days."
    ),
    "search.scorer.geo": (
        "Proximity to the searched location, decaying linearly to zero at "
        "max_radius_km. Applies only when a location was given."
    ),
    "search.scorer.promotion": (
        "Paid or editorial promotion. Applies ONLY to relevance ordering; "
        "choosing any explicit sort (price, date, distance) removes it "
        "entirely. Every promoted result is marked promoted: true."
    ),
    "search.scorer.popularity": (
        "Engagement with the listing, as reported by the host's popularity "
        "signal."
    ),
}

#: Stated in the disclosure verbatim, because they are the parts a reader
#: most needs and a weights table cannot express.
DISCLOSURE_NOTES = (
    "Sorting other than 'relevance' applies no promotional boost. This is a "
    "property of the module, not a configuration option: a scorer declares "
    "the sorts it applies to, and the promotion scorer declares only "
    "relevance.",
    "Every result item carries a 'promoted' flag under every sort, including "
    "when it is false (DSA Art. 26).",
    "No personalization and no machine-learned ranking is used. Ranking does "
    "not depend on who is asking.",
    "No query log is kept, so nothing a searcher typed before affects what "
    "they see now.",
    "Paying for promotion changes only the promotion component below, and "
    "never the textual relevance of a listing to a query.",
)


def ranking_disclosure(doc_type: str = "", *, backend_name: str = "", supported=None) -> dict:
    """The disclosure document for *doc_type*.

    *supported* is the configured backend's ``supported_scorers``; scorers
    outside it are reported as inactive rather than silently listed as
    though they were doing something.
    """
    from .registry import get_scorers

    scorers = get_scorers()
    supported_set = None if supported is None else set(supported)
    entries = []
    for slug in sorted(scorers):
        scorer = scorers[slug]
        active = supported_set is None or slug in supported_set
        entries.append(
            {
                "slug": scorer.slug,
                "weight": scorer.weight,
                "description_key": scorer.description_key,
                "description": SCORER_DESCRIPTIONS.get(scorer.description_key, ""),
                "params": dict(scorer.params),
                "applies_to_sorts": sorted(scorer.applies_to_sorts),
                "active": active,
                "inactive_reason": (
                    "" if active else f"the configured backend {backend_name!r} cannot evaluate it"
                ),
            }
        )
    return {
        "doc_type": doc_type,
        "backend": backend_name,
        "scorers": entries,
        "notes": list(DISCLOSURE_NOTES),
    }


def render_ranking_json() -> str:
    """Deterministic text of ``docs/ranking.json``.

    Emitted without a backend so the artifact describes the *registry*, not
    one deployment's engine: ``active`` there is always true, and the live
    endpoint is what tells a caller what this particular deployment runs.
    """
    document = ranking_disclosure(doc_type="", backend_name="")
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_ranking_json(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_ranking_json(), encoding="utf-8")
    return target


def active_scorers(sort: str) -> tuple:
    """Scorers that apply to *sort* — the structural form of the invariant."""
    from .registry import get_scorers

    return tuple(
        scorer for scorer in get_scorers().values() if sort in scorer.applies_to_sorts
    )


def scorer_slugs_for(sort: str) -> tuple[str, ...]:
    return tuple(sorted(scorer.slug for scorer in active_scorers(sort)))


def as_dicts() -> list[dict]:
    from .registry import get_scorers

    return [asdict(scorer) for scorer in get_scorers().values()]


__all__ = [
    "DISCLOSURE_NOTES",
    "SCORER_DESCRIPTIONS",
    "active_scorers",
    "as_dicts",
    "ranking_disclosure",
    "render_ranking_json",
    "scorer_slugs_for",
    "write_ranking_json",
]
