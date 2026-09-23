import json

import pytest

from agents_ide.adapters.base import LLMAdapterRequest
from agents_ide.adapters.llm_http import _request_body
from agents_ide.errors import AppError
from agents_ide.services.assistance_suggestions import _generation_params, _parse_questions


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"questions": ["Как продолжить запуск?"]}, ["Как продолжить запуск?"]),
        (["Как продолжить запуск?"], ["Как продолжить запуск?"]),
        (
            {
                "questions": [
                    " Как   продолжить\nзапуск? ",
                    "КАК ПРОДОЛЖИТЬ ЗАПУСК?",
                    None,
                    "x" * 221,
                ],
                "comment": "Ignored extra field",
            },
            ["Как продолжить запуск?"],
        ),
        (
            {"questions": [{"question": "Как проверить новый HEAD?", "id": 1}, {"answer": "skip"}]},
            ["Как проверить новый HEAD?"],
        ),
        (
            {"questions": [f"Вопрос номер {i}?" for i in range(6)]},
            [f"Вопрос номер {i}?" for i in range(4)],
        ),
    ],
)
def test_keeps_usable_questions_without_rejecting_the_whole_list(value, expected):
    assert _parse_questions(json.dumps(value)) == expected


@pytest.mark.parametrize(
    "body,reason",
    [
        ('{"answer": "No questions"}', "missing_questions"),
        ('{"questions": "A string instead of a list"}', "missing_questions"),
        ('{"questions": []}', "no_usable_questions"),
        ('{"questions": [null, 123, {}, ""]}', "no_usable_questions"),
        ('<mm:think>{"questions":["Private reasoning question?"]}</mm:think>', "invalid_json"),
        ('<think mode="reasoning">{"questions":["Private reasoning question?"]}', "invalid_json"),
        ('{"questions":["Incomplete question?"', "invalid_json"),
        ('{"questions":["First answer?"]}\n{"questions":["Second answer?"]}', "invalid_json"),
    ],
)
def test_rejects_incomplete_ambiguous_or_missing_answers_without_leaking_them(body, reason):
    with pytest.raises(AppError) as caught:
        _parse_questions(body)
    assert caught.value.details["reason"] == reason
    assert "Private reasoning" not in str(caught.value)
    assert "Private reasoning" not in json.dumps(caught.value.details)


@pytest.mark.parametrize(
    "url,model,disabled",
    [
        ("https://api.minimax.io/v1", "MiniMax-M3", True),
        ("https://api.minimax.io/v1/", "minimax-m3", True),
        ("https://api.minimax.io/v1", "MiniMax-M2.7", False),
        ("https://example.test/v1", "MiniMax-M3", False),
        ("https://api.minimax.io.example.test/v1", "MiniMax-M3", False),
    ],
)
def test_m3_suggestion_thinking_options_reach_http_request_only_for_matching_provider(
    url, model, disabled
):
    body = _request_body(
        LLMAdapterRequest(
            role="assistance",
            model_id=model,
            prompt="Generate questions",
            context_package={},
            params=_generation_params(url, model),
            response_format="json",
        )
    )
    assert "строго одним JSON-объектом" in body["messages"][0]["content"]
    # M3 does not require/support OpenAI's structured-output flag for this path.
    assert "response_format" not in body
    if disabled:
        assert body["thinking"] == {"type": "disabled"}
        assert body["reasoning_split"] is True
    else:
        assert "thinking" not in body and "reasoning_split" not in body
