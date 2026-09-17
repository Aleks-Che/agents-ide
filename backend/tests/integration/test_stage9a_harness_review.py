import json
import threading

import pytest
from sqlalchemy import func, select
from test_stage9a_harness import _create_harness, _setup_with_harness, create_simulated

from agents_ide.adapters.base import AgentAdapterRequest
from agents_ide.domain.planning import PlanningJobCreate, PlanningRetryRequest
from agents_ide.engine import planning_worker
from agents_ide.errors import AppError
from agents_ide.persistence.models import PlanningAttempt, PlanningJob, PlanningMember
from agents_ide.services import planning


def setup_harness(client, headers, tmp_path, **kwargs):
    return _setup_with_harness(
        client,
        headers,
        tmp_path,
        kind=kwargs.get("kind", "opencode"),
        settings=kwargs.get("settings", {"permission_mode": "no_tools"}),
    )


def agent_group(client, headers, profile, name="group"):
    result = client.post(
        "/api/model_groups/agent",
        headers=headers,
        json={
            "name": name,
            "members": [
                {"model_id": "alpha", "harness_profile_id": profile["id"]},
                {"model_id": "beta", "harness_profile_id": profile["id"]},
            ],
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.parametrize("placement", ["participant", "merger", "group"])
def test_real_create_rejects_entire_harness_job_atomically(authenticated, tmp_path, placement):
    client, headers = authenticated
    *_, profile, connection, payload = setup_harness(client, headers, tmp_path)
    if placement == "group":
        group = agent_group(client, headers, profile)
        payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
    elif placement == "merger":
        # Real LLM work must not start before discovering the late harness gate.
        payload["participants"][-1]["selection"] = payload["participants"][0]["selection"]
        for i in range(2):
            payload["participants"][i]["selection"] = {
                "kind": "direct",
                "model_id": f"llm-{i}",
                "provider_connection_id": connection["id"],
            }
    response = client.post("/api/planning_jobs", headers=headers, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "council_harness_real_unverified"
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanningJob)) == 0
        assert session.scalar(select(func.count()).select_from(PlanningMember)) == 0
        assert session.scalar(select(func.count()).select_from(PlanningAttempt)) == 0


def test_existing_mixed_job_real_dispatch_never_calls_any_adapter(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    *_, connection, payload = setup_harness(client, headers, tmp_path)
    payload["participants"][0]["selection"] = {
        "kind": "direct",
        "model_id": "llm-first",
        "provider_connection_id": connection["id"],
    }
    job = create_simulated(client, payload)
    calls = []
    monkeypatch.setattr(planning_worker, "_invoke_external", lambda *a, **kw: calls.append(kw))
    planning_worker.dispatch_planning_job(client.app.state.session_factory, job["id"])
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "failed"
    assert after["last_error"]["code"] == "council_harness_real_unverified"
    assert after["usage"]["external_calls"] == 0
    assert not calls
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={
            "expected_state_version": after["state_version"],
            "reset_all_failed": True,
        },
    )
    assert response.status_code == 422
    assert client.get(f"/api/planning_jobs/{job['id']}").json() == after
    # Replay of a fixture-created request does not bypass the REST gate either.
    assert client.post("/api/planning_jobs", headers=headers, json=payload).status_code == 422


@pytest.mark.parametrize(
    ("kind", "profile_settings"),
    [
        ("opencode", {"permission_mode": "no_tools", "auth": False}),
        ("opencode", {"permission_mode": "no_tools", "serve_args": ["--unsafe"]}),
        ("codex", {"permission_mode": "read_only", "approval_policy": "on-request"}),
        ("codex", {"permission_mode": "read_only", "env": {"CONFIG": "override"}}),
    ],
)
def test_policy_matches_runtime_validation(authenticated, tmp_path, kind, profile_settings):
    client, headers = authenticated
    *_, payload = setup_harness(client, headers, tmp_path, kind=kind, settings=profile_settings)
    with client.app.state.session_factory() as session, pytest.raises(AppError) as error:
        planning.create_planning_job(
            session, PlanningJobCreate.model_validate(payload), simulated=True
        )
    assert error.value.code == "council_harness_unverified"


def test_codex_default_never_policy_matches_profiles_created_in_ui(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_harness(
        client, headers, tmp_path, kind="codex", settings={"permission_mode": "read_only"}
    )
    job = create_simulated(client, payload)
    assert job["state"] == "drafting"


def test_same_model_on_different_harness_is_not_an_independent_vote(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup_harness(client, headers, tmp_path)
    codex = _create_harness(
        client, headers, name="codex", kind="codex", settings={"permission_mode": "read_only"}
    )
    payload["participants"][1]["selection"] = {
        "kind": "direct",
        "harness_profile_id": codex["id"],
        "model_id": payload["participants"][0]["selection"]["model_id"],
    }
    with client.app.state.session_factory() as session, pytest.raises(AppError) as error:
        planning.create_planning_job(
            session, PlanningJobCreate.model_validate(payload), simulated=True
        )
    assert error.value.code == "council_duplicate_member"


def test_simulation_calls_agent_contract_without_workspace_and_passes_drafts_to_merger(
    authenticated,
    tmp_path,
    monkeypatch,
):
    client, headers = authenticated
    project, _, profile, _, payload = setup_harness(client, headers, tmp_path)
    payload["participants"][-1]["selection"] = {
        "kind": "direct",
        "model_id": "merge",
        "harness_profile_id": profile["id"],
    }
    job = create_simulated(client, payload)
    calls = []
    original = planning_worker.FakeAgentAdapter.run

    def capture(self, request):
        calls.append(request)
        return original(self, request)

    monkeypatch.setattr(planning_worker.FakeAgentAdapter, "run", capture)
    planning_worker.dispatch_planning_job(
        client.app.state.session_factory, job["id"], simulated=True
    )
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "ready_for_confirmation", after["last_error"]
    assert len(calls) == 3
    for request in calls:
        assert isinstance(request, AgentAdapterRequest)
        assert request.workspace_path == ""
        assert request.resume_session_id is None
        assert request.context_package["read_workspace_mode"] == "no_workspace_access"
        assert request.context_package["read_manifest_hash"] == job["read_manifest_hash"]
        assert request.stop_event is not None and request.check_owned is not None
    assert calls[0].prompt == calls[1].prompt
    assert "Simulated plan from model-0" not in calls[1].prompt
    assert "Simulated plan from model-0" in calls[2].prompt
    assert "Simulated plan from model-1" in calls[2].prompt
    assert after["usage"]["external_calls"] == 3


def test_group_fallback_uses_attempt_safety_and_reports_actual_candidate(authenticated, tmp_path):
    client, headers = authenticated
    *_, profile, _, payload = setup_harness(client, headers, tmp_path)
    group = agent_group(client, headers, profile)
    payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
    payload["participants"][-1]["selection"] = {
        "kind": "direct",
        "model_id": "merge",
        "harness_profile_id": profile["id"],
    }
    job = create_simulated(client, payload)
    assert job["members"][0]["selected_harness_id"] is None
    assert not job["members"][0]["selected_model_id"]
    planning_worker.dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {
                    "node_id": "council_participant_0",
                    "outcome": "unavailable",
                    "error_code": "provider_unavailable",
                    "retry_safety": "safe",
                    "no_effect": True,
                    "tokens_used": 7,
                    "cost_estimated": 0.1,
                }
            ]
        },
    )
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "ready_for_confirmation", after["last_error"]
    assert after["members"][0]["selected_model_id"] == "beta"
    assert after["members"][0]["selected_harness_name"] == profile["name"]
    assert after["usage"]["external_calls"] == 4
    assert after["usage"]["tokens_used"] == 7
    assert after["usage"]["cost_estimated"] == 0.1
    with client.app.state.session_factory() as session:
        attempts = session.scalars(
            select(PlanningAttempt)
            .where(
                PlanningAttempt.member_id == job["members"][0]["id"],
            )
            .order_by(PlanningAttempt.attempt_index)
        ).all()
        assert [a.outcome for a in attempts] == ["unavailable", "succeeded"]
        assert [json.loads(a.candidate_json)["model_id"] for a in attempts] == ["alpha", "beta"]


@pytest.mark.parametrize("change", ["permission", "settings", "executable", "name"])
def test_retry_cannot_replace_pinned_harness_configuration(authenticated, tmp_path, change):
    client, headers = authenticated
    *_, profile, _, payload = setup_harness(client, headers, tmp_path)
    job = create_simulated(client, payload)
    planning_worker.dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={"harness_outcome": "confirmed_failure"},
    )
    job = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert job["state"] == "failed"
    patch = {"expected_version": profile["version"]}
    if change == "permission":
        patch["settings"] = {"permission_mode": "allow"}
    elif change == "settings":
        patch["settings"] = {"permission_mode": "no_tools", "new_setting": True}
    elif change == "executable":
        patch["executable_path"] = "C:/changed.exe"
    else:
        patch["name"] = "renamed-profile"
    response = client.patch(f"/api/harness_profiles/{profile['id']}", headers=headers, json=patch)
    assert response.status_code == 200, response.text
    factory = client.app.state.session_factory
    with factory() as session:
        before = session.get(PlanningMember, job["members"][0]["id"]).candidates_json
    retry = PlanningRetryRequest(
        expected_state_version=job["state_version"], reset_all_failed=True, refresh_credentials=True
    )
    if change == "name":
        with factory() as session:
            planning.retry_planning_job(session, job["id"], retry, simulated=True)
            session.commit()
            member = session.get(PlanningMember, job["members"][0]["id"])
            assert member.candidates_json == before
            assert json.loads(member.access_overrides_json)[profile["id"]] == {
                "version": 2,
                "native_fingerprint": json.loads(before)[0]["harness"]["native_fingerprint"],
            }
            current = planning.effective_candidate(member, json.loads(before)[0])
            assert current["harness"]["settings"] == {"permission_mode": "no_tools"}
            assert current["harness"]["name"] == profile["name"]
    else:
        with factory() as session, pytest.raises(AppError) as error:
            planning.retry_planning_job(session, job["id"], retry, simulated=True)
        assert error.value.code == (
            "council_harness_unverified"
            if change == "permission"
            else "planning_retry_harness_changed"
        )
        assert client.get(f"/api/planning_jobs/{job['id']}").json() == job


@pytest.mark.parametrize("body", ["", "not JSON"])
def test_empty_or_invalid_harness_output_is_never_replaced_with_valid_stub(
    authenticated, tmp_path, body
):
    client, headers = authenticated
    *_, payload = setup_harness(client, headers, tmp_path)
    job = create_simulated(client, payload)
    planning_worker.dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={"harness_body_text": body},
    )
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "failed"
    assert not after["revisions"]
    assert all(not d["accepted"] for d in after["drafts"])
    assert after["usage"]["external_calls"] == 4


def test_simulated_harness_checks_ownership_before_dispatch():
    called = []

    def lost():
        called.append(True)
        raise AppError("planning_lease_lost", "Lease lost", 409)

    with pytest.raises(AppError) as error:
        planning_worker._invoke_external(
            {"kind": "agent"},
            None,
            simulated=True,
            fake_scenario=None,
            secret_store=None,
            stop_event=threading.Event(),
            check_owned=lost,
        )
    assert error.value.code == "planning_lease_lost"
    assert called == [True]


@pytest.mark.parametrize("outcome", ["unknown", "unavailable"])
def test_ambiguous_harness_outcome_never_falls_back(authenticated, tmp_path, outcome):
    client, headers = authenticated
    *_, profile, _, payload = setup_harness(client, headers, tmp_path)
    group = agent_group(client, headers, profile)
    payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
    job = create_simulated(client, payload)
    planning_worker.dispatch_planning_job(
        client.app.state.session_factory,
        job["id"],
        simulated=True,
        fake_scenario={
            "responses": [
                {
                    "node_id": "council_participant_0",
                    "outcome": outcome,
                    "retry_safety": "unknown",
                    "no_effect": False,
                }
            ]
        },
    )
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "failed"
    assert after["members"][0]["selected_model_id"] == "alpha"
    with client.app.state.session_factory() as session:
        attempts = session.scalars(
            select(PlanningAttempt).where(
                PlanningAttempt.member_id == job["members"][0]["id"],
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].outcome == outcome


def test_simulated_harness_honors_stop_and_never_edits_workspace(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    *_, payload = setup_harness(client, headers, tmp_path)
    job = create_simulated(client, payload)
    started = threading.Event()
    stopped = threading.Event()
    abort = threading.Event()
    original = planning_worker.FakeAgentAdapter.run

    def capture(self, request):
        started.set()
        result = original(self, request)
        stopped.set()
        return result

    monkeypatch.setattr(planning_worker.FakeAgentAdapter, "run", capture)
    errors = []

    def dispatch():
        try:
            planning_worker.dispatch_planning_job(
                client.app.state.session_factory,
                job["id"],
                simulated=True,
                abort=abort,
                fake_scenario={
                    "responses": [
                        {
                            "node_id": "council_participant_0",
                            "delay_seconds": 30,
                            "files": {"should-not-exist.txt": "bad"},
                        }
                    ]
                },
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=dispatch)
    thread.start()
    try:
        assert started.wait(5)
        abort.set()
        assert stopped.wait(3)
    finally:
        abort.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    after = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert after["state"] == "failed"
    assert not after["revisions"]
    assert after["usage"]["external_calls"] == 1
    assert not list(tmp_path.rglob("should-not-exist.txt"))
