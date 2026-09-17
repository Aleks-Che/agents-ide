"""Council's native dispatch with real owned fixture processes, no model calls."""

import json
import os
import sys
from pathlib import Path

import pytest
from council_support import document
from sqlalchemy import select
from test_stage9a_harness import _setup_with_harness

from agents_ide.adapters.codex import CodexStream
from agents_ide.domain.common import utc_now
from agents_ide.domain.planning import PlanningRetryRequest
from agents_ide.domain.planning_document import PlanDocument
from agents_ide.engine.codex_runtime import CodexRuntime
from agents_ide.engine.planning_native import PlanningSupervisor, processes_stopped
from agents_ide.engine.planning_worker import _prepare, claim_planning_job, dispatch_planning_job
from agents_ide.errors import AppError
from agents_ide.persistence.models import PlanningAttempt, PlanningJob
from agents_ide.security.codex_policy import config_for
from agents_ide.services.planning import retry_planning_job
from agents_ide.worker.processes import ProcessRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Council Windows isolation")
FIXTURE = Path(__file__).parents[1] / "fixtures/fake_codex_server.py"


def test_native_plan_schema_requires_defaulted_question_fields():
    schema = PlanDocument.native_output_schema()
    question = schema["$defs"]["PlanQuestion"]
    assert set(question["required"]) == set(question["properties"])
    assert "default" not in question["properties"]["allow_text"]
    assert question["additionalProperties"] is False


def setup_native(authenticated, tmp_path):
    client, headers = authenticated
    project, _, profile, _, payload = _setup_with_harness(
        client, headers, tmp_path, kind="codex", settings={"permission_mode": "read_only"}
    )
    response = client.patch(
        f"/api/harness_profiles/{profile['id']}",
        headers=headers,
        json={"expected_version": profile["version"], "executable_path": sys.executable},
    )
    assert response.status_code == 200, response.text
    payload["participants"][-1]["selection"] = {
        "kind": "direct",
        "model_id": "merger",
        "harness_profile_id": profile["id"],
    }
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return client, response.json(), project


def test_native_council_uses_owned_fresh_sessions_and_only_merger_sees_drafts(
    authenticated, tmp_path, monkeypatch
):
    client, job, project = setup_native(authenticated, tmp_path)
    roots, prompts, rows = [], [], []
    artifacts = []

    def start(**kwargs):
        root, supervisor, attempt = (
            kwargs["workspace_path"],
            kwargs["supervisor"],
            kwargs["attempt_id"],
        )
        assert kwargs["isolated_read"] is True
        roots.append(root)
        prompts.append((root / "request.md").read_text(encoding="utf-8"))
        artifacts.append([p.read_text(encoding="utf-8") for p in root.glob("draft-*.json")])
        with supervisor.factory() as session:
            saved = session.get(PlanningAttempt, attempt)
            assert saved.outcome == "running"
            candidate = json.loads(saved.candidate_json)
        env = dict(os.environ)
        env["FAKE_CODEX_FINAL_TEXT"] = json.dumps(
            document("Private draft " + candidate["model_id"])
        )
        entry, process = supervisor.start_stdio(
            [sys.executable, str(FIXTURE)],
            root,
            env,
            role="codex_app_server",
            kind="harness",
            attempt_id=attempt,
            stdin=-1,
        )
        with supervisor.factory() as session:
            ledger = json.loads(session.get(PlanningAttempt, attempt).runtime_json)
            assert ledger["processes"][0]["pid"] == entry.pid
            assert ledger["processes"][0]["job_owned"]
            rows.append(ledger)
        stream = CodexStream.from_process(process)
        stream.group = entry.group
        return CodexRuntime(
            root,
            sys.executable,
            stream,
            entry=entry,
            supervisor=supervisor,
            isolated_config=config_for(root),
        )

    monkeypatch.setattr(CodexRuntime, "start", start)
    result = dispatch_planning_job(client.app.state.session_factory, job["id"])
    assert result.final_state == "ready_for_confirmation", result.error
    assert len(set(roots)) == 3 and len(rows) == 3
    assert all(not path.exists() for path in roots)
    assert all(str(path) != project["workspace"]["normalized_path"] for path in roots)
    assert all("Private draft" not in prompt for prompt in prompts[:2])
    assert "Private draft model-0" in prompts[-1] and "Private draft model-1" in prompts[-1]
    assert artifacts[:2] == [[], []]
    assert len(artifacts[-1]) == 2
    assert "Private draft model-0" in "".join(artifacts[-1])
    with client.app.state.session_factory() as session:
        attempts = list(
            session.scalars(select(PlanningAttempt).where(PlanningAttempt.job_id == job["id"]))
        )
        sessions = {json.loads(a.runtime_json)["session_id"] for a in attempts}
        assert len(sessions) == 3
        assert processes_stopped(session, job["id"])
        assert all(json.loads(a.runtime_json)["event_count"] > 0 for a in attempts)


def test_native_auth_change_requires_explicit_refresh(authenticated, tmp_path, monkeypatch):
    from agents_ide.persistence.models import PlanningEvent, PlanningMember
    from agents_ide.services.planning import _refresh_access, effective_candidate

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth = tmp_path / "auth.json"
    auth.write_text("original-synthetic")
    client, job, _ = setup_native(authenticated, tmp_path)
    factory = client.app.state.session_factory
    auth.write_text("changed-synthetic")
    claim = claim_planning_job(factory, "owner", job["id"])
    assert _prepare(factory, claim) is None
    with factory() as session:
        event = session.scalar(
            select(PlanningEvent).where(
                PlanningEvent.job_id == job["id"],
                PlanningEvent.type == "planning.candidate.skipped",
            )
        )
        assert json.loads(event.payload_json)["reason"] == "native_credentials_changed"
        member = session.get(PlanningMember, event.member_id)
        pinned = json.loads(member.candidates_json)[0]
        assert _refresh_access(session, member)
        current = effective_candidate(member, pinned)
        assert current["harness"]["native_fingerprint"] != pinned["harness"]["native_fingerprint"]
        assert current["harness"]["version"] == pinned["harness"]["version"]


def test_retry_cannot_overlap_a_live_native_process_after_lease_loss(authenticated, tmp_path):
    client, job, _ = setup_native(authenticated, tmp_path)
    factory = client.app.state.session_factory
    claim = claim_planning_job(factory, "owner", job["id"])
    attempt_id, _, _ = _prepare(factory, claim)
    supervisor = PlanningSupervisor(
        factory, ProcessRegistry(), job["id"], "owner", claim.generation
    )
    try:
        supervisor.start(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            tmp_path,
            dict(os.environ),
            kind="harness",
            attempt_id=attempt_id,
        )
        with factory() as session:
            row = session.get(PlanningJob, job["id"])
            row.lease_expires_at = utc_now() - 1
            session.commit()
        assert claim_planning_job(factory, "recovery", job["id"]) is None
        with factory() as session:
            version = session.get(PlanningJob, job["id"]).state_version
            with pytest.raises(AppError) as error:
                retry_planning_job(
                    session,
                    job["id"],
                    PlanningRetryRequest(
                        expected_state_version=version,
                        reset_all_failed=True,
                        acknowledge_unknown_result=True,
                    ),
                )
            assert error.value.code == "planning_retry_worker_active"
    finally:
        assert supervisor.stop()
    with factory() as session:
        result = retry_planning_job(
            session,
            job["id"],
            PlanningRetryRequest(
                expected_state_version=version,
                reset_all_failed=True,
                acknowledge_unknown_result=True,
            ),
        )
        assert result.state == "drafting"
