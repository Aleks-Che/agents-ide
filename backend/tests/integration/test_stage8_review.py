"""Stage 8 acceptance through API, fixed plans and normal engine execution."""

import json
import re
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture

from agents_ide.adapters.base import AgentAdapter, AgentResult, ExternalOutcome
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.domain.graph_validation import validate_graph
from agents_ide.engine import plan_control, queue
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    PipelineVersion,
    PlanItem,
    Run,
    StepAttempt,
)
from agents_ide.services.presets import ensure_preset_installed, get_builtin_preset

repository = repository_fixture


class Agent(AgentAdapter):
    name = "test-agent"

    def __init__(self, fail_first=False, fail_review=False):
        self.calls = Counter()
        self.requests = []
        self.fail_first, self.fail_review = fail_first, fail_review

    def run(self, request):
        self.requests.append(request)
        work = request.context_package["work"]
        current = work["current_plan_item_id"]
        self.calls[(request.role, current)] += 1
        workspace = Path(request.workspace_path)
        if request.role == "implementer":
            value = (
                "bad"
                if self.fail_first and current == "P1" and self.calls[(request.role, current)] == 1
                else f"good-{self.calls[(request.role, current)]}"
            )
            (workspace / "src" / f"{current}.txt").write_text(value)
            body = {"output": value}
        else:
            ids = work["plan_item_ids"] if request.role == "plan_checker" else [current]
            rows = []
            for item in ids:
                ok = (workspace / "src" / f"{item}.txt").exists()
                if (
                    request.role == "reviewer"
                    and self.fail_review
                    and current == "P1"
                    and self.calls[(request.role, current)] == 1
                ):
                    ok = False
                rows.append(
                    {
                        "plan_item_id": item,
                        "verdict": "passed" if ok else "failed",
                        "evidence_ids": [],
                        "findings": [] if ok else ["implementation missing or needs review fix"],
                    }
                )
            body = {
                "verdict": "passed" if all(r["verdict"] == "passed" for r in rows) else "failed",
                "item_results": rows,
                "missing_evidence": [],
            }
        return AgentResult(ExternalOutcome.SUCCEEDED, json.dumps(body), body, body.get("verdict"))


class Provider(BaseHTTPRequestHandler):
    requests = []
    workspace = None
    inconclusive = False
    asked = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        type(self).requests.append(body)
        match = re.search(r"выбранный ID (P\d+)", prompt)
        ids = [match.group(1)] if match else ["P1", "P2"]
        rows = [
            {
                "plan_item_id": i,
                "verdict": "passed" if (self.workspace / "src" / f"{i}.txt").exists() else "failed",
                "evidence_ids": [],
                "findings": []
                if (self.workspace / "src" / f"{i}.txt").exists()
                else ["not implemented"],
            }
            for i in ids
        ]
        missing = []
        if self.inconclusive and not self.asked:
            type(self).asked = True
            for row in rows:
                row["verdict"] = "inconclusive"
            missing = [
                {
                    "kind": "command_report",
                    "command_id": "verify",
                    "reason": "repeat the configured check",
                }
            ]
        response = {
            "verdict": "passed"
            if all(x["verdict"] == "passed" for x in rows)
            else "inconclusive"
            if missing
            else "failed",
            "item_results": rows,
            "missing_evidence": missing,
        }
        payload = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(response)}}],
                "usage": {"total_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def provider(repository):
    Provider.requests = []
    Provider.workspace = repository
    Provider.inconclusive = False
    Provider.asked = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def start_preset(
    authenticated,
    repository,
    provider,
    *,
    inputs=None,
    roles=True,
    overrides=None,
    commit_generation=None,
):
    client, headers = authenticated
    preset = client.get("/api/presets", headers=headers).json()[0]
    versions = client.get(f"/api/templates/{preset['template_id']}/versions", headers=headers)
    assert versions.status_code == 200, versions.text
    project = client.post(
        "/api/projects", headers=headers, json={"name": "stage8", "workspace_path": str(repository)}
    ).json()
    connection = client.post(
        "/api/connections", headers=headers, json={"name": "local", "base_url": provider}
    ).json()
    version_id = versions.json()[0]["id"]
    if commit_generation is not None:
        version = versions.json()[0]
        graph = version["graph"]
        for node in graph["nodes"]:
            if node["type"] == "GitCommit":
                node["config"].update(
                    generate_message=True,
                    message_generation={
                        "connection_id": connection["id"],
                        **commit_generation,
                    },
                )
        template = client.post(
            "/api/templates", headers=headers, json={"name": "Generated commits"}
        ).json()
        created = client.post(
            f"/api/templates/{template['id']}/versions",
            headers=headers,
            json={
                "graph": graph,
                "inputs": version["inputs"],
                "settings": version["settings"],
            },
        )
        assert created.status_code == 201, created.text
        version_id = created.json()["id"]
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "fake",
            "harness_kind": "codex",
            "settings": {"permission_mode": "read_only"},
        },
    ).json()
    selections = {
        r: {"kind": "direct", "model_id": "test", "harness_profile_id": profile["id"]}
        for r in ["implementer", "reviewer", "plan_checker"]
    }
    selections["verifier"] = {
        "kind": "direct",
        "model_id": "test",
        "provider_connection_id": connection["id"],
    }
    binding = client.post(
        f"/api/versions/{version_id}/bindings",
        headers=headers,
        json={
            "project_id": project["id"],
            "name": "plan",
            "model_selections": selections if roles else {},
        },
    ).json()
    values = {
        "task": "implement two items",
        "plan": "First and second feature",
        "plan_items": [
            {"title": "First", "acceptance_criteria": ["first works"]},
            {"title": "Second", "acceptance_criteria": ["second works"]},
        ],
        "git_allowlist": ["src/**"],
        "context_paths": ["src/**"],
        "verification_commands": [
            {
                "id": "verify",
                "program": sys.executable,
                "args": [
                    "-c",
                    "from pathlib import Path; import sys; "
                    "files=list(Path('src').glob('P*.txt')); print('checked',len(files)); "
                    "sys.exit(any(p.read_text()=='bad' for p in files))",
                ],
                "required": True,
                "retry_safety": "safe",
                "success_exit_codes": [0],
            }
        ],
    }
    if inputs:
        values.update(inputs)
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "start",
            "idempotency_key": "stage8",
            "inputs": values,
            "overrides": overrides or {},
        },
    )
    return response, client.app.state.session_factory


def execute_run(run, factory, settings, agent):
    job = queue.claim_next_job(factory, worker_id="review8", lease_seconds=300)
    assert job and job.run_id == run["id"]
    runner = Runner(
        session_factory=factory,
        worker_id="review8",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    runner._build_adapters = lambda snapshot: (agent, HttpLLMAdapter())
    errors = []
    monitored = runner._call_monitored

    def debug_call(fn, *args, **kwargs):
        def invoke():
            try:
                return fn()
            except Exception:
                import traceback

                errors.append(traceback.format_exc())
                raise

        return monitored(invoke, *args, **kwargs)

    runner._call_monitored = debug_call
    result = runner.execute(run["id"])
    assert not errors, "\n".join(errors)
    return result


def test_builtin_is_valid_idempotent_and_available_through_api(authenticated):
    client, headers = authenticated
    assert validate_graph(get_builtin_preset("development_with_plan").graph).ok
    factory = client.app.state.session_factory
    with factory() as session:
        before = list(session.scalars(select(PipelineVersion)))
        template = ensure_preset_installed(session, None, "development_with_plan")
        session.commit()
        assert len(list(session.scalars(select(PipelineVersion)))) == len(before)
    response = client.get("/api/templates", headers=headers)
    assert response.status_code == 200, response.text
    assert any(x["id"] == template.id and x["kind"] == "system" for x in response.json())


def test_preset_cannot_start_without_roles(authenticated, repository, provider):
    response, _ = start_preset(authenticated, repository, provider, roles=False)
    assert response.status_code == 422, response.text


def test_nonoverlap_requires_enforced_writer_isolation(authenticated, repository, provider):
    response, _ = start_preset(
        authenticated, repository, provider, overrides={"dirty_policy": "allow_nonoverlap"}
    )
    assert response.status_code == 422, response.text
    assert "policy_unsupported" in response.text


def test_preset_update_keeps_user_copy_and_validates_group_resources(authenticated, monkeypatch):
    from agents_ide.persistence.models import HarnessProfile, PipelineTemplate
    from agents_ide.services import presets

    client, headers = authenticated
    factory = client.app.state.session_factory
    definition = presets.get_builtin_preset("development_with_plan")
    copy = client.post("/api/templates", headers=headers, json={"name": definition.name}).json()
    published = client.post(
        f"/api/templates/{copy['id']}/versions",
        headers=headers,
        json={"graph": definition.graph, "settings": definition.default_settings},
    )
    assert published.status_code == 201, published.text
    body = json.loads(json.dumps(definition.body))
    next(n for n in body["graph"]["nodes"] if n["id"] == "implementer")["config"]["prompt"] += (
        "\nUpdated policy"
    )
    replacement = presets.PresetDefinition(definition.name, body, definition.source_path)
    monkeypatch.setattr(presets, "get_builtin_preset", lambda _: replacement)
    with factory() as session:
        system = presets.ensure_preset_installed(session, None, definition.preset_id)
        session.commit()
        assert (
            len(
                list(
                    session.scalars(
                        select(PipelineVersion).where(PipelineVersion.template_id == system.id)
                    )
                )
            )
            == 2
        )
        assert (
            session.get(PipelineVersion, published.json()["id"]).execution_hash
            == published.json()["execution_hash"]
        )
        assert session.get(PipelineTemplate, copy["id"]).kind == "user"
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={
            "name": "profile",
            "harness_kind": "codex",
            "settings": {"permission_mode": "read_only"},
        },
    ).json()
    group = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "heavy",
            "members": [{"harness_profile_id": profile["id"], "model_id": "test"}],
        },
    ).json()
    selections = {r: {"kind": "group", "group_id": group["id"]} for r in definition.graph["roles"]}
    with factory() as session:
        report = presets.binding_readiness(session, definition.preset_id, selections)
        assert report["roles"]["implementer"]["ready"]
        assert not report["roles"]["verifier"]["ready"]
        session.get(HarnessProfile, profile["id"]).archived_at = 1
        session.flush()
        assert not presets.binding_readiness(session, definition.preset_id, selections)["roles"][
            "implementer"
        ]["ready"]


def test_upgrade_from_stage8_preserves_fixed_plan(authenticated, repository, provider, settings):
    from alembic import command as alembic_command
    from alembic.config import Config

    from agents_ide.persistence import database

    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    with factory() as session:
        before = [
            (i.id, i.title, i.acceptance_criteria_json) for i in session.scalars(select(PlanItem))
        ]
    engine = database.create_database(settings)
    config = Config()
    config.set_main_option("script_location", str(Path(database.__file__).parent / "migrations"))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            alembic_command.downgrade(config, "0010_stage8_plan")
        database.migrate(settings)
        assert database.check_database(engine)
        with factory() as session:
            assert [
                (i.id, i.title, i.acceptance_criteria_json)
                for i in session.scalars(select(PlanItem))
            ] == before
    finally:
        engine.dispose()


def test_plain_text_plan_becomes_one_fixed_item(authenticated, repository, provider):
    response, factory = start_preset(
        authenticated, repository, provider, inputs={"plan_items": None}
    )
    # An explicit null does not satisfy the optional array contract; omit it instead at API level.
    assert response.status_code == 422
    assert plan_control.normalize_plan({"plan": "whole original text"})["items"] == [
        {"id": "P1", "title": "whole original text", "acceptance_criteria": ["whole original text"]}
    ]


def test_two_item_preset_repairs_review_and_finishes_via_real_git_http(
    authenticated, repository, provider, settings
):
    Provider.inconclusive = True
    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    run = response.json()
    agent = Agent(fail_first=True, fail_review=True)
    result = execute_run(run, factory, settings, agent)
    assert result.final_state == "completed", result
    assert Provider.asked
    client, headers = authenticated
    projection = client.get(f"/api/runs/{run['id']}/plan", headers=headers).json()
    assert [x["status"] for x in projection["items"]] == ["done", "done"]
    assert all(x["commit_shas"] and x["evidence_ids"] for x in projection["items"])
    assert command(repository, "branch", "--show-current") == f"agents-ide/run/{run['id']}"
    assert command(repository, "rev-list", "--count", "master..HEAD") == "2"
    assert command(repository, "status", "--porcelain") == ""
    modes = [r.context_package["work"]["mode"] for r in agent.requests if r.role == "implementer"]
    assert modes == ["initial", "repair", "repair", "next_item"]
    final_requests = [
        x for x in Provider.requests if "все исходные ID" in x["messages"][0]["content"]
    ]
    assert final_requests and "commits" in final_requests[-1]["messages"][0]["content"]
    assert "First and second feature" in final_requests[-1]["messages"][0]["content"]
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert json.loads(saved.snapshot_json)["plan"]["items"][0]["id"] == "P1"
        assert (
            session.scalar(select(StepAttempt).where(StepAttempt.error_code.is_not(None))) is None
        )


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"plan_item_id": "P1", "verdict": "passed"}],
        [{"plan_item_id": "P1", "verdict": "passed"}, {"plan_item_id": "P1", "verdict": "passed"}],
        [{"plan_item_id": "P1", "verdict": "passed"}, {"plan_item_id": "P3", "verdict": "passed"}],
    ],
)
def test_full_plan_check_rejects_missing_duplicate_unknown_ids(results):
    with pytest.raises(AppError):
        plan_control.validate_item_results(results, ["P1", "P2"])


def test_fixed_plan_projection_and_evidence_guard(authenticated, repository, provider):
    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    with factory() as session:
        fixed = json.loads(session.get(Run, run_id).snapshot_json)["plan"]["items"]
        assert len(plan_control.upsert_plan_items(session, run_id, fixed)) == 2
        changed = json.loads(json.dumps(fixed))
        changed[0]["title"] = "different"
        with pytest.raises(AppError, match="immutable"):
            plan_control.upsert_plan_items(session, run_id, changed)
        assert plan_control.select_next_item(session, run_id).current == "P1"
        assert plan_control.select_next_item(session, run_id).current == "P1"
        with pytest.raises(AppError):
            plan_control.record_verified(
                session, run_id, "P1", verdict="passed", evidence_ids=["invented"]
            )
        from agents_ide.engine.artifacts import ArtifactPayload, record_artifact

        proof = record_artifact(
            session, run_id, ArtifactPayload("test_evidence", body={"proof": 1})
        )
        plan_control.record_verified(
            session, run_id, "P1", verdict="passed", evidence_ids=[proof.id]
        )
        assert plan_control.plan_summary(session, run_id).current == "P2"
        rows = [
            {
                "plan_item_id": "P1",
                "verdict": "failed",
                "evidence_ids": [proof.id],
                "findings": ["defect"],
            },
            {"plan_item_id": "P2", "verdict": "passed", "evidence_ids": [proof.id], "findings": []},
        ]
        with pytest.raises(AppError, match="new concrete defect"):
            plan_control.record_final_check(session, run_id, rows)
        proof2 = record_artifact(
            session, run_id, ArtifactPayload("test_evidence", body={"proof": 2})
        )
        rows[0]["evidence_ids"] = [proof2.id]
        summary, defects = plan_control.record_final_check(session, run_id, rows)
        assert defects == ["P1"] and summary.remaining == ("P1", "P2")
        assert (
            next(
                i for i in plan_control.load_plan_items(session, run_id) if i.item_id == "P2"
            ).status
            == "pending"
        )


def test_database_rejects_plan_definition_changes(authenticated, repository, provider):
    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with factory() as session:
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(text("UPDATE plan_items SET title='changed' WHERE item_id='P1'"))
        session.rollback()


def test_no_changes_still_checks_all_remaining_plan_items(
    authenticated, repository, provider, settings
):
    for item in ["P1", "P2"]:
        (repository / "src" / f"{item}.txt").write_text("already implemented")
    command(repository, "add", ".")
    command(repository, "commit", "-qm", "existing implementation")

    class ExistingAgent(Agent):
        def run(self, request):
            if request.role == "implementer":
                self.requests.append(request)
                return AgentResult(ExternalOutcome.SUCCEEDED, "{}", {}, None)
            return super().run(request)

    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    result = execute_run(response.json(), factory, settings, ExistingAgent())
    assert result.final_state == "completed", result
    assert command(repository, "rev-list", "--count", "master..HEAD") == "0"
    with factory() as session:
        items = list(session.scalars(select(PlanItem)))
        assert len(items) == 2 and all(i.status == "done" for i in items)
        assert all(json.loads(i.commit_shas_json) == [] for i in items)


@pytest.mark.parametrize("crash_node", ["commit", "select_next_item"])
def test_crash_reconciles_commit_and_atomic_plan_projection(
    authenticated, repository, provider, settings, monkeypatch, crash_node
):
    from agents_ide.persistence.models import QueueJob

    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    agent = Agent()
    original = Runner._finish_visit
    crashed = False

    def finish(self, node, visit, result):
        nonlocal crashed
        # Git crash is after actual commit and durable intent, before the
        # attempt result. Plan crash is after the atomic projection/result.
        if node["id"] == crash_node and not crashed:
            crashed = True
            raise RuntimeError("injected worker crash")
        return original(self, node, visit, result)

    original_save = Runner._save_attempt

    def save(self, visit, *args, **kwargs):
        nonlocal crashed
        if (
            crash_node == "commit"
            and self.nodes[visit.node_id]["type"] == "GitCommit"
            and not crashed
        ):
            crashed = True
            raise RuntimeError("injected worker crash")
        return original_save(self, visit, *args, **kwargs)

    monkeypatch.setattr(Runner, "_finish_visit", finish)
    monkeypatch.setattr(Runner, "_save_attempt", save)
    with pytest.raises(RuntimeError, match="injected worker crash"):
        execute_run(response.json(), factory, settings, agent)
    assert crashed
    with factory() as session:
        job = session.scalar(select(QueueJob))
        job.lease_expires_at = 0
        job.owner_pid, job.owner_create_time = None, None
        job.owner_pid, job.owner_create_time = 99999999, 1
        row = session.get(Run, response.json()["id"])
        runtime = json.loads(row.runtime_json)
        runtime.pop("active_call_owner", None)
        row.runtime_json = json.dumps(runtime)
        session.commit()
    result = execute_run(response.json(), factory, settings, agent)
    assert result.final_state == "completed", result
    assert command(repository, "rev-list", "--count", "master..HEAD") == "2"
    assert agent.calls[("implementer", "P1")] == 1
    assert agent.calls[("implementer", "P2")] == 1


def test_paused_run_detects_external_file_change(
    authenticated, repository, provider, settings, monkeypatch
):
    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    client, headers = authenticated
    run_id = response.json()["id"]

    def control(kind):
        current = client.get(f"/api/runs/{run_id}", headers=headers).json()
        result = client.post(
            f"/api/runs/{run_id}/commands",
            headers=headers,
            json={
                "command_type": kind,
                "command_id": kind,
                "expected_state_version": current["state_version"],
            },
        )
        assert result.status_code == 200, result.text

    original = Runner._finish_visit

    def finish(self, node, visit, result):
        result = original(self, node, visit, result)
        if node["id"] == "select_next_item":
            control("pause")
        return result

    monkeypatch.setattr(Runner, "_finish_visit", finish)
    agent = Agent()
    result = execute_run(response.json(), factory, settings, agent)
    assert result.final_state == "paused", result
    (repository / "src" / "a.txt").write_text("external edit while paused")
    control("resume")
    result = execute_run(response.json(), factory, settings, agent)
    assert result.waiting_reason.code == "external_change_detected", result
    assert not agent.calls


def test_unchanged_repairs_wait_for_no_progress(authenticated, repository, provider, settings):
    settings.enforce_execution_limits = True

    class StalledAgent(Agent):
        def run(self, request):
            result = super().run(request)
            if request.role == "implementer":
                (repository / "src" / "P1.txt").write_text("bad")
            return result

    response, factory = start_preset(authenticated, repository, provider)
    assert response.status_code == 201, response.text
    agent = StalledAgent()
    result = execute_run(response.json(), factory, settings, agent)
    assert result.waiting_reason.code == "no_progress", result
    assert agent.calls[("implementer", "P1")] == 3
    assert command(repository, "rev-list", "--count", "master..HEAD") == "0"
