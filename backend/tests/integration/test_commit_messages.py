"""Generated commit messages use the actual staged tree and saved settings."""

import json

import pytest
from test_stage8_git_plan import baseline, command, intent
from test_stage8_git_plan import repository as repository_fixture
from test_stage8_review import Agent, execute_run, start_preset
from test_stage8_review import provider as provider_fixture

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.domain.commit_messages import DEFAULT_COMMIT_MESSAGE_PROMPT
from agents_ide.engine import git_commit as git
from agents_ide.persistence.models import Run
from agents_ide.services.settings import DEFAULTS, capture_dependencies

repository = repository_fixture
provider = provider_fixture


def test_general_defaults_and_node_overrides_are_pinned(authenticated):
    client, headers = authenticated
    original = client.get("/api/settings/general").json()
    assert original["commit_message"]["prompt"] == DEFAULT_COMMIT_MESSAGE_PROMPT
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "Messages", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    settings = {
        **original,
        "commit_message": {
            "connection_id": connection["id"],
            "model": "global-model",
            "prompt": "Global prompt",
            "language": "en",
            "params": {"temperature": 0.2},
        },
    }
    assert client.put("/api/settings/general", json=settings).status_code == 403
    saved = client.put("/api/settings/general", headers=headers, json=settings)
    assert saved.status_code == 200, saved.text
    assert client.put("/api/settings/general", headers=headers, json=settings).status_code == 409
    graph = {"nodes": [{"id": "commit", "type": "GitCommit", "config": {"generate_message": True}}]}
    with client.app.state.session_factory() as session:
        pinned = capture_dependencies(session, graph, DEFAULTS)
        assert pinned["nodes"]["commit"]["message_generation"]["prompt"] == "Global prompt"
        assert (
            pinned["provider_connections"][connection["id"]]["base_url"] == "http://127.0.0.1:9/v1"
        )
        graph["nodes"][0]["config"]["message_generation"] = {
            "prompt": "Node prompt",
            "language": "ru",
            "model": "node-model",
            "params": {"max_tokens": 900},
        }
        custom = capture_dependencies(session, graph, DEFAULTS)["nodes"]["commit"][
            "message_generation"
        ]
        assert custom["connection_id"] == connection["id"]
        assert custom["prompt"] == "Node prompt" and custom["language"] == "ru"
        assert custom["params"] == {"max_tokens": 900}
    updated = saved.json()
    updated["commit_message"]["prompt"] = "Later global prompt"
    assert client.put("/api/settings/general", headers=headers, json=updated).status_code == 200
    assert pinned["nodes"]["commit"]["message_generation"]["prompt"] == "Global prompt"
    for change in [{"prompt": "  "}, {"params": {"temperature": 4}}, {"language": "invalid"}]:
        invalid = {**updated, "commit_message": {**updated["commit_message"], **change}}
        assert client.put("/api/settings/general", headers=headers, json=invalid).status_code == 422


@pytest.mark.parametrize("format_", ["plain", "thinking", "fenced_thinking"])
def test_exact_staged_diff_omits_deleted_contents_and_preserves_message(repository, format_):
    value = baseline(repository, allowlist=("src/**", "README.md"))
    (repository / "src/a.txt").unlink()
    (repository / "src/new.txt").write_text("new content\n")
    (repository / "README.md").write_text("outside selected node\n")
    seen = []
    message = "feat(src): update files\n\n- Add the new file\n- Remove the old file"

    def generate(diff):
        seen.append(diff)
        if format_ == "thinking":
            return (
                "<think>\nDraft reasoning.<think>Nested draft.</think>Still reasoning.\n</think>\n"
                f"{message}\n<THINK>Another private block.</THINK>"
            )
        if format_ == "fenced_thinking":
            return "<think>" + "reasoning\n" * 1000 + f"</think>\n```text\n{message}\n```"
        return message

    result = git.execute(repository, value, intent(value), generate_message=generate)
    assert result.sha
    assert seen[0]["deleted_files"] == ["src/a.txt"]
    assert "base" not in seen[0]["staged_diff"]
    assert "new content" in seen[0]["staged_diff"]
    assert "outside selected node" not in seen[0]["staged_diff"]
    committed = command(repository, "log", "-1", "--format=%B")
    assert committed.startswith(message)
    assert "think" not in committed.lower() and "reasoning" not in committed
    assert result.intent.message == message
    assert command(repository, "diff", "--cached", "--name-only") == ""
    git.execute(
        repository,
        value,
        result.intent,
        recover_only=True,
        generate_message=lambda _: pytest.fail("Recovery must not regenerate"),
    )


def test_large_staged_diff_reaches_generator_and_commits_without_truncation(repository):
    value = baseline(repository)
    content = "test report entry\n" * 40000 + "end of report\n"
    assert len(content.encode()) > 512 * 1024
    (repository / "src/report.txt").write_text(content, encoding="utf-8", newline="\n")
    seen = []

    def generate(diff):
        seen.append(diff)
        return "test: save complete report"

    result = git.execute(repository, value, intent(value), generate_message=generate)
    assert result.sha
    assert len(seen) == 1
    expected_patch = "".join(f"+{line}\n" for line in content.splitlines())
    assert expected_patch in seen[0]["staged_diff"]
    assert command(repository, "show", "HEAD:src/report.txt") == content.strip()
    assert command(repository, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "invalid_message",
    [
        "  ",
        "<think>Only reasoning</think>",
        "<think>Unclosed reasoning\nfix: draft answer",
        "reasoning</think>\nfix: draft answer",
        "<think>Outer<think>Inner</think>Unfinished outer\nfix: draft answer",
    ],
)
def test_no_diff_skips_generation_and_failure_does_not_commit(repository, invalid_message):
    value = baseline(repository)
    assert git.execute(
        repository, value, intent(value), generate_message=lambda _: pytest.fail("No diff")
    ).no_changes
    (repository / "src/a.txt").write_text("changed")
    with pytest.raises(git.GitCommitError, match="empty or invalid"):
        git.execute(repository, value, intent(value), generate_message=lambda _: invalid_message)
    assert command(repository, "rev-list", "--count", "HEAD") == "1"
    assert command(repository, "diff", "--cached", "--name-only") == ""


def test_generation_cannot_commit_files_changed_during_llm_call(repository):
    value = baseline(repository)
    (repository / "src/a.txt").write_text("staged content")

    def generate(_):
        (repository / "src/a.txt").write_text("changed during generation")
        return "fix: change file"

    with pytest.raises(git.GitCommitError, match="during message generation"):
        git.execute(repository, value, intent(value), generate_message=generate)
    assert command(repository, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize("fail", [False, True])
def test_real_git_node_generates_with_custom_settings(
    authenticated, repository, provider, settings, monkeypatch, fail
):
    requests = []
    original_run = HttpLLMAdapter.run
    message = "feat(src): implement plan item\n\n- Add the requested implementation"

    def llm(self, request):
        if request.role != "git_commit_message":
            return original_run(self, request)
        requests.append(request)
        if fail:
            return LLMResult(
                ExternalOutcome.UNAVAILABLE,
                "",
                None,
                None,
                error=AdapterError("rate_limit", "Busy", "safe"),
                no_effect=True,
            )
        return LLMResult(
            ExternalOutcome.SUCCEEDED,
            f"<think>Inspect the staged diff before writing the message.</think>\n{message}",
            {"text": message},
            None,
            tokens_used=12,
            budget_quality="observed",
        )

    monkeypatch.setattr(HttpLLMAdapter, "run", llm)
    response, factory = start_preset(
        authenticated,
        repository,
        provider,
        commit_generation={
            "model": "message-model",
            "prompt": "Custom node prompt",
            "language": "en",
            "params": {"temperature": 0.1},
        },
    )
    assert response.status_code == 201, response.text
    result = execute_run(response.json(), factory, settings, Agent())
    assert requests
    request = requests[0]
    assert request.prompt.startswith("Custom node prompt")
    assert "English" in request.prompt
    assert request.model_id == "message-model" and request.params == {"temperature": 0.1}
    assert request.connection["base_url"] == provider
    assert "P1.txt" in request.context_package["evidence"]["staged_diff"]
    if fail:
        assert result.final_state == "waiting_input"
        assert command(repository, "rev-list", "--count", "HEAD") == "1"
    else:
        assert result.final_state == "completed", result
        assert len(requests) == 2
        assert command(repository, "log", "-1", "--format=%B").startswith(message)
        with factory() as session:
            runtime = json.loads(session.get(Run, response.json()["id"]).runtime_json)
            assert runtime["tokens_used"] >= 24
