"""Recognize explicit provider limit errors, never ordinary assistant output."""

from __future__ import annotations

import json
from typing import Any


def limit_error_code(error: Any, status: int | None = None) -> str | None:
    if not isinstance(error, dict):
        error = {"message": error} if isinstance(error, str) else {}
    values = [
        str(error.get(key, "")).lower() for key in ("code", "type", "message", "codexErrorInfo")
    ]
    text = " ".join(values)
    if status == 402 or any(
        marker in text
        for marker in (
            "insufficient_quota",
            "usage_limit_reached",
            "usage_limit_exceeded",
            "quota_exceeded",
            "usagelimitexceeded",
            "billing_hard_limit_reached",
            "credit_balance_too_low",
            "insufficient balance",
            "insufficient_balance",
            "insufficient credit",
            "credit balance is too low",
            "usage limit has been reached",
            "exceeded your current quota",
            "quota exhausted",
        )
    ):
        return "provider_quota_exhausted"
    # Some harnesses keep the structured provider error in responseBody.
    body = error.get("responseBody")
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            nested = limit_error_code(payload.get("error"))
            if nested:
                return nested
    if status == 429 or any(
        value in {"rate_limit_exceeded", "rate_limit_error", "ratelimitexceeded"}
        for value in values
    ):
        return "provider_rate_limited"
    return None
