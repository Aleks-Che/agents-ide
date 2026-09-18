"""Opt-in cleanup of JSON answers, without repairing or guessing model output."""

import json
import re
from typing import Any

_THINKING = re.compile(r"<(think|thinking|analysis|reasoning)\s*>", re.IGNORECASE)
_JSON_OR_THINKING = re.compile(r"<(think|thinking|analysis|reasoning)\s*>|[\[{]", re.IGNORECASE)


def _thinking_end(text: str, opening: re.Match[str]) -> int:
    closing = re.search(rf"</{opening[1]}\s*>", text[opening.end() :], re.IGNORECASE)
    if closing is None:
        raise ValueError("Unclosed thinking block")
    return opening.end() + closing.end()


def _json_start(text: str, offset: int = 0) -> int | None:
    # JSON examples inside reasoning are not answers. Scan only outside closed
    # blocks; never remove tags from the JSON container or its string values.
    while token := _JSON_OR_THINKING.search(text, offset):
        if token[1] is None:
            return token.start()
        offset = _thinking_end(text, token)
    return None


def parse_json_response(
    text: str, *, strip_thinking_tags: bool = False, extract_json: bool = False
) -> Any:
    candidate = text.strip()
    if strip_thinking_tags:
        # Only leading blocks are removable: tags inside JSON string values are data.
        while opening := _THINKING.match(candidate):
            candidate = candidate[_thinking_end(candidate, opening) :].lstrip()
    try:
        return json.loads(candidate)
    except ValueError:
        if not extract_json:
            raise
    start = _json_start(candidate)
    if start is None:
        raise ValueError("No JSON answer outside reasoning")
    # Decode the first whole container. Never salvage a nested object from a
    # malformed/truncated answer, or silently choose between multiple answers.
    value, end = json.JSONDecoder().raw_decode(candidate, start)
    if _json_start(candidate, end) is not None:
        raise ValueError("Ambiguous JSON answer")
    return value
