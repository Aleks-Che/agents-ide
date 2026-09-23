from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from agents_ide.api.deps import get_secret_store, get_session, get_settings
from agents_ide.config import Settings
from agents_ide.security.secrets import SecretStore
from agents_ide.services import assistance
from agents_ide.services.assistance_catalog import AssistanceGuide, AssistanceTarget, catalog
from agents_ide.services.assistance_suggestions import (
    AssistanceSuggestions,
    SuggestionsRequest,
    generate,
)
from agents_ide.services.assistance_tools import (
    AssistanceToolRequest,
    AssistanceToolResult,
    execute,
)
from agents_ide.services.commit_connection import CommitConnectionReview, review_connection

router = APIRouter(prefix="/api/assistance", tags=["assistance"])
SessionDep = Annotated[Session, Depends(get_session, scope="function")]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/catalog", response_model=list[AssistanceGuide])
def assistance_catalog() -> list[AssistanceGuide]:
    return catalog()


@router.get("/models", response_model=list[assistance.AssistanceModel])
def assistance_models(session: SessionDep) -> list[assistance.AssistanceModel]:
    return assistance.models(session)


@router.get("/context", response_model=assistance.AssistanceContext)
def assistance_context(
    session: SessionDep, settings: SettingsDep, target: Annotated[AssistanceTarget, Query()]
) -> assistance.AssistanceContext:
    return assistance.collect_context(session, settings, target)


@router.post("/messages", response_model=assistance.AssistanceAnswer)
def assistance_message(
    session: SessionDep,
    settings: SettingsDep,
    secrets: Annotated[SecretStore, Depends(get_secret_store)],
    payload: assistance.AssistanceMessage,
) -> assistance.AssistanceAnswer:
    return assistance.answer(session, settings, secrets, payload)


@router.post("/suggestions", response_model=AssistanceSuggestions)
def assistance_suggestions(
    session: SessionDep,
    settings: SettingsDep,
    secrets: Annotated[SecretStore, Depends(get_secret_store)],
    payload: SuggestionsRequest,
) -> AssistanceSuggestions:
    return generate(session, settings, secrets, payload)


@router.post("/tools/execute", response_model=AssistanceToolResult)
def assistance_tool(session: SessionDep, payload: AssistanceToolRequest) -> AssistanceToolResult:
    return execute(session, payload)


@router.get("/runs/{run_id}/commit-connection", response_model=CommitConnectionReview)
def assistance_commit_connection(session: SessionDep, run_id: str) -> CommitConnectionReview:
    return review_connection(session, run_id)
