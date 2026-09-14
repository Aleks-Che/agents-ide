"""Server-side candidate selection for agent and LLM groups.

The selection algorithm is the engine's single source of truth: it never
delegates to the adapter. Real Codex and OpenCode adapters only see the
selected candidate's identity and may refuse, in which case the adapter
returns ``unavailable`` and the runner advances to the next member.

Selection rules (matching PROJECT_DESCRIPTION §7.5):

* Disabled members are skipped with reason ``disabled``.
* Archived profiles or provider connections are skipped with reason
  ``archived``.
* Missing profiles or provider connections are skipped with reason
  ``missing``.
* Members whose params reference an unavailable SecretStore entry are
  skipped with reason ``secret_unavailable``.
* Resolution is forward-only. We never wrap back to an earlier member; the
  rule "fallback only forward" comes from the architecture contract.
* A member whose :class:`StepAttempt` previously failed with
  ``CONFIRMED_FAILURE_OUTCOMES`` is permanently excluded for the current
  visit (resume of an interrupted visit must keep the same candidate per
  §7.5).
* ``model_group.exhausted`` is reached when no remaining candidate is
  eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateSkip:
    member_index: int
    member_id: str | None
    reason: str


@dataclass(frozen=True)
class CandidateSelection:
    member_index: int
    member_id: str | None
    candidate: dict[str, Any]


def select_candidate(
    candidates: list[dict[str, Any]],
    *,
    excluded_member_indexes: frozenset[int] | None = None,
) -> tuple[CandidateSelection | None, list[CandidateSkip]]:
    """Walk the ordered candidate list and return the first eligible entry.

    ``excluded_member_indexes`` records members that previously failed
    irreversibly during this visit. They are skipped without changing the
    reason, mirroring §7.5's "no cycling".
    """

    excluded = excluded_member_indexes or frozenset()
    skips: list[CandidateSkip] = []
    for entry in candidates:
        if entry["member_index"] in excluded:
            skips.append(
                CandidateSkip(
                    member_index=entry["member_index"],
                    member_id=entry.get("id"),
                    reason="previous_confirmed_failure",
                )
            )
            continue
        if not entry.get("enabled", True):
            skips.append(
                CandidateSkip(
                    member_index=entry["member_index"],
                    member_id=entry.get("id"),
                    reason="disabled",
                )
            )
            continue
        reason = entry.get("unavailable_reason")
        if reason:
            skips.append(
                CandidateSkip(
                    member_index=entry["member_index"],
                    member_id=entry.get("id"),
                    reason=reason,
                )
            )
            continue
        return (
            CandidateSelection(
                member_index=entry["member_index"],
                member_id=entry.get("id"),
                candidate=entry,
            ),
            skips,
        )
    return None, skips


def candidate_kind_label(candidate: dict[str, Any]) -> str:
    if candidate.get("harness_profile_id"):
        return "agent"
    return "llm"
