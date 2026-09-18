"""Built-in preset catalog and idempotent installer.

The catalog ships as versioned JSON inside the package. Application updates
never overwrite user-owned copies. A stable system template ID identifies the
source; a changed executable payload updates its validated definition.

Real install paths happen during application start (``api/app.py`` lifespan)
and the API catalog exposes the resulting template for ordinary bindings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash, utc_now
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    PipelineTemplate,
    PipelineVersion,
    Project,
)

BUILTIN_ORIGIN = "local"
PRESETS_PACKAGE = "agents_ide.presets"


@dataclass(frozen=True)
class PresetDefinition:
    name: str
    body: dict[str, Any]
    source_path: str

    @property
    def preset_id(self) -> str:
        return str(self.body.get("preset_id") or self.name)

    @property
    def description(self) -> str:
        return str(self.body.get("description") or "")

    @property
    def graph(self) -> dict[str, Any]:
        return dict(self.body.get("graph") or {})

    @property
    def inputs(self) -> list[dict[str, Any]]:
        return list(self.body.get("inputs") or [])

    @property
    def default_settings(self) -> dict[str, Any]:
        return dict(self.body.get("default_settings") or {})

    @property
    def role_examples(self) -> dict[str, str]:
        return dict(self.body.get("role_examples") or {})

    @property
    def prompts(self) -> dict[str, str]:
        return dict(self.body.get("prompts") or {})


def list_builtin_presets() -> list[PresetDefinition]:
    """Return every preset bundled with the application."""

    package = resources.files(PRESETS_PACKAGE)
    presets: list[PresetDefinition] = []
    entries = [entry for entry in package.iterdir() if entry.name.endswith(".json")]
    for entry in sorted(entries, key=lambda item: item.name):
        presets.append(_load_preset_from_path(entry))
    return presets


def get_builtin_preset(preset_id: str) -> PresetDefinition:
    for preset in list_builtin_presets():
        if preset.preset_id == preset_id:
            return preset
    raise AppError(
        "preset_unknown",
        f"Встроенный пресет {preset_id!r} не найден",
        404,
    )


def _load_preset_from_path(path: Any) -> PresetDefinition:
    raw = path.read_text(encoding="utf-8")
    body = json.loads(raw)
    name = str(body.get("name") or Path(str(path)).stem)
    return PresetDefinition(name=name, body=body, source_path=str(path))


def ensure_preset_installed(
    session: Session,
    project: Project | None,
    preset_id: str,
    *,
    template_name: str | None = None,
) -> PipelineTemplate:
    """Install by stable catalog identity using the normal validation/hash contract."""
    from agents_ide.domain.graph_schema import required_features_for
    from agents_ide.domain.graph_validation import definition_hash, validate_graph
    from agents_ide.domain.schemas import PipelineVersionCreate, SettingsOverrides
    from agents_ide.services.templates import create_version
    from agents_ide.services.transactions import begin_write

    begin_write(session)
    definition = get_builtin_preset(preset_id)
    identifier = content_hash({"builtin_preset": preset_id})[:32]
    template = session.get(PipelineTemplate, identifier)
    payload = PipelineVersionCreate(
        graph=definition.graph,
        inputs=definition.body.get("default_inputs", {}),
        settings=SettingsOverrides.model_validate(definition.default_settings),
        required_features=required_features_for(definition.graph),
    )
    report = validate_graph(payload.graph, inputs=payload.inputs)
    if not report.ok:
        raise AppError(
            "preset_invalid",
            "Bundled preset failed validation",
            500,
            {"errors": [x.to_dict() for x in report.errors]},
        )
    if template is None:
        template = PipelineTemplate(
            id=identifier,
            name=template_name or definition.name,
            description=definition.description,
            kind="system",
            schema_version="1.0.0",
            draft_json=payload.model_dump_json(),
            version=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(template)
        session.flush()
    if template.kind != "system":
        raise AppError("preset_conflict", "Catalog identity is occupied by a user template", 409)
    expected = definition_hash(payload.model_dump(mode="json", exclude_none=True))
    version = session.scalar(
        select(PipelineVersion).where(
            PipelineVersion.template_id == identifier, PipelineVersion.execution_hash == expected
        )
    )
    if version is None:
        create_version(session, identifier, payload, _system=True)
        template.draft_json = payload.model_dump_json()
        template.version += 1
        template.updated_at = utc_now()
    return template


def install_all(session: Session) -> None:
    for definition in list_builtin_presets():
        ensure_preset_installed(session, None, definition.preset_id)


def binding_readiness(
    session: Session,
    preset_id: str,
    binding_model_selections: dict[str, Any],
    available_groups: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    from pydantic import ValidationError

    from agents_ide.domain.schemas import MODEL_SELECTION
    from agents_ide.persistence.models import HarnessProfile, ProviderConnection
    from agents_ide.services.groups import load_group_snapshot

    definition = get_builtin_preset(preset_id)
    roles: dict[str, str] = definition.graph.get("roles", {})
    report: dict[str, Any] = {}
    for role, expected_kind in roles.items():
        entry: dict[str, Any] = {"ready": False, "expected_kind": expected_kind}
        try:
            choice = MODEL_SELECTION.validate_python(binding_model_selections.get(role)).model_dump(
                exclude_none=True
            )
            if choice["kind"] == "group":
                loaded = load_group_snapshot(session, choice["group_id"])
                entry["ready"] = bool(
                    loaded
                    and loaded[0].kind == expected_kind
                    and any(
                        m.enabled
                        and (
                            (
                                target := (
                                    session.get(HarnessProfile, m.harness_profile_id)
                                    if expected_kind == "agent" and m.harness_profile_id
                                    else session.get(ProviderConnection, m.provider_connection_id)
                                    if expected_kind == "llm" and m.provider_connection_id
                                    else None
                                )
                            )
                            is not None
                        )
                        and target.archived_at is None
                        for m in loaded[1]
                    )
                )
            else:
                key = "harness_profile_id" if expected_kind == "agent" else "provider_connection_id"
                target = (
                    (
                        session.get(HarnessProfile, choice[key])
                        if expected_kind == "agent"
                        else session.get(ProviderConnection, choice[key])
                    )
                    if choice.get(key)
                    else None
                )
                entry["ready"] = bool(target and target.archived_at is None)
            entry["kind"] = choice["kind"]
        except (AppError, ValidationError, KeyError):
            pass
        report[role] = entry
    return {
        "preset_id": preset_id,
        "ready": all(x["ready"] for x in report.values()),
        "roles": report,
    }
