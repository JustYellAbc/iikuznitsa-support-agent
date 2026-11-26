from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Path as PathParam

from app.config import get_settings
from app.contracts.schemas import (
    KBPatch,
    ModeState,
    ModeUpdate,
    OperatorReply,
    PatchStatus,
    TelemetrySummary,
    Ticket,
    TicketStatus,
    TicketWithContext,
)
from app.core import events
from app.core.llm import today_spend_rub, today_start
from app.core.security import require_operator
from app.db import repository
from app.services import turn_service

router = APIRouter(dependencies=[Depends(require_operator)], tags=["manager"])

Id = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


# --- Тикеты ----------------------------------------------------------------------


@router.get("/api/operator/tickets", response_model=list[TicketWithContext])
async def list_tickets(status: TicketStatus | None = None, since: datetime | None = None) -> list[TicketWithContext]:
    """Черновики и эскалации (новые первыми) с перепиской и трейсами ходов. since — фильтр по updated_at."""
    result = []
    for ticket in await repository.list_tickets(status=status, since=since):
        result.append(
            TicketWithContext(
                ticket=ticket,
                messages=await repository.list_messages(ticket.session_id),
                turns=await repository.list_turns(ticket.session_id),
            )
        )
    return result


@router.post("/api/operator/tickets/{ticket_id}/reply", response_model=Ticket)
async def reply_ticket(body: OperatorReply, ticket_id: Id, operator: str = Depends(require_operator)) -> Ticket:
    """Ответ менеджера в диалог клиента (SSE operator_reply). send_draft=true — отправить черновик в 1 клик."""
    ticket = await repository.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тикет не найден")
    if ticket.status == "resolved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Тикет уже закрыт")
    if body.send_draft:
        if not ticket.draft_answer:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "У тикета нет черновика")
        text = ticket.draft_answer
    else:
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пустой ответ")

    msg = await repository.add_message(ticket.session_id, "operator", text)
    events.publish(ticket.session_id, "operator_reply", msg)

    fields: dict = {"status": "resolved", "operator_answer": text}
    if body.propose_patch:
        patch = await repository.create_kb_patch(question=ticket.summary, answer=text, ticket_id=ticket.id)
        fields["suggested_patch_id"] = patch.id
    return await repository.update_ticket(ticket.id, **fields)


# --- Патчи БЗ ------------------------------------------------------------------------


@router.get("/api/kb/patches", response_model=list[KBPatch])
async def list_patches(status: PatchStatus | None = None) -> list[KBPatch]:
    return await repository.list_kb_patches(status)


async def _moderate(patch_id: str, new_status: PatchStatus, operator: str) -> KBPatch:
    patch = await repository.get_kb_patch(patch_id)
    if patch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Патч не найден")
    if patch.status != "proposed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Патч уже {patch.status}")
    return await repository.set_kb_patch_status(patch_id, new_status, operator)


@router.post("/api/kb/patches/{patch_id}/approve", response_model=KBPatch)
async def approve_patch(patch_id: Id, operator: str = Depends(require_operator)) -> KBPatch:
    """Одобренный патч сразу попадает в kb_articles и полнотекстовый поиск."""
    return await _moderate(patch_id, "approved", operator)


@router.post("/api/kb/patches/{patch_id}/reject", response_model=KBPatch)
async def reject_patch(patch_id: Id, operator: str = Depends(require_operator)) -> KBPatch:
    return await _moderate(patch_id, "rejected", operator)


# --- Режим pilot / autopilot ---------------------------------------------------------------


@router.get("/api/admin/mode", response_model=ModeState)
async def get_mode() -> ModeState:
    return ModeState(default_mode=await turn_service.default_mode())


@router.post("/api/admin/mode", response_model=ModeState)
async def set_mode(body: ModeUpdate) -> ModeState:
    """Без session_id — режим для новых сессий; с session_id — режим конкретного диалога."""
    if body.session_id is None:
        await repository.set_setting("default_mode", body.mode)
        return ModeState(default_mode=body.mode)
    if await repository.get_session(body.session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    await repository.set_session_mode(body.session_id, body.mode)
    return ModeState(
        default_mode=await turn_service.default_mode(), session=await repository.get_session(body.session_id)
    )


# --- Телеметрия -------------------------------------------------------------------------------


@router.get("/api/telemetry/summary", response_model=TelemetrySummary)
async def telemetry_summary(all_time: bool = False) -> TelemetrySummary:
    """Агрегация LLM-вызовов по шагам за сегодня (all_time=true — за всё время) и последние вызовы."""
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
