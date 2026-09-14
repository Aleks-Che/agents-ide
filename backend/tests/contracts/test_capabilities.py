import json
from pathlib import Path

from agents_ide.domain.contracts import AdapterCapabilities, RunState

FIXTURES = Path(__file__).resolve().parents[3] / "docs/integrations/fixtures"


def test_recorded_harness_evidence_never_implies_write_permission():
    report = json.loads((FIXTURES / "2026-09-14-windows.json").read_text())
    for harness in ("codex", "opencode"):
        capabilities = AdapterCapabilities.model_validate(report[harness]["capabilities"])
        assert capabilities.transport_ready
        assert not capabilities.permits_autonomous_write(supervisor_verified=True)
        assert report[harness]["version"]
        assert report[harness]["observations"]


def test_unknown_transport_and_stopped_run_are_not_success():
    assert not AdapterCapabilities().transport_ready
    assert not RunState.STOPPED.terminal
    assert not RunState.WAITING_INPUT.terminal
    assert RunState.COMPLETED.terminal


def test_control_probes_preserve_recovery_boundary():
    report = json.loads((FIXTURES / "2026-09-14-controls.json").read_text())
    for harness in ("codex", "opencode"):
        assert report[harness]["permission"] == "supported"
        assert report[harness]["completed_session_recovery"] == "supported"
        assert report[harness]["active_turn_recovery"] == "unverified"
        assert report[harness]["write_isolation"] == "unverified"
    assert report["codex"]["file_written_after_decline"] is False
    assert report["codex"]["interrupted_status"] == "interrupted"
    assert report["opencode"]["second_response_ok"] is True
