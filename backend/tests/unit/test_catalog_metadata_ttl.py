import json

from agents_ide.persistence.models import HarnessProfile
from agents_ide.services.harness import current_model_metadata


def test_options_survive_ttl_but_not_native_configuration_changes(monkeypatch):
    monkeypatch.setattr("agents_ide.services.harness.fingerprint", lambda *args: "same")
    options = {"provider/model": {"reasoning_efforts": ["high", "max"]}}
    profile = HarnessProfile(
        harness_kind="opencode",
        executable_path="opencode",
        catalog_fingerprint="same",
        catalog_fetched_at=1,
        catalog_ttl_seconds=900,
        catalog_metadata_json=json.dumps(options),
    )
    assert current_model_metadata(profile) == options
    profile.catalog_fingerprint = "old"
    assert current_model_metadata(profile) == {}
    profile.catalog_fingerprint = "same"
    profile.catalog_fetched_at = None
    assert current_model_metadata(profile) == {}
