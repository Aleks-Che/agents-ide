"""Whitelisted model metadata and explicit protocol parameter mappings."""

import re
from typing import Any

from agents_ide.errors import AppError

_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def codex_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Adapt Claudexor's model/list validation, without fallback or clamping.

    Source: sources/claudexor/packages/harness-codex/src/effort-probe.ts (MIT).
    A malformed live ladder invalidates the probe, not just the bad level.
    """
    entries = item.get("supportedReasoningEfforts", [])
    if not isinstance(entries, list):
        raise OSError("Invalid Codex effort metadata")
    levels: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OSError("Invalid Codex effort entry")
        level = entry.get("reasoningEffort")
        if level is None:
            continue
        if not isinstance(level, str) or not _EFFORT.fullmatch(level) or level in levels:
            raise OSError("Invalid Codex effort level")
        levels.append(level)
    default = item.get("defaultReasoningEffort")
    if default not in (None, "") and (
        not isinstance(default, str) or not _EFFORT.fullmatch(default) or default not in levels
    ):
        raise OSError("Invalid Codex default effort")
    return {"source": "native_catalog", "reasoning_efforts": levels}


class CatalogModels(tuple[str, ...]):
    metadata: dict[str, dict[str, Any]]

    def __new__(cls, metadata: dict[str, dict[str, Any]]) -> "CatalogModels":
        value = super().__new__(cls, sorted(metadata))
        value.metadata = metadata
        return value


def parameters_for(
    harness: str, model: str, params: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    if not params:
        return {}
    supported = metadata.get(model, {}).get("reasoning_efforts", [])
    if set(params) != {"reasoning_effort"} or params["reasoning_effort"] not in supported:
        raise AppError(
            "configuration_invalid", "Параметры не подтверждены каталогом выбранной модели", 422
        )
    return {"effort" if harness == "codex" else "variant": params["reasoning_effort"]}
