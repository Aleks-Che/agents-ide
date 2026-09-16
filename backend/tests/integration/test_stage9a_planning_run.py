import json

from council_support import ANSWERS, QUESTIONS, confirm, create, dispatch, setup
from sqlalchemy import select

from agents_ide.persistence.models import PlanItem, Run


def binding(client, headers, project_id):
    template = client.post("/api/templates", headers=headers, json={"name": project_id}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions",
        headers=headers,
        json={
            "graph": {
                "nodes": [
                    {"id": "start", "type": "Start"},
                    {"id": "plan", "type": "PlanControl", "config": {"operation": "select_next"}},
                    {"id": "end", "type": "End"},
                ],
                "edges": [
                    {"id": "a", "from": "start", "to": "plan"},
                    {"id": "b", "from": "plan", "to": "end"},
                ],
            }
        },
    )
    assert version.status_code == 201, version.text
    response = client.post(
        f"/api/versions/{version.json()['id']}/bindings",
        headers=headers,
        json={"project_id": project_id, "name": "Council input"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_confirmed_plan_preflight_run_and_plan_items_share_fixed_source(
    authenticated, tmp_path, settings
):
    client, headers = authenticated
    project, chat, _, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload), questions=QUESTIONS)
    response = client.post(
        f"/api/planning_jobs/{job['id']}/answers",
        headers=headers,
        json={"expected_revision": 1, "answers": ANSWERS},
    )
    assert response.status_code == 200, response.text
    job = client.get(f"/api/planning_jobs/{job['id']}").json()
    source = confirm(client, headers, job)
    bound = binding(client, headers, project["id"])
    check = client.post(
        f"/api/bindings/{bound['id']}/preflight",
        headers=headers,
        json={"execution_mode": "real", "planning_source": source},
    )
    assert check.status_code == 200 and check.json()["ok"], check.text
    start = {
        "project_id": project["id"],
        "chat_id": chat["id"],
        "binding_id": bound["id"],
        "execution_mode": "real",
        "planning_source": source,
        "idempotency_key": "start-council",
        "trusted_execution_hash": check.json()["execution_hash"],
    }
    response = client.post("/api/runs", headers=headers, json=start)
    assert response.status_code == 201, response.text
    run = response.json()
    assert client.post("/api/runs", headers=headers, json=start).json()["id"] == run["id"]
    with client.app.state.session_factory() as session:
        snapshot = json.loads(session.get(Run, run["id"]).snapshot_json)
        assert snapshot["planning_source"] == source
        assert snapshot["input"]["values"]["planning_revision_hash"] == source["confirmation_hash"]
        assert "Offline only" in snapshot["plan"]["original_text"]
        items = list(session.scalars(select(PlanItem).where(PlanItem.run_id == run["id"])))
        assert [item.item_id for item in items] == ["P1"]
        assert json.loads(items[0].acceptance_criteria_json) == ["Tests pass"]
    from test_stage9_selection_summary import _execute

    result = _execute(client, run, settings)
    # Selecting a plan item is not proof that its implementation has been verified.
    assert result.final_state.value == "waiting_input"
    with client.app.state.session_factory() as session:
        item = session.scalar(select(PlanItem).where(PlanItem.run_id == run["id"]))
        assert item.item_id == "P1" and item.status == "in_progress"


def test_source_rejects_wrong_project_hash_unconfirmed_and_overrides(authenticated, tmp_path):
    client, headers = authenticated
    project, _, _, payload = setup(client, headers, tmp_path)
    job = dispatch(client, create(client, headers, payload))
    bound = binding(client, headers, project["id"])
    revision = job["revisions"][-1]
    source = {
        "job_id": job["id"],
        "revision_number": 1,
        "confirmation_hash": revision["confirmation_hash"],
    }
    url = f"/api/bindings/{bound['id']}/preflight"

    def preflight(value, inputs=None):
        return client.post(
            url,
            headers=headers,
            json={"planning_source": value, "execution_mode": "simulated", "inputs": inputs or {}},
        )

    assert preflight(source).status_code == 409
    source = confirm(client, headers, job)
    assert preflight({**source, "confirmation_hash": "0" * 64}).status_code == 409
    assert preflight(source, {"plan": "replace confirmed text"}).status_code == 409
    assert preflight({**source, "revision_number": 0}).status_code == 422
    other, *_ = setup(client, headers, tmp_path)
    other_binding = binding(client, headers, other["id"])
    assert (
        client.post(
            f"/api/bindings/{other_binding['id']}/preflight",
            headers=headers,
            json={"planning_source": source},
        ).status_code
        == 409
    )
