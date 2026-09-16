import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from council_support import create, dispatch, document, setup
from sqlalchemy import select

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.adapters.fake import FakeLLMAdapter
from agents_ide.domain.common import utc_now
from agents_ide.engine.planning_worker import claim_planning_job, dispatch_planning_job
from agents_ide.persistence.models import PlanningAttempt, PlanningJob


def test_participants_independent_and_merger_gets_actual_drafts(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    prompts = []
    original = FakeLLMAdapter.run

    def capture(self, request):
        prompts.append(request.prompt)
        # A different writer can commit while the model is running.
        with client.app.state.session_factory() as session:
            row = session.get(PlanningJob, job["id"])
            assert json.loads(row.usage_json)["external_calls"] == len(prompts)
            row.updated_at = utc_now()
            session.commit()
        return original(self, request)

    monkeypatch.setattr(FakeLLMAdapter, "run", capture)
    ready = dispatch(client, job)
    assert ready["state"] == "ready_for_confirmation"
    assert ready["n_participants_actual"] == 2 and not ready["degraded"]
    assert prompts[0] == prompts[1] and "Pinned context marker" in prompts[0]
    assert "Draft marker" not in prompts[0]
    assert "Draft marker 0" in prompts[2] and "Draft marker 1" in prompts[2]
    assert [d["slot_index"] for d in ready["drafts"]] == [0, 1]
    assert len({d["content_hash"] for d in ready["drafts"]}) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "## Open Questions\n- (none)",
        '{"body_text":"x","steps":[{"title":"x","acceptance_criteria":["ok"]}]}',
        '{"body_text":"x","steps":[],"questions":[]}',
        '{"body_text":"x","steps":[{"title":"x","acceptance_criteria":[]}],"questions":[]}',
        "я" * 66000,
    ],
    ids=["empty", "markdown", "missing_questions", "no_steps", "no_criteria", "oversize"],
)
def test_invalid_or_truncated_success_never_becomes_plan(authenticated, tmp_path, bad):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    result = dispatch(
        client,
        job,
        responses=[
            {"node_id": f"council_participant_{i}", "attempt_index": attempt, "raw_text": bad}
            for i in range(2)
            for attempt in (1, 2)
        ],
    )
    assert result["state"] == "failed" and not result["revisions"]
    assert not any(d["accepted"] for d in result["drafts"])
    assert result["usage"]["external_calls"] <= 4


def test_budget_includes_merger_and_deadline(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path, budget={"max_external_calls": 2})
    job = create(client, headers, payload)
    failed = dispatch(client, job)
    assert failed["last_error"]["code"] == "planning_budget_exhausted"
    assert failed["usage"]["external_calls"] == 2 and not failed["revisions"]
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    with client.app.state.session_factory() as session:
        session.get(PlanningJob, job["id"]).started_at = utc_now() - 1000
        session.commit()
    assert dispatch(client, job)["last_error"]["code"] == "planning_deadline_exceeded"


def test_adapter_format_error_gets_one_explicit_repair(authenticated, tmp_path, monkeypatch):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    prompts = []
    original = FakeLLMAdapter.run

    def capture(self, request):
        prompts.append(request.prompt)
        return original(self, request)

    monkeypatch.setattr(FakeLLMAdapter, "run", capture)
    result = dispatch(
        client,
        job,
        responses=[
            {
                "node_id": "council_participant_0",
                "outcome": "invalid_format",
                "error_code": "output_schema_invalid",
            },
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document()),
            },
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert "Your previous answer was invalid" in prompts[1]
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 4


def test_group_snapshot_fallback_and_independence(authenticated, tmp_path):
    client, headers = authenticated
    *_, connection, payload = setup(client, headers, tmp_path)
    group = client.post(
        "/api/model_groups/llm",
        headers=headers,
        json={
            "name": "shared",
            "members": [
                {"provider_connection_id": connection["id"], "model_id": "a"},
                {"provider_connection_id": connection["id"], "model_id": "b"},
                {"provider_connection_id": connection["id"], "model_id": "c"},
            ],
        },
    ).json()
    for member in payload["participants"][:2]:
        member["selection"] = {"kind": "group", "group_id": group["id"]}
    job = create(client, headers, payload)
    # Changing the group after enqueue must not change its pinned candidates.
    assert (
        client.put(
            f"/api/model_groups/{group['id']}/llm/members",
            headers=headers,
            json={
                "expected_revision": group["revision"],
                "members": [{"provider_connection_id": connection["id"], "model_id": "new"}],
            },
        ).status_code
        == 200
    )
    result = dispatch(
        client,
        job,
        responses=[
            {
                "node_id": "council_participant_0",
                "attempt_index": 1,
                "outcome": "unavailable",
                "error_code": "quota",
                "retry_safety": "safe",
                "no_effect": True,
            },
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document()),
            },
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert [m["selected_model_id"] for m in result["members"][:2]] == ["b", "a"]
    assert result["usage"]["external_calls"] == 4


def test_lease_exclusive_restart_does_not_repeat_unknown(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    factory = client.app.state.session_factory
    claim = claim_planning_job(factory, "worker-one", job["id"])
    assert claim and claim_planning_job(factory, "worker-two", job["id"]) is None
    from agents_ide.engine.planning_worker import _prepare

    assert _prepare(factory, claim)
    with factory() as session:
        session.get(PlanningJob, job["id"]).lease_expires_at = 0
        session.commit()
    assert claim_planning_job(factory, "worker-two", job["id"]) is None
    failed = dispatch(client, job)
    assert failed["last_error"]["code"] == "unknown_external_result"
    assert failed["usage"]["external_calls"] == 1
    with factory() as session:
        assert (
            session.scalar(
                select(PlanningAttempt).where(PlanningAttempt.job_id == job["id"])
            ).outcome
            == "unknown"
        )


def test_restart_keeps_committed_draft_and_shared_budget(authenticated, tmp_path):
    from agents_ide.engine.planning_worker import _finish, _prepare

    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    factory = client.app.state.session_factory
    claim = claim_planning_job(factory, "before-restart", job["id"])
    attempt_id, _, _ = _prepare(factory, claim)
    _finish(
        factory,
        claim,
        attempt_id,
        LLMResult(ExternalOutcome.SUCCEEDED, json.dumps(document("Saved draft")), None, None),
    )
    with factory() as session:
        session.get(PlanningJob, job["id"]).lease_expires_at = 0
        session.commit()
    result = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert result["state"] == "ready_for_confirmation"
    assert result["usage"]["external_calls"] == 3
    assert "Saved draft" in result["drafts"][0]["body_text"]


def test_unknown_and_invalid_format_do_not_advance_group(authenticated, tmp_path):
    client, headers = authenticated
    for outcome in ("unknown", "invalid_format", "confirmed_failure"):
        *_, connection, payload = setup(client, headers, tmp_path)
        group = client.post(
            "/api/model_groups/llm",
            headers=headers,
            json={
                "name": outcome,
                "members": [
                    {"provider_connection_id": connection["id"], "model_id": model}
                    for model in ("bad", "must-not-call")
                ],
            },
        ).json()
        payload["participants"][0]["selection"] = {"kind": "group", "group_id": group["id"]}
        result = dispatch(
            client,
            create(client, headers, payload),
            responses=[
                {
                    "node_id": "council_participant_0",
                    "attempt_index": i,
                    "outcome": outcome,
                    "error_code": outcome,
                }
                for i in (1, 2)
            ],
        )
        assert result["state"] == "failed"
        assert result["members"][0]["selected_model_id"] == "bad"
        assert not any(e["payload"].get("model_id") == "must-not-call" for e in result["events"])


def test_cancel_interrupts_active_call_and_preserves_cancelled(
    authenticated, tmp_path, monkeypatch
):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    entered = threading.Event()

    def blocked(self, request):
        entered.set()
        assert request.stop_event.wait(5)
        return LLMResult(ExternalOutcome.UNKNOWN, "partial response", None, None)

    monkeypatch.setattr(FakeLLMAdapter, "run", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            dispatch_planning_job, client.app.state.session_factory, job["id"], simulated=True
        )
        assert entered.wait(3)
        current = client.get(f"/api/planning_jobs/{job['id']}").json()
        response = client.post(
            f"/api/planning_jobs/{job['id']}/cancel",
            headers=headers,
            json={"expected_state_version": current["state_version"]},
        )
        assert response.status_code == 200
        assert future.result(timeout=6).final_state.value == "cancelled"
    result = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert result["usage"]["external_calls"] == 1


def test_two_of_three_merge_but_single_draft_never_passes_as_council(authenticated, tmp_path):
    client, headers = authenticated
    for count in (3, 2):
        *_, payload = setup(client, headers, tmp_path, count=count)
        result = dispatch(
            client,
            create(client, headers, payload),
            responses=[
                {
                    "node_id": "council_participant_0",
                    "outcome": "confirmed_failure",
                    "error_code": "failed",
                },
                *[
                    {"node_id": f"council_participant_{i}", "raw_text": json.dumps(document())}
                    for i in range(1, count)
                ],
                {"node_id": "council_merger", "raw_text": json.dumps(document())},
            ],
        )
        assert result["n_participants_actual"] == count - 1
        assert result["degraded"]
        assert result["state"] == ("ready_for_confirmation" if count == 3 else "failed")


def test_retry_failed_job_preserves_budget_and_accepted_drafts(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {
                "node_id": "council_participant_0",
                "outcome": "confirmed_failure",
                "error_code": "quota",
            },
            {
                "node_id": "council_participant_1",
                "outcome": "confirmed_failure",
                "error_code": "quota",
            },
        ],
    )
    assert failed["state"] == "failed"
    assert failed["last_error"]["code"] == "council_quorum_missing"
    usage_before = failed["usage"]["external_calls"]
    # After access restoration: retry resets both failed participants.
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={"expected_state_version": failed["state_version"], "reset_all_failed": True},
    )
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["state"] == "drafting"
    assert len(accepted["reset_member_ids"]) == 2
    # The merger is untouched (never tried), so it is preserved.
    assert len(accepted["preserved_member_ids"]) == 1
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert refreshed["state"] == "drafting"
    assert refreshed["usage"]["external_calls"] == usage_before
    statuses = sorted(m["status"] for m in refreshed["members"] if m["role"] == "participant")
    assert statuses == ["pending", "pending"]
    # Redispatch completes the council with the same shared budget preserved.
    finished = dispatch(
        client,
        refreshed,
        responses=[
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Recovered 0")),
            },
            {
                "node_id": "council_participant_1",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Recovered 1")),
            },
            {"node_id": "council_merger", "raw_text": json.dumps(document("Merged after retry"))},
        ],
    )
    assert finished["state"] == "ready_for_confirmation"
    assert finished["usage"]["external_calls"] == usage_before + 3
    assert "Recovered 0" in finished["drafts"][0]["body_text"]
    assert "Recovered 1" in finished["drafts"][1]["body_text"]


def test_retry_subset_preserves_unselected_failures_and_requires_quorum(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "outcome": "confirmed_failure"},
        ],
    )
    assert failed["state"] == "failed"
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={"expected_state_version": failed["state_version"], "reset_member_indices": [0]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reset_member_ids"] == [failed["members"][0]["id"]]
    finished = dispatch(
        client,
        failed,
        responses=[
            {
                "node_id": "council_participant_0",
                "attempt_index": 2,
                "raw_text": json.dumps(document("Recovered one")),
            }
        ],
    )
    assert finished["state"] == "failed"
    assert finished["last_error"]["code"] == "council_quorum_missing"
    assert finished["usage"]["external_calls"] == failed["usage"]["external_calls"] + 1
    assert finished["members"][1] == failed["members"][1]
    assert finished["drafts"][1] == failed["drafts"][1]


def test_retry_rejects_attempt_to_reset_succeeded_member(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path, count=3)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "raw_text": json.dumps(document())},
            {"node_id": "council_participant_2", "raw_text": json.dumps(document())},
            {"node_id": "council_merger", "raw_text": json.dumps(document())},
        ],
    )
    assert failed["state"] == "ready_for_confirmation"
    # ready_for_confirmation is not eligible for retry even with reset_all_failed.
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={"expected_state_version": failed["state_version"], "reset_all_failed": True},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_state_invalid"


def test_retry_state_version_mismatch_returns_conflict(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "outcome": "confirmed_failure"},
        ],
    )
    assert failed["state"] == "failed"
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={"expected_state_version": failed["state_version"] + 99, "reset_all_failed": True},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "planning_state_version_invalid"


def test_retry_records_event_and_clears_finished_at(authenticated, tmp_path):
    client, headers = authenticated
    *_, payload = setup(client, headers, tmp_path)
    job = create(client, headers, payload)
    failed = dispatch(
        client,
        job,
        responses=[
            {"node_id": "council_participant_0", "outcome": "confirmed_failure"},
            {"node_id": "council_participant_1", "outcome": "confirmed_failure"},
        ],
    )
    assert failed["state"] == "failed"
    assert failed["finished_at"] is not None
    response = client.post(
        f"/api/planning_jobs/{job['id']}/retry",
        headers=headers,
        json={"expected_state_version": failed["state_version"], "reset_all_failed": True},
    )
    assert response.status_code == 200, response.text
    refreshed = client.get(f"/api/planning_jobs/{job['id']}").json()
    assert refreshed["finished_at"] is None
    assert any(e["type"] == "planning.retried" for e in refreshed["events"])
    retry_event = next(e for e in refreshed["events"] if e["type"] == "planning.retried")
    assert len(retry_event["payload"]["reset_members"]) == 2
    assert retry_event["payload"]["reason"] is None
