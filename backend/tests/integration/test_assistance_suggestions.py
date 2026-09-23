import json

import pytest
from test_assistance import stopped_frontend, target

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.persistence.models import Run
from agents_ide.services import assistance_suggestions


@pytest.fixture
def suggestions_case(authenticated, tmp_path):
    client, headers = authenticated
    run, factory = stopped_frontend(authenticated, tmp_path)
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Questions model",
            "provider_kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "manual_models": ["questions-model"],
        },
    )
    assert connection.status_code == 201
    context = client.get("/api/assistance/context", params=target(run)).json()
    return (
        run,
        factory,
        {
            "target": target(run),
            "connection_id": connection.json()["id"],
            "model_id": "questions-model",
            "diagnostic_revision": context["diagnostic_revision"],
        },
    )


@pytest.mark.parametrize(
    "wrapper",
    [
        "{body}",
        '<think>Reasoning\n{{"questions":["Wrong question?",'
        '"Another wrong question?"]}}</think>\n{body}',
        "<think>First thought</think>\n<THINK>Second\nthought</THINK>\n```json\r\n{body}\r\n```",
        "<think>Reasoning</think>\n```\n{body}\n```\n<think>Extra thought</think>",
        "Вот дополнительные вопросы:\n```json\n{body}\n```\nВыберите подходящий.",
        '<think mode="reasoning">Example {{"questions":["Private example?"]}}</think>\n{body}',
        "<mm:think>Reasoning</mm:think>\n{body}",
    ],
)
def test_questions_are_generated_from_fresh_scoped_diagnostics(
    authenticated, suggestions_case, monkeypatch, wrapper
):
    run, factory, payload = suggestions_case
    seen = []
    questions = [
        "Что изменилось в защищённых файлах этого запуска?",
        "Как проверить эти изменения перед принятием?",
    ]

    def model(self, request):
        seen.append(request)
        return LLMResult(
            ExternalOutcome.SUCCEEDED,
            wrapper.format(body=json.dumps({"questions": questions})),
            None,
            None,
        )

    monkeypatch.setattr(assistance_suggestions.HttpLLMAdapter, "run", model)
    response = authenticated[0].post(
        "/api/assistance/suggestions", headers=authenticated[1], json=payload
    )
    assert response.status_code == 200, response.text
    assert response.json()["questions"] == questions
    assert "Reasoning" not in response.text and "thought" not in response.text
    assert response.json()["diagnostic_revision"] == payload["diagnostic_revision"]
    evidence = seen[0].context_package["evidence"]
    assert seen[0].response_format == "json"
    assert evidence["title"] == "math-portal — Фронтенд"
    assert evidence["findings"][0]["evidence"]["git_check"]["status"] == "unavailable"
    assert "questions" not in evidence and "questions" not in evidence["guide"]
    assert "question" not in evidence["findings"][0]
    assert "history" not in evidence
    assert "must-not-leak" not in json.dumps(evidence)
    with factory() as session:
        assert session.get(Run, run["id"]).state == "waiting_input"


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        '{"questions":[]}',
        json.dumps({"questions": ["x" * 221, None, ""]}),
        '<think>{"questions":["Reasoning question?","Another reasoning question?"]}',
        '<think>{"questions":["Reasoning question?","Another reasoning question?"]}</think>',
    ],
)
def test_invalid_generation_never_falls_back_to_canned_questions(
    authenticated, suggestions_case, monkeypatch, output
):
    _, _, payload = suggestions_case
    monkeypatch.setattr(
        assistance_suggestions.HttpLLMAdapter,
        "run",
        lambda *_: LLMResult(ExternalOutcome.SUCCEEDED, output, None, None),
    )
    response = authenticated[0].post(
        "/api/assistance/suggestions", headers=authenticated[1], json=payload
    )
    assert response.status_code == 502
    assert response.json()["code"] == "assistance_suggestions_invalid"
    assert response.json()["details"]["reason"] in {"invalid_json", "no_usable_questions"}


@pytest.mark.parametrize("during", [False, True])
def test_stale_suggestions_are_not_published(authenticated, suggestions_case, monkeypatch, during):
    run, factory, payload = suggestions_case
    seen = []

    def change():
        with factory.begin() as session:
            row = session.get(Run, run["id"])
            row.state = "completed"
            row.state_version += 1

    def model(self, request):
        seen.append(request)
        change()
        return LLMResult(
            ExternalOutcome.SUCCEEDED,
            '{"questions":["Первый вопрос?","Второй вопрос?"]}',
            None,
            None,
        )

    monkeypatch.setattr(assistance_suggestions.HttpLLMAdapter, "run", model)
    if not during:
        change()
    response = authenticated[0].post(
        "/api/assistance/suggestions", headers=authenticated[1], json=payload
    )
    assert response.status_code == 409
    assert response.json()["code"] == "assistance_context_changed"
    assert len(seen) == int(during)


def test_revision_ignores_polling_but_changes_after_resolution(authenticated, suggestions_case):
    run, factory, payload = suggestions_case
    client, headers = authenticated
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.updated_at += 1
    assert (
        client.get("/api/assistance/context", params=target(run)).json()["diagnostic_revision"]
        == payload["diagnostic_revision"]
    )
    with factory.begin() as session:
        session.get(Run, run["id"]).state_version += 1
    assert (
        client.get("/api/assistance/context", params=target(run)).json()["diagnostic_revision"]
        != payload["diagnostic_revision"]
    )
    assert client.post("/api/assistance/suggestions", json=payload).status_code == 403
    assert (
        client.post(
            "/api/assistance/suggestions", headers=headers, json={**payload, "model_id": "unknown"}
        ).status_code
        == 422
    )
