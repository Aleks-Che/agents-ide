"""Connection diagnostics must use manual models and allow time for reasoning."""

import json

import httpx
import pytest

from agents_ide.adapters.base import ExternalOutcome, LLMAdapterRequest
from agents_ide.adapters.llm_http import HttpLLMAdapter, probe_connection


@pytest.fixture
def provider(monkeypatch):
    state = {"catalog": None, "reply": None, "requests": []}
    original = httpx.AsyncClient

    def handle(request):
        if request.method == "GET":
            if state["catalog"] is None:
                return httpx.Response(404, json={"error": {"message": "No catalog"}})
            return httpx.Response(200, json={"data": [{"id": m} for m in state["catalog"]]})
        body = json.loads(request.content)
        state["requests"].append(body)
        if state["reply"] is not None:
            return httpx.Response(200, json=state["reply"])
        # Reproduce a model that spends its first tokens on reasoning.
        if body.get("max_tokens", 0) < 64:
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "length", "message": {"role": "assistant"}}]},
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    return state


@pytest.mark.parametrize("catalog", [None, [], ["catalog-model"]])
def test_manual_model_probe_allows_reasoning_without_requiring_catalog(
    authenticated, provider, catalog
):
    client, headers = authenticated
    provider["catalog"] = catalog
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Reasoning provider",
            "base_url": "http://127.0.0.1:1234/v1",
            "manual_models": ["manual-reasoner", "second-model"],
        },
    ).json()
    response = client.post(f"/api/connections/{connection['id']}/test", headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok", result
    assert result["tested_model"] == "manual-reasoner"
    assert result["models"] == (catalog or [])
    assert len(provider["requests"]) == 1
    assert provider["requests"][0]["model"] == "manual-reasoner"
    current = client.get(f"/api/connections/{connection['id']}").json()
    assert current["version"] == connection["version"]
    assert current["manual_models"] == connection["manual_models"]
    assert current["catalog_models"] == (catalog or [])
    assert (current["catalog_fetched_at"] is not None) == (catalog is not None)


def test_catalog_fallback_reports_the_model_actually_sent(provider):
    provider["catalog"] = ["catalog-first", "catalog-second"]
    result = probe_connection({"base_url": "http://127.0.0.1:1234/v1"})
    assert result.ok
    assert result.tested_model == "catalog-first"
    assert provider["requests"][0]["model"] == "catalog-first"


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (
            {"choices": [{"finish_reason": "length", "message": {"role": "assistant"}}]},
            "лимитом токенов",
        ),
        (
            {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]},
            "фильтром",
        ),
        ({"choices": []}, "нет текстового содержимого"),
    ],
)
def test_failed_probe_explains_missing_text_without_claiming_no_models(
    authenticated, provider, reply, detail
):
    client, headers = authenticated
    provider["catalog"] = ["catalog-model"]
    provider["reply"] = reply
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Empty response",
            "base_url": "http://127.0.0.1:1234/v1",
            "manual_models": ["manual"],
        },
    ).json()
    result = client.post(f"/api/connections/{connection['id']}/test", headers=headers).json()
    assert result["status"] == "failed"
    assert result["tested_model"] == "manual"
    assert result["models"] == ["catalog-model"]
    assert detail in result["detail"]
    assert len(provider["requests"]) == 1
    current = client.get(f"/api/connections/{connection['id']}").json()
    assert current["last_test_status"] == "failed"
    assert current["manual_models"] == ["manual"]


def test_normal_generation_still_rejects_truncated_text(provider):
    provider["reply"] = {
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}]
    }
    result = HttpLLMAdapter().run(
        LLMAdapterRequest(
            role="task",
            model_id="manual",
            prompt="finish task",
            context_package={},
            params={},
            connection={"base_url": "http://127.0.0.1:1234/v1"},
        )
    )
    assert result.outcome == ExternalOutcome.INVALID_FORMAT
    assert result.error.code == "invalid_provider_response"
