"""Council via harness: participants use agent adapters (Codex/OpenCode)."""

from __future__ import annotations

import json
from uuid import uuid4

from council_support import document
from sqlalchemy import select

from agents_ide.domain.planning import PlanningJobCreate
from agents_ide.engine.planning_worker import dispatch_planning_job
from agents_ide.persistence.models import PlanningAttempt, PlanningMember
from agents_ide.services.planning import create_planning_job


def create_simulated(client, payload):
    with client.app.state.session_factory() as session:
        job = create_planning_job(
            session, PlanningJobCreate.model_validate(payload), simulated=True
        )
        session.commit()
        job_id = job.id
    return client.get(f"/api/planning_jobs/{job_id}").json()


def _create_harness(client, headers, *, name, kind, settings):
    suffix = uuid4().hex
    payload = {
        "name": name + "-" + suffix[:8],
        "harness_kind": kind,
        "settings": settings,
    }
    response = client.post("/api/harness_profiles", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _setup_with_harness(client, headers, tmp_path, *, kind, settings, count=2):
    suffix = uuid4().hex
    workspace = tmp_path / suffix
    workspace.mkdir()
    project = client.post(
        "/api/projects", headers=headers, json={"name": suffix, "workspace_path": str(workspace)}
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "Council"}
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "llm-" + suffix[:6], "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    harness = _create_harness(client, headers, name="h-" + kind, kind=kind, settings=settings)
    participants = [
        {
            "role": "participant",
            "selection": {
                "kind": "direct",
                "model_id": f"model-{i}",
                "harness_profile_id": harness["id"],
            },
        }
        for i in range(count)
    ]
    merger = {
        "role": "merger",
        "selection": {
            "kind": "direct",
            "model_id": "merger",
            "provider_connection_id": connection["id"],
        },
    }
    payload = {
        "project_id": project["id"],
        "chat_id": chat["id"],
        "task_text": "Plan a feature",
        "context_text": "Pinned context marker",
        "participants": [*participants, merger],
        "idempotency_key": suffix,
    }
    return project, chat, harness, connection, payload


def test_direct_harness_member_accepted_and_merger_receives_drafts(authenticated, tmp_path):
    client, headers = authenticated
    project, chat, harness, _, payload = _setup_with_harness(
        client,
        headers,
        tmp_path,
        kind="opencode",
        settings={"permission_mode": "no_tools"},
        count=2,
    )
    job = create_simulated(client, payload)
    assert "id" in job, job
    plan_text_a = json.dumps(document("Harness draft A"))
    plan_text_b = json.dumps(document("Harness draft B"))
    dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {
                    "node_id": f"council_participant_{i}",
                    "attempt_index": 1,
                    "raw_text": plan_text_a if i == 0 else plan_text_b,
                }
                for i in range(2)
            ]
            + [
                {
                    "node_id": "council_merger",
                    "attempt_index": 1,
                    "raw_text": json.dumps(document("Unified plan")),
                }
            ],
            "harness_body_text:council_participant_0": plan_text_a,
            "harness_body_text:council_participant_1": plan_text_b,
        },
    )
    job_view = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job_view["state"] == "ready_for_confirmation", job_view.get("last_error")
    assert job_view["n_participants_actual"] == 2
    participants = [m for m in job_view["members"] if m["role"] == "participant"]
    assert all(p["selected_harness_id"] == harness["id"] for p in participants)
    assert all(p["selected_harness_kind"] == "opencode" for p in participants)
    assert all(p["selected_model_id"] for p in participants)
    drafts = [d for d in job_view["drafts"] if d["role"] == "participant"]
    assert all(d["accepted"] for d in drafts)
    assert len({d["content_hash"] for d in drafts}) == 2


def test_harness_participants_with_invalid_read_only_policy_rejected(authenticated, tmp_path):
    client, headers = authenticated
    project, chat, harness_bad, _, payload = _setup_with_harness(
        client,
        headers,
        tmp_path,
        kind="opencode",
        settings={"permission_mode": "allow"},
        count=2,
    )
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 422, response.text
    code = response.json()["code"]
    assert code in {"council_harness_unverified", "validation_error"}


def test_codex_harness_participant_accepted_with_read_only_never(authenticated, tmp_path):
    client, headers = authenticated
    project, chat, harness, _, payload = _setup_with_harness(
        client,
        headers,
        tmp_path,
        kind="codex",
        settings={"permission_mode": "read_only", "approval_policy": "never"},
        count=2,
    )
    job = create_simulated(client, payload)
    assert "id" in job
    plan_text = json.dumps(document("Codex draft"))
    dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {
                    "node_id": f"council_participant_{i}",
                    "attempt_index": 1,
                    "raw_text": plan_text,
                }
                for i in range(2)
            ]
            + [
                {
                    "node_id": "council_merger",
                    "attempt_index": 1,
                    "raw_text": json.dumps(document("Unified plan")),
                }
            ],
            "harness_body_text": plan_text,
        },
    )
    job_view = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job_view["state"] == "ready_for_confirmation", job_view.get("last_error")
    assert job_view["n_participants_actual"] == 2
    members = [m for m in job_view["members"] if m["role"] == "participant"]
    assert all(m["selected_harness_id"] == harness["id"] for m in members)
    assert all(m["selected_harness_kind"] == "codex" for m in members)
    assert all(m["status"] == "succeeded" for m in members)


def test_harness_group_supports_multiple_independent_candidates(authenticated, tmp_path):
    client, headers = authenticated
    suffix = uuid4().hex
    workspace = tmp_path / suffix
    workspace.mkdir()
    project = client.post(
        "/api/projects", headers=headers, json={"name": suffix, "workspace_path": str(workspace)}
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "Council"}
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "llm-" + suffix[:6], "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    harness_a = _create_harness(
        client, headers, name="a", kind="opencode", settings={"permission_mode": "no_tools"}
    )
    harness_b = _create_harness(
        client, headers, name="b", kind="opencode", settings={"permission_mode": "no_tools"}
    )
    group = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": "agent-pair",
            "members": [
                {"harness_profile_id": harness_a["id"], "model_id": "alpha"},
                {"harness_profile_id": harness_b["id"], "model_id": "beta"},
            ],
        },
    ).json()
    payload = {
        "project_id": project["id"],
        "chat_id": chat["id"],
        "task_text": "Plan via agent group",
        "context_text": "ctx",
        "participants": [
            {
                "role": "participant",
                "selection": {"kind": "group", "group_id": group["id"]},
            },
            {
                "role": "participant",
                "selection": {
                    "kind": "direct",
                    "model_id": "gamma",
                    "harness_profile_id": harness_a["id"],
                },
            },
            {
                "role": "merger",
                "selection": {
                    "kind": "direct",
                    "model_id": "merger",
                    "provider_connection_id": connection["id"],
                },
            },
        ],
        "idempotency_key": suffix,
    }
    job = create_simulated(client, payload)
    plan_text = json.dumps(document("Draft"))
    dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {"node_id": "council_participant_0", "raw_text": plan_text},
                {"node_id": "council_participant_1", "raw_text": plan_text},
                {"node_id": "council_merger", "raw_text": json.dumps(document("Unified"))},
            ],
            "harness_body_text": plan_text,
        },
    )
    job_view = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job_view["state"] == "ready_for_confirmation"
    # Group participant selected one harness, the direct participant the other;
    # they are independent.
    members = [m for m in job_view["members"] if m["role"] == "participant"]
    kinds = {m["selected_harness_kind"] for m in members}
    assert kinds == {"opencode"}


def test_harness_member_with_unknown_or_archived_profile_rejected(authenticated, tmp_path):
    client, headers = authenticated
    suffix = uuid4().hex
    workspace = tmp_path / suffix
    workspace.mkdir()
    project = client.post(
        "/api/projects", headers=headers, json={"name": suffix, "workspace_path": str(workspace)}
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats", headers=headers, json={"title": "Council"}
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": "llm-" + suffix[:6], "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    harness = _create_harness(
        client, headers, name="h", kind="opencode", settings={"permission_mode": "no_tools"}
    )
    archive = client.post(
        f"/api/harness_profiles/{harness['id']}/archive?expected_version={harness['version']}",
        headers=headers,
    )
    assert archive.status_code == 200
    payload = {
        "project_id": project["id"],
        "chat_id": chat["id"],
        "task_text": "Plan",
        "context_text": "ctx",
        "participants": [
            {
                "role": "participant",
                "selection": {
                    "kind": "direct",
                    "model_id": "x",
                    "harness_profile_id": harness["id"],
                },
            },
            {
                "role": "participant",
                "selection": {
                    "kind": "direct",
                    "model_id": "y",
                    "provider_connection_id": connection["id"],
                },
            },
            {
                "role": "merger",
                "selection": {
                    "kind": "direct",
                    "model_id": "merger",
                    "provider_connection_id": connection["id"],
                },
            },
        ],
        "idempotency_key": suffix,
    }
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "harness_unavailable"


def test_harness_attempt_uses_agent_request_kind(authenticated, tmp_path):
    """Captured attempts must mark ``kind='agent'`` and the right harness ID."""
    client, headers = authenticated
    project, chat, harness, _, payload = _setup_with_harness(
        client,
        headers,
        tmp_path,
        kind="opencode",
        settings={"permission_mode": "no_tools"},
        count=2,
    )
    job = create_simulated(client, payload)
    plan_text = json.dumps(document("Harness attempt"))
    dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {"node_id": "council_participant_0", "raw_text": plan_text},
            ],
            "harness_body_text": plan_text,
        },
    )
    factory = client.app.state.session_factory
    with factory() as session:
        members = session.scalars(
            select(PlanningMember).where(PlanningMember.job_id == job["id"])
        ).all()
        attempts = session.scalars(
            select(PlanningAttempt).where(PlanningAttempt.job_id == job["id"])
        ).all()
    assert any(m.harness_profile_id == harness["id"] for m in members)
    harness_member_ids = {m.id for m in members if m.harness_profile_id == harness["id"]}
    harness_attempts = [a for a in attempts if a.member_id in harness_member_ids]
    assert harness_attempts, "expected at least one harness attempt to be recorded"
    assert all(
        json.loads(a.candidate_json).get("kind") == "agent"
        for a in harness_attempts
        if a.candidate_json
    )
