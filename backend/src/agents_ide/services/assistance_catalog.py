"""Curated zone descriptions. Extend these alongside new assistant capabilities."""

from typing import Literal

from pydantic import Field, model_validator

from agents_ide.domain.schemas import ApiModel, ApiOutput
from agents_ide.services.assistance_playbooks import (
    DIAGNOSTIC_RULES,
    DiagnosticCase,
    diagnostic_cases,
)

Zone = Literal["application", "project", "chat", "runs", "templates", "settings"]


class AssistanceTarget(ApiModel):
    zone: Zone
    project_id: str | None = Field(default=None, min_length=1, max_length=32)
    chat_id: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> "AssistanceTarget":
        if self.zone in {"project", "chat", "runs"} and not self.project_id:
            raise ValueError("Для этой зоны требуется проект")
        if self.zone == "chat" and not self.chat_id:
            raise ValueError("Для зоны диалога требуется диалог")
        if self.zone != "chat" and self.chat_id:
            raise ValueError("Диалог допустим только для зоны chat")
        if self.zone not in {"project", "chat", "runs"} and self.project_id:
            raise ValueError("Эта зона не привязана к проекту")
        return self


class AssistanceGuide(ApiOutput):
    zone: Zone
    title: str
    description: str
    available_data: list[str]
    capabilities: list[str]
    allowed_mutations: list[str]
    questions: list[str]
    diagnostic_rules: list[str] = Field(default_factory=list)
    diagnostic_cases: list[DiagnosticCase] = Field(default_factory=list)


def guide(zone: Zone) -> AssistanceGuide:
    titles: dict[Zone, str] = {
        "application": "Приложение",
        "project": "Проект",
        "chat": "Диалог проекта",
        "runs": "Запуски проекта",
        "templates": "Шаблоны",
        "settings": "Настройки",
    }
    descriptions: dict[Zone, str] = {
        "application": "Рабочая область Agents IDE и состояние отдельной службы исполнения.",
        "project": "Проект объединяет диалоги и запуски. Значок ! суммирует их запросы и ошибки.",
        "chat": "Диалог проекта: задания, запуски и планы. Значок ! требует разбора причины.",
        "runs": "Состояния выполнения этапов, причины ожидания и доступные команды запуска.",
        "templates": "Шаблоны задают граф этапов. Диагностика отдельных узлов пока не подключена.",
        "settings": "Подключения, агенты и журнал. Чтение и изменение настроек пока не подключены.",
    }
    scoped = zone in {"project", "chat", "runs"}
    return AssistanceGuide(
        zone=zone,
        title=titles[zone],
        description=descriptions[zone],
        available_data=["Состояние исполнителя"]
        + (
            [
                "Текущие запуски и планы",
                "Причина ожидания, ошибка этапа, доступные действия",
                "Тип этапа, модель попытки и последние события запуска без текстов сообщений",
                "Сохранённая сессия агента, варианты восстановления и условия их применения",
                "Проверка Git при вопросе об изменениях: пути и признаки, без содержимого файлов",
                "Сравнение защищённых файлов, оценка риска и условия явного принятия пользователем",
            ]
            if scoped
            else []
        ),
        capabilities=["Объяснить состояние", "Предложить проверки и следующие шаги"],
        allowed_mutations=[],
        diagnostic_rules=DIAGNOSTIC_RULES,
        diagnostic_cases=diagnostic_cases(scoped),
        questions=[
            "Что означает восклицательный знак и что сейчас мешает работе?",
            "Что нужно проверить, чтобы продолжить выполнение?",
        ]
        if scoped
        else ["Как работает эта часть приложения?", "Работает ли исполнитель?"],
    )


def catalog() -> list[AssistanceGuide]:
    return [
        guide(zone) for zone in ("application", "project", "chat", "runs", "templates", "settings")
    ]
