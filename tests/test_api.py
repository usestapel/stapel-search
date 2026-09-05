"""The HTTP surface: who may call it, and what comes back."""
from __future__ import annotations

import pytest

from stapel_search.testing import DOC_TYPE

pytestmark = pytest.mark.django_db


QUERY = "/api/v1/query"
SUGGEST = "/api/v1/suggest"
RANKING = "/api/v1/ranking"
HEALTH = "/api/v1/health"
REINDEX = "/api/v1/reindex"


def test_query_is_anonymous_and_answers(api_client, conformance):
    response = api_client.get(QUERY, {"type": DOC_TYPE})
    assert response.status_code == 200
    body = response.json()
    assert {item["key"] for item in body["items"]} == {"1", "2", "3", "4"}
    assert body["backend"] == conformance.backend.name


def test_every_item_carries_the_promoted_marker(api_client, conformance):
    """DSA Art. 26 — the serializer cannot omit it, false included."""
    body = api_client.get(QUERY, {"type": DOC_TYPE}).json()
    assert body["items"]
    assert all("promoted" in item for item in body["items"])
    assert all(item["promoted"] is False for item in body["items"])


def test_every_item_carries_the_owner_key(api_client, conformance):
    """A storefront draws a seller panel per card without a second search.

    The value is the one the source indexed (`SearchDocumentInput.owner_key`,
    stored on the row by the indexer), so it is the same opaque id `owner=`
    filters on — which is what makes a batched profile read by id possible.
    """
    body = api_client.get(QUERY, {"type": DOC_TYPE}).json()
    assert body["items"]
    assert all("owner_key" in item for item in body["items"])
    owners = {item["key"]: item["owner_key"] for item in body["items"]}
    assert owners == {"1": "u1", "2": "u1", "3": "u1", "4": "u2"}


def test_an_item_with_no_indexed_owner_answers_an_empty_string(api_client, conformance):
    """Unknown is `""`, never a missing key: a client branches on the value."""
    from stapel_search.models import SearchDocument

    SearchDocument.objects.filter(doc_type=DOC_TYPE, doc_key="3").update(owner_key="")

    body = api_client.get(QUERY, {"type": DOC_TYPE}).json()
    orphan = next(item for item in body["items"] if item["key"] == "3")
    assert orphan["owner_key"] == ""
    assert all("owner_key" in item for item in body["items"])


def test_the_owner_key_is_neither_a_facet_nor_a_scorer(api_client, conformance):
    """It rides on the row and stays out of the panel and the ranking."""
    body = api_client.get(QUERY, {"type": DOC_TYPE}).json()
    assert "owner_key" not in body.get("facets", {})
    assert "owner_key" not in body["facet_meta"]["counted"]
    assert "owner_key" not in body["facet_meta"]["skipped"]

    ranking = api_client.get(RANKING, {"type": DOC_TYPE}).json()
    assert all("owner" not in entry["slug"] for entry in ranking["scorers"])


def test_a_bad_request_answers_with_an_error_key(api_client, conformance):
    response = api_client.get(QUERY, {"type": "nope"})
    assert response.status_code == 400
    assert "search_unknown_doc_type" in response.content.decode()


def test_suggest_is_anonymous(api_client, conformance):
    response = api_client.get(SUGGEST, {"type": DOC_TYPE, "q": "Apple"})
    assert response.status_code == 200
    assert any(title.startswith("Apple") for title in response.json()["items"])


def test_ranking_is_public_and_generated(api_client, conformance):
    """A disclosure behind a login is a disclosure nobody can read."""
    response = api_client.get(RANKING, {"type": DOC_TYPE})
    assert response.status_code == 200
    body = response.json()
    slugs = {entry["slug"] for entry in body["scorers"]}
    assert "promotion_boost" in slugs
    assert body["notes"]


def test_ranking_matches_the_committed_artifact(api_client, conformance):
    """The endpoint and docs/ranking.json render the same registry."""
    import json
    from pathlib import Path

    committed = json.loads(
        (Path(__file__).resolve().parent.parent / "docs" / "ranking.json").read_text()
    )
    served = api_client.get(RANKING, {"type": DOC_TYPE}).json()
    assert [e["slug"] for e in served["scorers"]] == [
        e["slug"] for e in committed["scorers"]
    ]
    assert [e["weight"] for e in served["scorers"]] == [
        e["weight"] for e in committed["scorers"]
    ]
    assert served["notes"] == committed["notes"]


def test_health_and_reindex_refuse_an_anonymous_caller(api_client, conformance):
    assert api_client.get(HEALTH).status_code in (401, 403)
    assert api_client.post(REINDEX, {"doc_type": DOC_TYPE}, format="json").status_code in (
        401,
        403,
    )


def test_health_refuses_a_signed_in_non_operator(api_client, conformance, db):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create(username="joe", email="joe@example.com")
    api_client.force_authenticate(user=user)
    response = api_client.get(HEALTH)
    assert response.status_code == 403
    assert "search_forbidden" in response.content.decode()


def test_health_answers_an_operator(api_client, conformance, staff_user):
    api_client.force_authenticate(user=staff_user)
    response = api_client.get(HEALTH)
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == conformance.backend.name
    assert body["reachable"] is True
    assert DOC_TYPE in body["types"]
    assert "capabilities" in body


def test_reindex_refuses_an_unknown_type(api_client, conformance, staff_user):
    api_client.force_authenticate(user=staff_user)
    response = api_client.post(REINDEX, {"doc_type": "nope"}, format="json")
    assert response.status_code == 400


def test_the_public_views_declare_their_anonymous_access():
    """Core's adoption check treats silence as the red state, so the
    declaration is stated rather than defaulted into."""
    from stapel_core.django.api.permissions import ANONYMOUS_ALLOWED, ANONYMOUS_DENIED

    from stapel_search import views

    assert views.SearchQueryView.stapel_anonymous_access == ANONYMOUS_ALLOWED
    assert views.SearchSuggestView.stapel_anonymous_access == ANONYMOUS_ALLOWED
    assert views.SearchRankingView.stapel_anonymous_access == ANONYMOUS_ALLOWED
    assert views.SearchHealthView.stapel_anonymous_access == ANONYMOUS_DENIED
    assert views.SearchReindexView.stapel_anonymous_access == ANONYMOUS_DENIED


def test_throttle_rates_come_from_this_modules_namespace():
    """A library does not own the project's DEFAULT_THROTTLE_RATES."""
    from django.test import override_settings

    from stapel_search.views import QueryThrottle, SuggestThrottle

    with override_settings(STAPEL_SEARCH={"QUERY_THROTTLE": "7/min"}):
        assert QueryThrottle().get_rate() == "7/min"
    assert SuggestThrottle().get_rate() == "300/min"


def test_the_comm_function_serves_the_same_contract(conformance):
    """A server-side caller needs no HTTP round trip and no import."""
    from stapel_core.comm import call

    body = call("search.query", {"type": DOC_TYPE, "limit": 2})
    assert len(body["items"]) == 2
    assert "degraded" in body and "facet_meta" in body
    # Same rows, same fields: a caller assembling a strip server-side gets
    # the seller id the HTTP surface serves, not a thinner item.
    owners = {"1": "u1", "2": "u1", "3": "u1", "4": "u2"}
    assert all(item["owner_key"] == owners[item["key"]] for item in body["items"])


def test_the_reindex_function_targets_keys(conformance, monkeypatch):
    from stapel_core.comm import call

    monkeypatch.setattr(
        "stapel_core.comm.call",
        lambda name, payload: {
            key: {"doc_type": DOC_TYPE, "doc_key": key, "status": "published",
                  "title": "Re-pulled", "seq": 9999}
            for key in payload["keys"]
        },
    )
    from stapel_search.functions import reindex_function

    result = reindex_function({"doc_type": DOC_TYPE, "keys": ["1"]})
    assert result["indexed"] == 1

    from stapel_search.models import SearchDocument

    assert SearchDocument.objects.get(doc_type=DOC_TYPE, doc_key="1").title == "Re-pulled"
    assert call  # the public entry point exists under its registered name


def test_the_admin_is_read_only():
    """Every row is derived: a human correcting one by hand produces a value
    the next re-index silently reverts."""
    from django.contrib import admin

    from stapel_search.models import SearchDocument, SearchNumber, SearchSignal

    for model in (SearchDocument, SearchNumber, SearchSignal):
        registered = admin.site._registry[model]
        assert registered.has_add_permission(None) is False
        assert registered.has_change_permission(None) is False
        assert registered.has_delete_permission(None) is False
