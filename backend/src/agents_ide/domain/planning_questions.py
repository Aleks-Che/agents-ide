"""Plan question parser ported from sources/claudexor/.../planQuestions.ts.

The orchestrator's parser restricts itself to the INSTRUCTED ``## Open
Questions`` block of one plan text. We retain the QA-016 boundary rules:

* once a structured bullet appears, the FIRST non-tagging bullet ends the set;
* `(none)` is a terminal bullet (a `ready` plan with zero questions);
* a legacy untagged block keeps a tolerant behaviour: an untagged bullet with
  2+ ``::`` options becomes a single-choice question; otherwise free text.

Each question receives a stable ``id`` (``q1..qN``) within its parsed text.
The engine parser never reaches into the user-visible answer widget — only
the server-derived ``questions`` and ``readiness`` projection.
"""

from __future__ import annotations

import re
from typing import Any, Final

RECOGNIZED_KINDS: Final = frozenset({"single", "multi", "text"})


def _is_heading(line: str) -> bool:
    return bool(re.match(r"^#{1,6}\s+\S", line))


def _heading_text(line: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", line).strip()


def _is_none_bullet(body: str) -> bool:
    return re.match(r"^\(?none\)?\.?$", body.strip(), re.IGNORECASE) is not None


def _bullet_body(raw_line: str) -> str | None:
    stripped = raw_line.strip()
    if not (stripped.startswith("- ") or stripped.startswith("* ")):
        return None
    return stripped[2:].strip()


def _recognized_tag(body: str) -> str | None:
    if not body.startswith("["):
        return None
    close = body.find("]")
    if close <= 0:
        return None
    tag = body[1:close].strip().lower()
    return tag if tag in RECOGNIZED_KINDS else None


def _parse_tagged(body: str, kind: str) -> dict[str, Any] | None:
    rest = body[body.find("]") + 1 :].strip()
    if _is_none_bullet(rest):
        return None
    segments = [seg.strip() for seg in rest.split("::") if seg.strip()]
    if len(segments) > 1:
        prompt_text = segments[0] or ""
        options = [{"id": f"o{idx + 1}", "label": label} for idx, label in enumerate(segments[1:])]
    else:
        prompt_text = rest
        options = []
    if not prompt_text:
        return None
    resolved_kind = kind if options or kind == "text" else "text"
    return {
        "kind": resolved_kind,
        "prompt": prompt_text,
        "options": options,
        "allow_text": True,
    }


def _parse_legacy_unstructured(body: str) -> dict[str, Any] | None:
    if _is_none_bullet(body):
        return None
    segments = [seg.strip() for seg in body.split("::") if seg.strip()]
    if len(segments) >= 3:
        prompt_text = segments[0] or ""
        options = [{"id": f"o{idx + 1}", "label": label} for idx, label in enumerate(segments[1:])]
    else:
        prompt_text = body
        options = []
    if not prompt_text:
        return None
    return {
        "kind": "single" if options else "text",
        "prompt": prompt_text,
        "options": options,
        "allow_text": True,
    }


def _parse_question_block(block_lines: list[str]) -> list[dict[str, Any]]:
    bodies = [b for b in (_bullet_body(line) for line in block_lines) if b is not None]
    if not bodies:
        return []
    structured = any(_recognized_tag(body) is not None for body in bodies)
    questions: list[dict[str, Any]] = []
    for body in bodies:
        if _is_none_bullet(body):
            break
        tag = _recognized_tag(body)
        if structured and tag is None:
            break
        parsed = _parse_tagged(body, tag) if tag is not None else _parse_legacy_unstructured(body)
        if parsed is None:
            continue
        parsed["id"] = f"q{len(questions) + 1}"
        questions.append(parsed)
    return questions


def extract_plan_questions(plan: str) -> dict[str, Any]:
    """Parse one plan text; return ``{parse, questions}``.

    ``parse`` is ``found`` when a recognized heading was located (even with
    zero questions) and ``none_found`` when no heading exists. The returned
    list favours the most structured block when several share the same
    heading.
    """

    lines = plan.split("\n")
    blocks: list[list[dict[str, Any]]] = []
    saw_heading = False
    for offset, line in enumerate(lines):
        stripped = line.strip()
        if not _is_heading(stripped):
            continue
        if "open questions" not in _heading_text(stripped).lower():
            continue
        saw_heading = True
        block: list[str] = []
        for raw in lines[offset + 1 :]:
            if _is_heading(raw.strip()):
                break
            block.append(raw)
        # Skip the echoed instruction template — it quotes the format itself.
        if any(
            "in exactly this format" in raw.lower() or "[single] = pick exactly one" in raw.lower()
            for raw in block
        ):
            continue
        blocks.append(_parse_question_block(block))

    best: list[dict[str, Any]] = []
    best_score = (-1, -1)
    for items in blocks:
        tagged = sum(1 for q in items if q["kind"] in {"single", "multi"})
        score = (tagged, len(items))
        if score > best_score:
            best_score = score
            best = items
    return {
        "parse": "found" if saw_heading else "none_found",
        "questions": best,
    }


def derive_readiness(parse: str, questions: list[dict[str, Any]]) -> str:
    if parse == "invalid_format":
        return "invalid_format"
    if parse == "none_found":
        return "unverified"
    return "ready" if not questions else "needs_answers"


def hash_questions(questions: list[dict[str, Any]]) -> str:
    """Deterministic hash of the parsed question list."""

    import hashlib
    import json

    canonical = [
        {
            "id": q["id"],
            "kind": q["kind"],
            "prompt": q["prompt"],
            "options": [list(opt.values()) for opt in q.get("options", [])],
            "allow_text": q.get("allow_text", True),
        }
        for q in questions
    ]
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
