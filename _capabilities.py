"""stapel-search capabilities.json emitter — a shim over stapel_tools."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_search._codegen import _configure

    _configure()
    from stapel_search.conf import DEFAULTS
    from stapel_search.urls_v1 import GATE_REGISTRY

    # The CTO-facing axes are the ones that change what the PRODUCT is
    # allowed to promise, not how fast it runs. Which engine answers
    # (BACKEND) decides whether typo tolerance, exact totals and exact
    # facet counts exist at all. Which corpora are indexed (SOURCES) is the
    # difference between a search box and an empty page. Which sorts are
    # offered (SORTS) is a product surface. The candidate cap and the result
    # window change the ANSWER a user gets — approximate counts, and a
    # refusal to page deeper — so they are axes too, not tuning.
    #
    # Deliberately NOT axes: throttle rates, page sizes, cache TTLs, batch
    # sizes and the trigram threshold. They bound cost and abuse; they do
    # not change the deal.
    axes = {
        "BACKEND",
        "SOURCES",
        "SORTS",
        "FACET_CANDIDATE_CAP",
        "MAX_RESULT_WINDOW",
        "MAX_FACET_FIELDS",
        "TRANSLITERATE",
        "ACCEPT_FEATURES_SEARCH",
    }
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/search/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k in axes,
        axis_group=axis_group_rules(
            exact={
                "BACKEND": "search.engine",
                "SOURCES": "search.corpus",
                "SORTS": "search.ranking",
                "FACET_CANDIDATE_CAP": "search.facets",
                "MAX_FACET_FIELDS": "search.facets",
                "MAX_RESULT_WINDOW": "search.paging",
                "TRANSLITERATE": "search.text",
                "ACCEPT_FEATURES_SEARCH": "search.corpus",
            }
        ),
        prog="stapel-search-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
