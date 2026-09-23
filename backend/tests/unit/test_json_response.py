import pytest

from agents_ide.engine.json_response import parse_json_response, strip_leading_thinking


def test_plain_answer_strips_provider_reasoning_but_preserves_answer_markup():
    text = ' \n<MM:THINK mode="reasoning">private</MM:THINK>\n<analysis>private</analysis>**Ответ**'
    assert strip_leading_thinking(text) == "**Ответ**"
    assert (
        strip_leading_thinking("Пример: `<think>текст</think>`") == "Пример: `<think>текст</think>`"
    )
    for incomplete in ("<think>private", "<think>private<think>nested</think>private</think>"):
        with pytest.raises(ValueError):
            strip_leading_thinking(incomplete)


@pytest.mark.parametrize(
    "text,options",
    [
        ('{"answer":"NO"}', {}),
        (
            '<think>Reasoning, including {"answer":"YES"}.</think>\n{"answer":"NO"}',
            {"strip_thinking_tags": True},
        ),
        (
            ' <THINKING>First</THINKING>\n<analysis>Second</analysis>\n{"answer":"NO"}',
            {"strip_thinking_tags": True},
        ),
        ('```json\n{"answer":"NO"}\n```', {"extract_json": True}),
        ('Answer:\n{"answer":"NO"}\nThat is all.', {"extract_json": True}),
        ('<think>Reasoning</think>\n{"answer":"NO"}', {"extract_json": True}),
        (
            'Response: <think>Example: {"answer":"YES"}</think>\n```json\n{"answer":"NO"}\n```',
            {"extract_json": True},
        ),
        (
            '<THINKING>First</THINKING>\n<analysis>Second</analysis>\n{"answer":"NO"}',
            {"extract_json": True},
        ),
        (
            'Answer: {"answer":"NO"}\n<reasoning>Example: {"answer":"YES"}</reasoning>',
            {"extract_json": True},
        ),
        (
            '<think>Reasoning</think>\n```json\n{"answer":"NO"}\n```',
            {"strip_thinking_tags": True, "extract_json": True},
        ),
    ],
)
def test_json_processing(text, options):
    assert parse_json_response(text, **options) == {"answer": "NO"}


@pytest.mark.parametrize(
    "text,options",
    [
        ('<think>Reasoning</think>{"answer":"NO"}', {}),
        ('```json\n{"answer":"NO"}\n```', {}),
        ('```json\n{"answer":"NO"}\n```', {"strip_thinking_tags": True}),
        ('<think>Only an example: {"answer":"NO"}</think>', {"extract_json": True}),
        ('<think>Unfinished {"answer":"NO"}', {"extract_json": True}),
        ('<think>Unfinished {"answer":"NO"}', {"strip_thinking_tags": True, "extract_json": True}),
        ('{"answer":"YES"}\n{"answer":"NO"}', {"extract_json": True}),
        (
            '<think>Reasoning</think>{"answer":"YES"}\n{"answer":"NO"}',
            {"extract_json": True},
        ),
        ('```json\n{"answer":"YES"}\n```\n```json\n{"answer":"NO"}\n```', {"extract_json": True}),
        ('{"unfinished": {"answer":"NO"}', {"extract_json": True}),
        ('[{"answer":"NO"}', {"extract_json": True}),
        ("No answer", {"extract_json": True}),
    ],
)
def test_does_not_guess_or_repair_answers(text, options):
    with pytest.raises(ValueError):
        parse_json_response(text, **options)


def test_tags_and_braces_inside_json_strings_are_preserved():
    text = '{"answer":"<think>{reasoning}</think> and \\"quoted\\""}'
    assert parse_json_response(text, strip_thinking_tags=True, extract_json=True) == {
        "answer": '<think>{reasoning}</think> and "quoted"'
    }
    assert parse_json_response("Answer: " + text, extract_json=True) == {
        "answer": '<think>{reasoning}</think> and "quoted"'
    }
