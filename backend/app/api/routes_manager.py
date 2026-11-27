"""Менеджерские расширения T05 (/api/v1/manager/*): телеметрия, режим, трейс, черновики, патчи БЗ."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import Field

from app.config import get_settings
from app.contracts.schemas import (
    AgentTurn,
    Draft,
    DraftSend,
    KBPatch,
    KBPatchCreate,
    ModeState,
    ModeUpdate,
    PatchStatus,
    TelemetrySummary,
)
from app.contracts.t04 import ID_PATTERN, Error, Handoff, Id
from app.core.auth import Principal, require_manager
from app.core.errors import ApiError, not_found
from app.core.llm import today_spend_rub, today_start
from app.db import repository
from app.services import conversation_service, turn_service

router = APIRouter(
    prefix="/api/v1/manager",
    tags=["T05 расширения"],
    responses={401: {"model": Error}, 403: {"model": Error}, 404: {"model": Error}},
)

PathId = Annotated[str, Path(min_length=1, max_length=128, pattern=ID_PATTERN)]


@router.get("/telemetry", response_model=TelemetrySummary)
async def telemetry(all_time: bool = False, principal: Principal = Depends(require_manager)) -> TelemetrySummary:
    """LLM-вызовы по шагам за сегодня (all_time=true — за всё время) и последние вызовы."""
    s = get_settings()
    spent = await today_spend_rub()
    return TelemetrySummary(
        today_spend_rub=round(spent, 4),
        daily_budget_rub=s.daily_budget_rub,
        budget_left_rub=round(max(0.0, s.daily_budget_rub - spent), 4),
        usd_rub_rate=s.usd_rub_rate,
        steps=await repository.step_stats(None if all_time else today_start()),
        recent_calls=await repository.recent_llm_calls(20),
    )


@router.get("/mode", response_model=ModeState)
async def get_mode(principal: Principal = Depends(require_manager)) -> ModeState:
    return ModeState(default_mode=await turn_service.default_mode())


@router.post("/mode", response_model=ModeState)
async def set_mode(body: ModeUpdate, principal: Principal = Depends(require_manager)) -> ModeState:
    """Без conversation_id — режим для новых диалогов; с ним — режим конкретного диалога."""
    if body.conversation_id is None:
        await repository.set_setting("default_mode", body.mode)
        return ModeState(default_mode=body.mode)
    if await repository.get_conversation(body.conversation_id) is None:
        raise not_found(conversation_service.CONVERSATION_NOT_FOUND)
    await repository.set_conversation_mode(body.conversation_id, body.mode)
    conv = await repository.get_conversation(body.conversation_id)
    return ModeState(default_mode=await turn_service.default_mode(), conversation=conv.session)


@router.get("/conversations/{conversationId}/trace", response_model=list[AgentTurn])
async def trace(conversationId: PathId, principal: Principal = Depends(require_manager)) -> list[AgentTurn]:
    """Шаги агента по ходам диалога. Диалог вне области доступа оператора — 404."""
    await conversation_service.conversation_for(principal, conversationId)
    return await repository.list_turns(conversationId)


@router.get("/drafts", response_model=list[Draft])
async def list_drafts(principal: Principal = Depends(require_manager)) -> list[Draft]:
    """Черновики режима pilot, ожидающие отправки."""
    return await repository.list_drafts("pending")


@router.post("/drafts/{draftId}/send", response_model=Draft)
async def send_draft(draftId: PathId, body: DraftSend, principal: Principal = Depends(require_manager)) -> Draft:
    """Отправить черновик покупателю как есть или с заменой текста."""
    if await turn_service.send_draft(draftId, body.text) is None:
        if await repository.get_draft(draftId) is None:
            raise not_found("Черновик не найден.")
        raise ApiError(409, "already_resolved", "Черновик уже отправлен.")
    return await repository.get_draft(draftId)


@router.get("/kb/patches", response_model=list[KBPatch])
async def list_patches(
    status: PatchStatus | None = None, principal: Principal = Depends(require_manager)
) -> list[KBPatch]:
    return await repository.list_kb_patches(status)


class ArticleDraft(KBPatchCreate):
    phrasings: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=20)


@router.get("/kb/resolved", response_model=list[Handoff])
async def resolved_cases(principal: Principal = Depends(require_manager)):
    return await repository.list_resolved_handoffs()


@router.get("/kb/articles")
async def article_history(principal: Principal = Depends(require_manager)):
    return await repository.publication_history()


@router.post("/kb/patches", response_model=KBPatch, status_code=201)
async def create_patch(body: ArticleDraft, principal: Principal = Depends(require_manager)) -> KBPatch:
    """Предложить ответ в базу знаний (например, итог обращения)."""
    return await repository.create_kb_patch(
        body.question, body.answer, body.handoff_id, body.scope, tuple(body.phrasings)
    )


@router.post("/kb/patches/{patchId}/approve", response_model=KBPatch)
async def approve_patch(
    patchId: PathId, replaces: Id | None = None, principal: Principal = Depends(require_manager)
) -> KBPatch:
    """Одобренный патч сразу попадает в kb_articles и полнотекстовый поиск."""
    return await repository.set_kb_patch_status(patchId, "approved", principal.id.removeprefix("manager:"), replaces)


@router.post("/kb/patches/{patchId}/reject", response_model=KBPatch)
async def reject_patch(patchId: PathId, principal: Principal = Depends(require_manager)) -> KBPatch:
    return await repository.set_kb_patch_status(patchId, "rejected", principal.id.removeprefix("manager:"))


@router.post("/kb/patches/{patchId}/withdraw", status_code=204)
async def withdraw_patch(patchId: PathId, principal: Principal = Depends(require_manager)):
    await repository.withdraw_kb_patch(patchId, principal.id.removeprefix("manager:"))
