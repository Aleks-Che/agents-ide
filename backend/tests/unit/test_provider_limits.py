import json

import pytest

from agents_ide.adapters.provider_limits import limit_error_code


@pytest.mark.parametrize(
    "error,status,expected",
    [
        ({"code": "insufficient_quota"}, 429, "provider_quota_exhausted"),
        (
            {"message": "The Token Plan usage limit has been reached. (2067)"},
            429,
            "provider_quota_exhausted",
        ),
        (
            {"responseBody": json.dumps({"error": {"code": "insufficient_quota"}})},
            429,
            "provider_quota_exhausted",
        ),
        ({"codexErrorInfo": "usageLimitExceeded"}, None, "provider_quota_exhausted"),
        ({"type": "rate_limit_error"}, None, "provider_rate_limited"),
        ("rate limited", 429, "provider_rate_limited"),
        (None, 429, "provider_rate_limited"),
        ({}, 402, "provider_quota_exhausted"),
        ({"message": "maximum context length exceeded"}, 400, None),
        ({"message": "connection lost"}, 500, None),
        ({"message": "quota configuration invalid"}, 400, None),
        ({"responseBody": "not json"}, 500, None),
    ],
)
def test_explicit_limit_classification(error, status, expected):
    assert limit_error_code(error, status) == expected
