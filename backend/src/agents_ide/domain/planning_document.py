"""Strict Council output. Markdown parsing is never evidence of readiness."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from agents_ide.domain.schemas import ApiModel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class PlanStep(ApiModel):
    title: Text
    acceptance_criteria: list[Text] = Field(min_length=1, max_length=30)


class QuestionOption(ApiModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    label: Text


class PlanQuestion(ApiModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    kind: Literal["single", "multi", "text"]
    prompt: Text
    options: list[QuestionOption] = Field(default_factory=list, max_length=32)
    allow_text: bool = False

    @model_validator(mode="after")
    def valid_options(self) -> PlanQuestion:
        if self.kind == "text" and self.options:
            raise ValueError("text question cannot have options")
        if self.kind != "text" and len(self.options) < 2:
            raise ValueError("choice question requires at least two options")
        if len({o.id for o in self.options}) != len(self.options):
            raise ValueError("duplicate option IDs")
        return self


class PlanDocument(ApiModel):
    body_text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48000)
    ]
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    questions: list[PlanQuestion] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_questions(self) -> PlanDocument:
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question IDs")
        return self

    def items(self) -> list[dict[str, Any]]:
        return [{"id": f"P{i}", **step.model_dump()} for i, step in enumerate(self.steps, 1)]

    @classmethod
    def native_output_schema(cls) -> dict[str, Any]:
        """Native strict output requires every property, including defaulted ones."""

        def strict(value: Any) -> Any:
            if isinstance(value, list):
                return [strict(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: strict(item) for key, item in value.items() if key != "default"}
            if result.get("type") == "object":
                result["required"] = list(result.get("properties", {}))
                result["additionalProperties"] = False
            return result

        schema: dict[str, Any] = strict(cls.model_json_schema())
        return schema


def parse_document(body: str) -> PlanDocument:
    if len(body.encode("utf-8")) > 65536:
        raise ValueError("Council output exceeds 64 KiB")
    return PlanDocument.model_validate_json(body)
