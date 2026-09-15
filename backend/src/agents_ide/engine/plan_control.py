"""Server-owned fixed plan and evidence-backed status projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.errors import AppError
from agents_ide.persistence.models import ArtifactManifest, PlanItem, StepExecution


def normalize_plan(values: dict[str, Any]) -> dict[str, Any]:
    text = values.get("plan")
    if not isinstance(text, str) or not text.strip() or len(text) > 65536:
        raise AppError("plan_empty", "A nonempty original plan is required", 422)
    entries = values.get("plan_items")
    if entries is None:
        entries = [{"title": text, "acceptance_criteria": [text]}]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise AppError("plan_invalid", "Plan must contain 1–100 confirmed items", 422)
    result = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise AppError("plan_invalid", "Invalid plan item", 422)
        title, criteria = entry.get("title"), entry.get("acceptance_criteria", [])
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 65536
            or not isinstance(criteria, list)
            or len(criteria) > 100
            or any(not isinstance(c, str) or not c.strip() or len(c) > 65536 for c in criteria)
        ):
            raise AppError("plan_invalid", "Invalid title/acceptance criteria", 422)
        if entry.get("id", f"P{i}") != f"P{i}":
            raise AppError("plan_invalid", "IDs are assigned by the server in confirmed order", 422)
        result.append({"id": f"P{i}", "title": title, "acceptance_criteria": criteria})
    return {"version": 1, "original_text": text, "items": result}


@dataclass
class PlanSummary:
    plan_item_ids: tuple[str, ...]
    completed: tuple[str, ...]
    remaining: tuple[str, ...]
    failed: tuple[str, ...]
    current: str | None
    scope: str

    def as_work(self) -> dict[str, Any]:
        return {
            "current_plan_item_id": self.current,
            "plan_item_ids": list(self.plan_item_ids),
            "completed_items": list(self.completed),
            "remaining_items": list(self.remaining),
            "failed_items": list(self.failed),
            "scope": self.scope,
        }


def load_plan_items(session: Session, run_id: str) -> list[PlanItem]:
    session.flush()
    return list(
        session.scalars(
            select(PlanItem).where(PlanItem.run_id == run_id).order_by(PlanItem.order_index)
        )
    )


def plan_summary(session: Session, run_id: str, *, scope: str | None = None) -> PlanSummary:
    items = load_plan_items(session, run_id)
    if scope is not None:
        items = [i for i in items if i.scope == scope]
    active = next((i.item_id for i in items if i.status == "in_progress"), None)
    current = active or next((i.item_id for i in items if i.status in {"pending", "failed"}), None)
    return PlanSummary(
        tuple(i.item_id for i in items),
        tuple(i.item_id for i in items if i.status == "done"),
        tuple(i.item_id for i in items if i.status != "done"),
        tuple(i.item_id for i in items if i.status == "failed"),
        current,
        scope or "main",
    )


def upsert_plan_items(
    session: Session, run_id: str, items: list[dict[str, Any]], *, scope: str = "main"
) -> list[PlanItem]:
    if not items or len({x.get("id") for x in items}) != len(items):
        raise AppError("plan_invalid", "Nonempty unique fixed IDs are required", 422)
    existing = load_plan_items(session, run_id)
    if existing:
        old = [
            {
                "id": x.item_id,
                "title": x.title,
                "acceptance_criteria": json.loads(x.acceptance_criteria_json),
            }
            for x in existing
        ]
        if old != items or any(x.scope != scope for x in existing):
            raise AppError("plan_immutable", "Plan IDs, order and criteria are immutable", 409)
        return existing
    result = []
    for order, item in enumerate(items, 1):
        row = PlanItem(
            id=new_id(),
            run_id=run_id,
            item_id=item["id"],
            scope=scope,
            order_index=order,
            title=item["title"],
            acceptance_criteria_json=to_json(item["acceptance_criteria"]),
            status="pending",
            evidence_ids_json="[]",
            commit_shas_json="[]",
            related_execution_ids_json="[]",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(row)
        result.append(row)
    session.flush()
    return result


def _item(session: Session, run_id: str, item_id: str) -> PlanItem:
    row = session.scalar(
        select(PlanItem).where(PlanItem.run_id == run_id, PlanItem.item_id == item_id)
    )
    if row is None:
        raise AppError("plan_item_unknown", "Unknown fixed plan ID", 409)
    return row


def checked_evidence(session: Session, run_id: str, evidence_ids: Any) -> list[str]:
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or len(evidence_ids) > 100
        or any(not isinstance(x, str) for x in evidence_ids)
    ):
        raise AppError("plan_evidence_missing", "Immutable evidence is required", 409)
    for evidence_id in evidence_ids:
        row = session.get(ArtifactManifest, evidence_id)
        if row is None or row.run_id != run_id or not row.body_json:
            raise AppError("plan_evidence_invalid", "Evidence does not belong to this Run", 409)
    return list(dict.fromkeys(evidence_ids))


def attach_evidence(session: Session, run_id: str, item_id: str, evidence_ids: list[str]) -> None:
    checked = checked_evidence(session, run_id, evidence_ids)
    item = _item(session, run_id, item_id)
    item.evidence_ids_json = to_json(
        list(dict.fromkeys([*json.loads(item.evidence_ids_json), *checked]))
    )
    item.updated_at = utc_now()


def attach_execution(session: Session, run_id: str, item_id: str, execution_id: str) -> None:
    execution = session.get(StepExecution, execution_id)
    if execution is None or execution.run_id != run_id:
        raise AppError("plan_execution_invalid", "Execution does not belong to this Run", 409)
    item = _item(session, run_id, item_id)
    item.related_execution_ids_json = to_json(
        list(dict.fromkeys([*json.loads(item.related_execution_ids_json), execution_id]))
    )
    item.updated_at = utc_now()


def attach_commit(session: Session, run_id: str, item_id: str, sha: str) -> None:
    item = _item(session, run_id, item_id)
    item.commit_shas_json = to_json(list(dict.fromkeys([*json.loads(item.commit_shas_json), sha])))
    item.updated_at = utc_now()


def select_next_item(session: Session, run_id: str, scope: str | None = None) -> PlanSummary:
    summary = plan_summary(session, run_id, scope=scope)
    if summary.current:
        row = _item(session, run_id, summary.current)
        row.status, row.updated_at = "in_progress", utc_now()
    return plan_summary(session, run_id, scope=scope)


def record_verified(
    session: Session,
    run_id: str,
    item_id: str,
    *,
    verdict: str,
    evidence_ids: list[str] | None = None,
    commit_sha: str | None = None,
    findings: list[str] | None = None,
) -> PlanItem:
    item = _item(session, run_id, item_id)
    if item.status not in {"in_progress", "done"}:
        raise AppError("plan_status_conflict", "Verification requires the selected item", 409)
    if verdict not in {"passed", "failed", "inconclusive"}:
        raise AppError("plan_verdict_invalid", "Invalid verdict", 422)
    checked_evidence(session, run_id, evidence_ids)
    if item.status == "done" and verdict != "passed":
        raise AppError("plan_status_conflict", "Only a final check can reopen a done item", 409)
    attach_evidence(session, run_id, item_id, evidence_ids or [])
    if commit_sha:
        attach_commit(session, run_id, item_id, commit_sha)
    if verdict != "inconclusive":
        item.status = "done" if verdict == "passed" else "failed"
    item.updated_at = utc_now()
    return item


def validate_item_results(results: Any, expected_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(results, list) or len(results) != len(expected_ids):
        raise AppError(
            "plan_final_check_invalid", "Every requested fixed ID must appear exactly once", 422
        )
    seen = []
    for entry in results:
        if not isinstance(entry, dict) or entry.get("verdict") not in {
            "passed",
            "failed",
            "inconclusive",
        }:
            raise AppError("plan_final_check_invalid", "Invalid item result", 422)
        item_id = entry.get("plan_item_id")
        if item_id not in expected_ids or item_id in seen:
            raise AppError("plan_item_unknown", "Unknown or duplicate item ID", 422)
        if not isinstance(entry.get("findings", []), list):
            raise AppError("plan_final_check_invalid", "Findings must be a list", 422)
        seen.append(item_id)
    return results


def record_final_check(
    session: Session, run_id: str, results: list[dict[str, Any]], *, scope: str | None = None
) -> tuple[PlanSummary, list[str]]:
    items = {
        i.item_id: i for i in load_plan_items(session, run_id) if scope is None or i.scope == scope
    }
    validate_item_results(results, list(items))
    for entry in results:
        item = items[entry["plan_item_id"]]
        ids = checked_evidence(session, run_id, entry.get("evidence_ids"))
        if (
            entry["verdict"] == "failed"
            and item.status == "done"
            and (
                not entry.get("findings")
                or not (set(ids) - set(json.loads(item.evidence_ids_json)))
            )
        ):
            raise AppError(
                "plan_done_reopen_without_evidence",
                "A new concrete defect needs new evidence",
                409,
            )
    defects = []
    for entry in results:
        item = items[entry["plan_item_id"]]
        attach_evidence(session, run_id, item.item_id, entry["evidence_ids"])
        if entry["verdict"] == "failed":
            item.status = "failed"
            defects.append(item.item_id)
        # Final passed cannot skip implementation/verification/commit of pending items.
        item.updated_at = utc_now()
    return plan_summary(session, run_id, scope=scope), defects
