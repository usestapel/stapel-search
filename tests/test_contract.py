"""The contract artifacts and the public API pin.

Seven documents ship in the wheel: the quintet plus this module's own two,
``index.json`` (the index contract as data) and ``ranking.json`` (the P2B
disclosure). All seven are under one drift gate, which is the whole point —
a compliance text generated from the code that implements it cannot disagree
with the behaviour, and an index field cannot be added without its row.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"


def _load(name):
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


# --- the artifacts exist and ship ------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["schema.json", "flows.json", "errors.json", "capabilities.json",
     "index.json", "ranking.json", "llms.txt"],
)
def test_the_artifact_is_committed(name):
    assert (DOCS / name).is_file(), f"docs/{name} is missing"


@pytest.mark.parametrize(
    "name",
    ["docs/schema.json", "docs/flows.json", "docs/errors.json",
     "docs/capabilities.json", "docs/index.json", "docs/ranking.json",
     "docs/llms.txt", "CONFIG.MD", "dictionaries/*.json", "translations/*.json"],
)
def test_the_artifact_ships_in_the_wheel(name):
    """Both stapel-docs packaging gotchas, closed by construction."""
    import tomllib

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]["stapel_search"]
    assert name in package_data, f"{name} is not listed in package-data"


# --- index.json -------------------------------------------------------------


def test_index_json_matches_the_declaration():
    from stapel_search.index_schema import render_index_json

    assert (DOCS / "index.json").read_text(encoding="utf-8") == render_index_json()


def test_every_index_field_has_a_source_a_read_path_and_a_test():
    document = _load("index.json")
    assert document["fields"]
    for field in document["fields"]:
        assert field["source"], field["field"]
        assert field["read_paths"], field["field"]
        assert field["test"], field["field"]
        assert field["proves"], field["field"]


def test_a_field_cannot_be_declared_without_a_read_path():
    """The dataclass refuses it, so "for later" is not expressible."""
    from stapel_search.index_schema import IndexField

    with pytest.raises(ValueError, match="no read_paths"):
        IndexField(field="ghost", kind="text", source="x", read_paths=(), test="t")
    with pytest.raises(ValueError, match="no test"):
        IndexField(field="ghost", kind="text", source="x", read_paths=("q",), test="")
    with pytest.raises(ValueError, match="closed"):
        IndexField(field="ghost", kind="invented", source="x", read_paths=("q",), test="t")


def test_every_model_column_is_accounted_for():
    """Adding a column forces a decision — indexed value, or bookkeeping."""
    from stapel_search.index_schema import INDEX_MODEL_COLUMNS
    from stapel_search.models import SearchDocument, SearchNumber

    for model in (SearchDocument, SearchNumber):
        declared = INDEX_MODEL_COLUMNS[model.__name__]
        actual = {f.name for f in model._meta.concrete_fields}
        missing = sorted(actual - set(declared))
        assert not missing, f"{model.__name__} columns absent from the contract: {missing}"


def test_the_static_gate_agrees_with_the_committed_contract():
    """stapel-index-lint, run against this package, must be clean."""
    index_lint = pytest.importorskip(
        "stapel_tools.index_lint", reason="stapel-tools not installed"
    )
    findings = index_lint.lint_project(REPO)
    assert findings == [], "\n".join(str(f) for f in findings)


# --- ranking.json -----------------------------------------------------------


def test_ranking_json_matches_the_registry():
    from stapel_search.scoring import render_ranking_json

    assert (DOCS / "ranking.json").read_text(encoding="utf-8") == render_ranking_json()


def test_the_disclosure_covers_every_scorer():
    from stapel_search.registry import BUILTIN_SCORERS

    document = _load("ranking.json")
    assert {e["slug"] for e in document["scorers"]} == set(BUILTIN_SCORERS)


# --- errors -----------------------------------------------------------------


def test_every_owned_error_key_has_ru_and_es():
    """Catalogues in the first release — the stapel-docs lesson, not repeated."""
    owned = {
        entry["code"]
        for entry in _load("errors.json")
        if entry.get("owner") == "stapel_search"
    }
    assert owned
    for language in ("ru", "es"):
        catalogue = json.loads(
            (REPO / "translations" / f"errors.{language}.json").read_text(encoding="utf-8")
        )
        missing = sorted(owned - set(catalogue))
        assert not missing, f"errors.{language}.json is missing {missing}"


def test_the_declared_keys_are_the_registered_keys():
    from stapel_search.errors import STAPEL_SEARCH_ERRORS

    owned = {
        entry["code"]
        for entry in _load("errors.json")
        if entry.get("owner") == "stapel_search"
    }
    assert owned == set(STAPEL_SEARCH_ERRORS)


# --- comm contracts ---------------------------------------------------------


def test_every_consumed_action_carries_a_schema():
    consumed = {
        p.stem for p in (REPO / "schemas" / "consumes").glob("*.json")
    }
    assert {"search.signal", "user.deleted", "category.changed"} <= consumed
    for path in (REPO / "schemas" / "consumes").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["title"] == path.stem
        assert document.get("description"), path.name


def test_no_emitted_event_is_declared_without_an_emitter():
    """A schema with no emitter is exactly the declared-but-unwired artifact
    this module's own gate exists to catch (spec §14). ``SavedQuery`` and
    ``search.saved_query.matched`` are reserved by NAME in MODULE.md; the
    schema appears when the emitter does."""
    emits = REPO / "schemas" / "emits"
    assert not emits.exists() or not list(emits.glob("*.json"))


def test_every_provided_function_carries_a_schema():
    provided = {p.stem for p in (REPO / "schemas" / "functions").glob("*.json")}
    assert provided == {"search.query", "search.reindex", "search.similar"}


# --- public API pin ---------------------------------------------------------


def test_the_public_api_is_pinned():
    """Adding to ``__all__`` is a deliberate act; removing is a break."""
    import stapel_search

    assert set(stapel_search.__all__) == {
        "FacetMapping",
        "INDEX_FIELDS",
        "IndexField",
        "Scorer",
        "SearchBackend",
        "SearchDocumentInput",
        "SourceSpec",
        "get_backend",
        "register_dictionary",
        "register_facet_mapping",
        "register_scorer",
        "register_source",
        "search_settings",
    }


def test_every_public_name_resolves():
    import stapel_search

    for name in stapel_search.__all__:
        assert getattr(stapel_search, name) is not None, name


def test_importing_the_package_does_not_need_django():
    """PEP 562 lazy exports: `import stapel_search` stays settings-free."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import stapel_search; print(stapel_search.__version__)"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_an_unknown_attribute_is_an_attribute_error():
    import stapel_search

    with pytest.raises(AttributeError):
        stapel_search.not_a_thing


def test_the_version_matches_pyproject():
    import tomllib

    import stapel_search

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert stapel_search.__version__ == data["project"]["version"]

def test_every_python_subpackage_ships_in_the_wheel():
    """0.9.1 shipped a call into a package the wheel did not carry, and
    0.10.0 shipped the package missing from pyproject's explicit
    ``packages`` list — the same wound twice: a green publish of a wheel
    that 500s at runtime. The list is explicit for good reasons, so the
    gate makes it COMPLETE: every directory under the repo root holding an
    ``__init__.py`` (tests and caches aside) must be named."""
    import tomllib

    with open(REPO / "pyproject.toml", "rb") as fh:
        listed = set(tomllib.load(fh)["tool"]["setuptools"]["packages"])
    expected = {"stapel_search"}
    skip = {"tests", "e2e", "docs", "build", "dist"}
    for init in REPO.rglob("__init__.py"):
        parts = init.relative_to(REPO).parent.parts
        if not parts or parts[0] in skip or any(p.startswith(".") for p in parts):
            continue
        expected.add(".".join(("stapel_search", *parts)))
    assert listed == expected, (
        f"pyproject packages drifted from the tree: missing {expected - listed}, "
        f"stale {listed - expected}"
    )

