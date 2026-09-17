"""Normalize native questions without granting tool permissions."""

from typing import Any


def questions_for_ui(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError("Invalid question list")
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            raise ValueError("Invalid question")
        result.append(
            {
                "id": str(item.get("id", index)),
                "question": item["question"][:3000],
                "options": [
                    str(option.get("label", ""))[:500]
                    for option in item.get("options", [])
                    if isinstance(option, dict)
                ][:12],
                "multiple": bool(item.get("multiple", False)),
            }
        )
    return result


def question_answers(message: dict[str, Any], questions: list[dict[str, Any]]) -> list[list[str]]:
    answers = message.get("answers") or ([[message["text"]]] if len(questions) == 1 else [])
    if len(answers) != len(questions):
        raise ValueError("Answer each question")
    return answers
