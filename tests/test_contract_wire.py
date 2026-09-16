"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This module is the sharpest form of that gap in the fleet, because nothing
here even passes through the serializer that documents it. ``serializers.py``
says so in its own first line — "documentation-first": the service layer
builds plain dicts, ``views._handle`` hands the dict straight to
``StapelResponse``, and the ``…ResponseSerializer`` classes exist only to be
read by drf-spectacular. There is no runtime step anywhere that compares the
two. This file is that step.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails;
* a collection that comes back empty in EVERY state the populated pass
  drove fails — an empty array validates against any item schema, so an
  empty answer is a check that looked at nothing. That covers the nested
  collections (``items``, ``facets``, ``scorers``, ``categories``,
  ``types``…), which a generic "is the body a list" check cannot see. It is
  asked of the operation rather than of each branch, because a recipe that
  drives three states on purpose may legitimately have one that shows an
  empty panel;
* every read is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``): a query that matches nothing, a type-ahead with no
  prefix, a health read against an index with no documents in it. Every null
  finding in the first wave of this gate was there.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT — and this module is one of the defective ones. ``codegen_urls.py``
mounts ``search/`` → ``stapel_search.urls``, which contributes ``api/v1/``,
so the committed document is written against ``/search/api/v1/…``.
``tests/urls.py`` mounts the module at the BARE root (``path("", …)``), so
the entire existing suite drives ``/api/v1/query`` and **not one path in the
committed contract resolves under it**. Five of the first eight libraries in
this wave carried exactly that shape; this is one of them, and
``test_every_declared_path_resolves_under_this_urlconf`` below is what
catches it. The emission mount is declared in this module rather than
borrowed, and the library's own urlconf is left alone.

What it found on its first run — 5 of 5 operations driven, 1 red:

* ``GET /search/api/v1/suggest`` declares ``CategorySuggestion.id`` as a
  REQUIRED ``integer`` and sends a STRING for every goods-driven row whose
  category path does not end in digits. ``suggest._listing_rows``
  (suggest.py:366) writes ``"id": int(leaf) if leaf.lstrip("-").isdigit()
  else leaf`` — the comment beside it ("keep the type where the segment
  allows it") is the defect stated out loud: the field's type is a function
  of the data. A goods-driven row is minted whenever no category NAME
  matched, which is the whole reason that half of the feature exists
  («samsung» names no node), so this is the ordinary answer for a brand
  query, not a corner. A generated client typed from this schema parses
  ``id`` as a number and gets a string.

  Only the goods-driven half is affected: a row that came from the
  ``categories.suggest`` provider passes the provider's own ``id`` through
  (suggest.py:523) and the fleet's provider serves integer pks.

Left exactly as it is: this is a gate, not a fix. The other four operations
are honest, in both states, including every ``nullable`` claim.
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked: it re-validates every driven body against
``{"type": "string"}`` and requires all of them to fail.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``search/`` → ``stapel_search.urls``, which
#: contributes ``api/v1/``). ``tests/urls.py`` mounts the module bare, so
#: none of these paths resolve under the suite's own urlconf — see the
#: module docstring.
urlpatterns = [
    url_path("search/", include("stapel_search.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/search/api/v1"

#: The conformance corpus' document type — the module's own public fixture
#: (``stapel_search.testing``), which is how a third-party backend is asked
#: to prove itself. Using it here means the gate drives the same corpus the
#: engine conformance suite does.
DOC_TYPE = "conformance"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: the seams a host wires, wired
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Nothing here writes files today; pin the root so nothing ever does.

    ``MEDIA_ROOT`` is unset in this module's harness settings
    (``_codegen_settings.py``), so it defaults to the working directory — in
    stapel-auth that put a data export into the checkout, where under a flat
    package layout a stray directory also shadowed a real submodule.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture(autouse=True)
def _no_throttle():
    """The two scoped throttles stood down; neither is a response shape.

    ``QUERY_THROTTLE`` and ``SUGGEST_THROTTLE`` share one per-IP bucket in
    the process-wide locmem cache, and this file drives the query endpoint a
    dozen times in a row (the canary re-drives every recipe). A 429 says
    nothing about the 200 body the contract declares.
    """
    with tuned(QUERY_THROTTLE=None, SUGGEST_THROTTLE=None):
        yield


@pytest.fixture(autouse=True)
def _clean_registries():
    """Registries and caches are process-global; a recipe must not leak.

    The suite's own conftest does this for its tests; this file does not
    inherit that guarantee for the state it registers itself.
    """
    from django.core.cache import cache

    yield
    cache.clear()
    from stapel_core.comm.registry import function_registry

    for name in ("categories.features", "categories.suggest",
                 f"{DOC_TYPE}.documents", f"{DOC_TYPE}.export"):
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)


def tuned(**overrides):
    """``override_settings`` REPLACES a dict setting; keep what is there."""
    from django.conf import settings

    return override_settings(
        STAPEL_SEARCH={**getattr(settings, "STAPEL_SEARCH", {}), **overrides}
    )


@contextlib.contextmanager
def corpus_loaded():
    """The conformance corpus in the index, through the module's own harness.

    ``stapel_search.testing.harness`` is public and importable on purpose —
    it is how a third-party backend runs the same scenarios — so the gate
    loads the corpus the way the engine conformance suite does rather than
    inventing a second one.
    """
    from stapel_search.testing import harness

    with harness() as context:
        yield context


@contextlib.contextmanager
def empty_index():
    """The source registered, the index deliberately empty.

    Not "no source at all": ``/query`` refuses an unregistered type with a
    400, so the emptiest state that still answers 200 is a registered type
    with nothing under it.
    """
    from stapel_search.models import SearchDocument
    from stapel_search.registry import register_source, unregister_source
    from stapel_search.testing import CONFORMANCE_SOURCE

    register_source(CONFORMANCE_SOURCE)
    try:
        from stapel_search.backends import get_backend

        get_backend().clear(DOC_TYPE)
        SearchDocument.objects.filter(doc_type=DOC_TYPE).delete()
        yield
    finally:
        with contextlib.suppress(Exception):
            from stapel_search.backends import get_backend

            get_backend().clear(DOC_TYPE)
        unregister_source(DOC_TYPE)


def _feature_def(slug, kind, **config):
    """One ``categories.features`` row, in the provider's own shape."""
    return {
        "id": 0,
        "slug": slug,
        "name": slug,
        "translate": "none",
        "mandatory": False,
        "show_at_title": False,
        "show_as_badge": False,
        "config": {"type": kind, **config},
    }


#: What the two corpus leaves declare. Shaped to the features the corpus
#: rows actually carry (``testing.corpus``), so the panel counts real
#: buckets rather than an empty plan.
CATALOGUE = {
    "phones": [
        _feature_def(
            "color", "select",
            options=[{"value": "red", "label": "Red"}, {"value": "blue", "label": "Blue"}],
            allowCustom=True,
        ),
        _feature_def(
            "brand", "select",
            options=[
                {"value": "apple", "label": "Apple"},
                {"value": "samsung", "label": "Samsung"},
            ],
            allowCustom=True,
        ),
        _feature_def("year", "int", postfix="y"),
    ],
    "laptops": [
        _feature_def(
            "brand", "select",
            options=[{"value": "lenovo", "label": "Lenovo"}], allowCustom=True,
        ),
        _feature_def("year", "int", postfix="y"),
    ],
}


def category_features():
    """Wire ``categories.features`` — the panel's authored schema.

    A comm Function seam owned by stapel-categories. Without it the plan is
    empty and ``facets`` comes back ``{}``, which validates against the
    declared map and checks nothing, so the gate stands the provider up the
    way a deployment does.
    """
    from stapel_core.comm import register_function

    def provider(payload):
        category_id = str(payload["category_id"])
        return {
            "category_id": category_id,
            "revision": 1,
            "features": CATALOGUE.get(category_id, []),
        }

    register_function("categories.features", provider)


def category_suggest(rows):
    """Wire ``categories.suggest`` — the type-ahead's name matcher.

    A function name has exactly ONE provider (``FunctionRegistry.register``
    refuses a second), so a recipe that asks the dropdown two questions
    replaces the provider rather than adding one.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    function_registry._providers.pop("categories.suggest", None)
    register_function("categories.suggest", lambda payload: {"categories": list(rows)})


def source_functions():
    """Wire the source's two comm Functions: content pull and snapshot export.

    ``reindex`` is the one operation that reads from the owner rather than
    from the index, through exactly these two names
    (``services.pull_documents`` / ``services._snapshot_pages``). They are
    seams a host fills, so the gate fills them and everything on this side
    runs for real.
    """
    from stapel_core.comm import register_function
    from stapel_search.testing import corpus

    payloads = {}
    for document in corpus():
        row = dataclasses.asdict(document)
        row["key"] = document.doc_key
        payloads[document.doc_key] = row

    register_function(
        f"{DOC_TYPE}.documents",
        lambda payload: {
            key: payloads[key] for key in payload.get("keys", []) if key in payloads
        },
    )
    register_function(
        f"{DOC_TYPE}.export",
        lambda payload: {"rows": list(payloads.values()), "cursor": None,
                         "total": len(payloads)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergence that matters here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside an ``allOf`` wrapping a ``$ref``, which is how
    ``category_resolved`` is emitted); JSON Schema has no such keyword and
    would refuse the null — which is exactly what a query naming no category
    answers, and what ``count`` answers on an engine that cannot say.
    Everything else drf-spectacular emits here (``$ref``, ``allOf``,
    ``enum``, ``required``, ``additionalProperties``) is JSON Schema as
    written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user(**kwargs):
    from django.contrib.auth import get_user_model

    defaults = dict(username=_unique("wire_"), email=f"{_unique('wire-')}@example.com")
    defaults.update(kwargs)
    return get_user_model().objects.create(**defaults)


def anonymous():
    return APIClient()


def operator():
    """``health`` and ``reindex`` are operator surface (``authz.can_manage``)."""
    client = APIClient()
    client.force_authenticate(user=make_user(is_staff=True))
    return client


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query=None, **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url, query or {}, **extra)
        return send(url, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. A recipe returns the response
#: it produced, or a list of ``(label, response)`` pairs when one operation
#: has more than one answering state worth asking.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: a query that matches nothing, a type-ahead nobody has typed into,
#: an index with no documents in it. A populated answer cannot say what a
#: field holds when there is nothing to hold, and that is where every null
#: finding in the first wave of this gate was.
EMPTY_STATE = {}


def recipe(method, path, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path)
        assert key not in target, f"duplicate recipe for {method} {path}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path):
    return recipe(method, path, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares runs against a real engine
#: here — the naive backend the harness settings select, which is a real
#: implementation of the backend protocol and not a stub. The three things
#: that genuinely live outside the module (the category name matcher, the
#: authored feature schema, the source's own content and snapshot reads) are
#: declared comm Function seams, so the gate wires them the way a deployment
#: does and everything on this side of them runs for real.
UNDRIVABLE: dict = {}

#: Collections nested inside an object body that must actually carry a row in
#: the populated pass. An empty array — or an empty map — validates against
#: any item schema, so a populated run that leaves one empty looked at
#: nothing, and the generic "is the body a list" check cannot see a
#: collection one level down.
POPULATED_COLLECTIONS = {
    ("GET", V1 + "/query"): ("items", "facets", "facet_labels"),
    ("GET", V1 + "/suggest"): ("categories", "terms", "items"),
    ("GET", V1 + "/ranking"): ("scorers", "notes"),
    ("GET", V1 + "/health"): ("types", "capabilities"),
}


# ── query ────────────────────────────────────────────────────────────────────


@recipe("GET", "/query")
def _query(call):
    """Three answering shapes of one endpoint.

    The plain SERP; the same query with the two optional halves of the
    envelope switched ON (``bands`` under ``geo_mode=rank``, and
    ``query_understanding`` under ``QUERY_UNDERSTANDING``), because both are
    absent by default and an absent field is a field this gate never saw;
    and the same read as an operator, whose geo answers are the exact ones
    rather than the public grid's.
    """
    category_features()
    with corpus_loaded():
        plain = call(anonymous(), query={"type": DOC_TYPE})
        with tuned(GEO_BANDS=True, QUERY_UNDERSTANDING=True):
            widened = call(
                anonymous(),
                query={
                    "type": DOC_TYPE,
                    "q": "Apple",
                    "lat": "49.6116",
                    "lon": "6.1319",
                    "geo_mode": "rank",
                    "radius_km": "10",
                },
            )
        as_staff = call(operator(), query={"type": DOC_TYPE, "sort": "newest"})
    return [
        ("the plain SERP", plain),
        ("bands and query understanding on", widened),
        ("an operator (exact geo, unredacted cards)", as_staff),
    ]


@empty_state("GET", "/query")
def _query_empty(call):
    """Two kinds of nothing: a registered type with no documents under it,
    and a corpus that simply does not contain the words. Both answer 200,
    and both are where ``category_resolved``, ``next_anchor``, ``prev_anchor``
    and ``count`` have to say what they hold when there is nothing."""
    with empty_index():
        no_documents = call(anonymous(), query={"type": DOC_TYPE})
    with corpus_loaded():
        no_match = call(
            anonymous(), query={"type": DOC_TYPE, "q": "zzzznothingmatchesthis"}
        )
    return [
        ("a registered type with an empty index", no_documents),
        ("a query nothing in the corpus matches", no_match),
    ]


# ── suggest ──────────────────────────────────────────────────────────────────


#: What ``categories.suggest`` answers — the provider's own row shape, with
#: the integer pks the fleet's implementation serves.
NAMED_ROWS = [
    {
        "id": 101,
        "slug": "phones",
        "name": "Phones",
        "path": ["Electronics", "Phones"],
        "path_ids": ["electronics", "phones"],
        "depth": 2,
        "match": "prefix",
    },
]


@recipe("GET", "/suggest")
def _suggest(call):
    """Both halves of the dropdown, which are two different row shapes.

    A NAME row comes from the ``categories.suggest`` provider and carries
    that provider's ``id``. A GOODS-DRIVEN row is minted here, from the
    engine's own answer about which categories hold matching documents, and
    derives its ``id`` from the path's leaf segment — which in this corpus,
    and in any catalogue whose paths are slugs, is not a number.
    """
    with corpus_loaded():
        category_suggest(NAMED_ROWS)
        named = call(anonymous(), query={"type": DOC_TYPE, "q": "phones"})
        # No name matches «apple», so the goods-driven half runs — the whole
        # reason that half exists (a brand names no category).
        category_suggest([])
        goods = call(anonymous(), query={"type": DOC_TYPE, "q": "apple"})
    return [
        ("a name-matched row (provider id)", named),
        ("a goods-driven row (id derived from the path leaf)", goods),
    ]


@empty_state("GET", "/suggest")
def _suggest_empty(call):
    """Two kinds of empty: nothing typed at all, and a dropdown whose only
    row is itself empty.

    The first is the state the box is in before the first keystroke — no
    prefix, so neither half runs and ``categories``, ``terms`` and ``items``
    are all empty. The second matters more: an empty ARRAY validates against
    any item schema, so a collection's emptiest interesting state is one row
    carrying none of its optional values, and for this dropdown that is the
    goods-driven row — no name provider answered, so its ``slug`` is the
    empty string and its ``name`` is the bare path segment."""
    with corpus_loaded():
        category_suggest([])
        nothing_typed = call(anonymous(), query={"type": DOC_TYPE})
        emptiest_row = call(anonymous(), query={"type": DOC_TYPE, "q": "apple"})
    return [
        ("nothing typed yet", nothing_typed),
        ("one goods-driven row, carrying none of its optional values",
         emptiest_row),
    ]


# ── ranking ──────────────────────────────────────────────────────────────────


@recipe("GET", "/ranking")
def _ranking(call):
    """The P2B disclosure, which is public on purpose — a disclosure behind
    a login is a disclosure nobody can read."""
    with corpus_loaded():
        return call(anonymous(), query={"type": DOC_TYPE})


@empty_state("GET", "/ranking")
def _ranking_empty(call):
    """No document type named, and no engine loaded to annotate against:
    the disclosure still has to be the declared shape, because a deployment
    that cannot answer it is the one a regulator asks about."""
    return call(anonymous())


# ── health ───────────────────────────────────────────────────────────────────


@recipe("GET", "/health")
def _health(call):
    with corpus_loaded():
        return call(operator())


@empty_state("GET", "/health")
def _health_empty(call):
    """An index with no documents in it: ``lag_seconds`` is null, because
    there is no newest row to measure from, and ``documents`` is whatever an
    empty engine reports. Both are declared nullable; this is the state that
    asks them."""
    with empty_index():
        return call(operator())


# ── reindex ──────────────────────────────────────────────────────────────────


@recipe("POST", "/reindex")
def _reindex(call):
    """Both branches: a targeted key re-pull, and a whole-type rebuild."""
    source_functions()
    with corpus_loaded():
        targeted = call(
            operator(), data={"doc_type": DOC_TYPE, "keys": ["1", "2"]}
        )
        whole = call(operator(), data={"doc_type": DOC_TYPE})
    return [
        ("a targeted re-pull", targeted),
        ("a whole-type rebuild", whole),
    ]


@empty_state("POST", "/reindex")
def _reindex_empty(call):
    """A re-pull of keys the source no longer has: every counter is zero,
    which is the report's emptiest legal shape and still four integers."""
    source_functions()
    with corpus_loaded():
        return call(
            operator(), data={"doc_type": DOC_TYPE, "keys": ["no-such-key"]}
        )


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send, with the defect and
#: its owner. ``strict=True``: a fixed entry fails until it is deleted, so a
#: finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES = {
    ("GET", V1 + "/suggest"):
        "declares CategorySuggestion.id as a REQUIRED integer and sends a "
        "STRING on every goods-driven row whose category path does not end "
        "in digits. suggest._listing_rows (suggest.py:366) writes "
        "'id': int(leaf) if leaf.lstrip('-').isdigit() else leaf — the "
        "field's type is a function of the data, which the comment beside it "
        "states as an intention ('keep the type where the segment allows "
        "it'). A goods-driven row is minted exactly when no category NAME "
        "matched the query, which is the case that half of the type-ahead "
        "exists for (a brand names no node), so a string id is the ordinary "
        "answer to a brand query rather than a corner. A client generated "
        "from this schema parses id as a number. The name-matched half is "
        "honest: it passes the categories.suggest provider's own id through "
        "(suggest.py:523). Owner: stapel-search — either coerce the derived "
        "id or declare the field as the union it already is.",
}


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted a doubled segment, one mounted
    less than the emission did — and THIS module mounts the package at the
    bare root (``tests/urls.py``: ``path("", include("stapel_search.urls"))``),
    so its whole suite drives ``/api/v1/…`` while the committed document is
    written against ``/search/api/v1/…``. Not one declared path resolves
    under it.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the
    contract says it goes. A missing recipe already fails loudly; this fails
    when the MOUNT is wrong, which no per-operation check can see, because
    when the mount is wrong every operation is equally and silently
    unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and a urlconf may use
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    stale_collections = sorted(set(POPULATED_COLLECTIONS) - declared)
    assert not stale_collections, (
        f"nested-collection expectations for undeclared operations: {stale_collections}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds four documents and asks never sees any of
    them — and this module's whole envelope (``count``, ``next_anchor``,
    ``category_resolved``, ``lag_seconds``) is nullable precisely there.
    """
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    }
    missing = sorted(reads - set(EMPTY_STATE))
    assert not missing, (
        "reads driven only against a populated index — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared = {(m, p) for m, p, _c, _s in OPERATIONS}
    stale = sorted(set(EMPTY_STATE) - declared)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _labelled(result):
    """A recipe answers with one response, or with labelled branches."""
    if isinstance(result, list):
        return result
    return [("", result)]


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = table.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    # An empty collection validates against any item schema, so a populated
    # run has only looked at something if a row actually arrived. Accumulated
    # across an operation's branches rather than demanded of each: an
    # operation answers several states on purpose, and it is enough that ONE
    # of them carried the rows — a branch that exists to show an empty panel
    # is not a gap.
    seen_rows = set()
    expected_rows = set(POPULATED_COLLECTIONS.get((method, path), ()))
    saw_top_level_list = False
    top_level_list_had_rows = False

    for label, response in _labelled(perform(Call(method, path))):
        where = f"{method} {path}" + (f" [{label}]" if label else "")
        assert response.status_code == code, (
            f"{where}: expected the declared {code}, got "
            f"{response.status_code}: {response.content[:400]}"
        )

        body = response.json()
        errors = sorted(
            _validator(body_schema).iter_errors(body), key=lambda e: list(e.path)
        )
        assert not errors, (
            f"{where} answers a body the contract does not describe:\n"
            + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
            + f"\n  body: {json.dumps(body)[:600]}"
        )

        if isinstance(body, list):
            saw_top_level_list = True
            top_level_list_had_rows = top_level_list_had_rows or bool(body)
        elif isinstance(body, dict):
            seen_rows |= {name for name in expected_rows if body.get(name)}

    if expect_rows:
        if saw_top_level_list:
            assert top_level_list_had_rows, (
                f"{method} {path}: the declared list came back empty in every "
                "state this recipe drove"
            )
        unchecked = sorted(expected_rows - seen_rows)
        assert not unchecked, (
            f"{method} {path}: the declared collections {unchecked} came back "
            "empty in every state this recipe drove, so nothing in them was "
            "checked"
        )


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if (method, path) in EMPTY_STATE
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object, so every one of them must fail. If any passes, the validation
    in ``_drive`` is not reaching the received body and this whole file proves
    nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
