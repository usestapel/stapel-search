"""stapel-search contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{search + core}`` Django instance mounted at the canonical
``/search/api/v1`` prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only
  docs/flows.json    generate_flow_docs machine artifact
  docs/errors.json   generate_error_keys registry

Two further artifacts are this module's own, and they are generated from
the code that implements them rather than written alongside it:

  docs/index.json    INDEX_FIELDS — the index contract as data (spec §11)
  docs/ranking.json  the P2B Art. 5 ranking disclosure (spec §10)

Usage:
    python -m stapel_search._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_search._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_search.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before
    # configure() ran). SCHEMA_PATH_PREFIX left None makes drf derive the
    # operationId prefix from the common path of all endpoints — "/" in an
    # aggregate but "/search/api" here, which would strip it to anonymous
    # names. Pin it to the aggregate convention.
    from drf_spectacular.settings import spectacular_settings

    from stapel_search._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort if not on the pinned interpreter.

    drf-spectacular renders component descriptions differently across
    Python minors, so emitting on another one produces false diffs against
    the committed docs/*.json forever.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-search contract emission ABORTED: running Python {got}, but "
            "contracts must be emitted on Python 3.12 (the CI/monolith pin)."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-search-contract",
        description="Emit this module's contract artifacts into --out.",
    )
    parser.add_argument("--out", default="docs")
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    from stapel_search.index_schema import write_index_json
    from stapel_search.scoring import write_ranking_json

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")
    write_index_json(out / "index.json")
    write_ranking_json(out / "ranking.json")

    from stapel_search.index_schema import INDEX_FIELDS
    from stapel_search.registry import get_scorers

    print(
        f"stapel-search contract: {paths} paths, {flows} flows, {errors} error keys, "
        f"{len(INDEX_FIELDS)} index fields, {len(get_scorers())} scorers -> {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
