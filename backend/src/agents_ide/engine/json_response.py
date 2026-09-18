"""Opt-in cleanup of JSON answers, without repairing or guessing model output."""

import json
import re
from typing import Any

_THINKING = re.compile(r"<(think|thinking|analysis|reasoning)\s*>", re.IGNORECASE)
_JSON_START = re.compile(r"[\[{]")


def parse_json_response(
    text: str, *, strip_thinking_tags: bool = False, extract_json: bool = False
) -> Any:
    candidate = text.strip()
    if strip_thinking_tags:
        # Only leading blocks are removable: tags inside JSON string values are data.
        while opening := _THINKING.match(candidate):
            closing = re.search(rf"</{opening[1]}\s*>", candidate[opening.end() :], re.IGNORECASE)
            if closing is None:
                raise ValueError("Unclosed thinking block")
            candidate = candidate[opening.end() + closing.end() :].lstrip()
    try:
        return json.loads(candidate)
    except ValueError:
        if not extract_json:
            raise
    start = _JSON_START.search(candidate)
    if start is None or _THINKING.search(candidate[: start.start()]):
        raise ValueError("No JSON answer outside reasoning")
    # Decode the first whole container. Never salvage a nested object from a
    # malformed/truncated answer, or silently choose between multiple answers.
    value, end = json.JSONDecoder().raw_decode(candidate, start.start())
    if _JSON_START.search(candidate[end:]):
        raise ValueError("Ambiguous JSON answer")
    return value
