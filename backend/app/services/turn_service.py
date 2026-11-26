"""Оркестрация хода: реплика клиента -> agent/runner.py -> сообщение / черновик / эскалация + SSE."""

import asyncio
import logging
import time

from app.agent import runner
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, Message, Mode, Session, StepTrace, Ticket
from app.core import events
from app.core.llm import BudgetExceededError
from app.db import repository

log = logging.getLogger(__name__)

HANDOFF_TEXT = "Передал ваш вопрос менеджеру — он ответит здесь же, в этом чате."
PILOT_TEXT = "Готовлю ответ, менеджер проверит его и пришлёт сюда."

_busy: set[str] = set()
_tasks: set[asyncio.Task] = set()


def is_busy(session_id: str) -> bool:
    return session_id in _busy


def start_turn(session: Session, client_msg: Message) -> None:
    """Запускает ход в фоне. Вызывающий обязан проверить is_busy() до сохранения реплики."""
    _busy.add(session.id)
    task = asyncio.create_task(_run(session, client_msg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_idle() -> None:
    """Дождаться завершения всех фоновых ходов (тесты, остановка приложения)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _run(session: Session, client_msg: Message) -> None:
    started = time.perf_counter()
    steps: list[StepTrace] = []

    async def emit_step(step: StepTrace) -> None:
        steps.append(step)
        events.publish(session.id, "step_trace", step)

    try:
        history = [m for m in await repository.list_messages(session.id) if m.id != client_msg.id]
        try:
            reply = await runner.run_turn(session, history, client_msg.text, emit_step)
        except BudgetExceededError as e:
            reply = _fallback(session, steps, started, "budget_exceeded", str(e))
        except Exception as e:
            log.exception("Agent turn failed for session %s", session.id)
            reply = _fallback(session, steps, started, "agent_error", f"{type(e).__name__}: {e}")
        await _apply(session, client_msg, reply)
    except Exception:
        log.exception("Failed to persist turn for session %s", session.id)
    finally:
        _busy.discard(session.id)


def _fallback(session: Session, steps: list[StepTrace], started: float, reason: str, detail: str) -> AgentReply:
    turn = AgentTurn(
        turn_id=repository.new_id("turn"),
        session_id=session.id,
        steps=steps,
        decision="escalate",
        escalation_reason=reason,
        total_cost_rub=round(sum(s.cost_rub for s in steps), 6),
        total_latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return AgentReply(turn=turn, text=HANDOFF_TEXT, ticket_summary=detail)


async def _apply(session: Session, client_msg: Message, reply: AgentReply) -> None:
    turn = reply.turn
    current = await repository.get_session(session.id) or session
    if turn.decision == "answer" and current.mode == "pilot":
        turn = turn.model_copy(update={"decision": "draft"})
    await repository.save_turn(turn)

    if turn.decision == "answer":
        await _agent_message(session.id, reply.text, reply)
        return

    if turn.decision == "draft":
        ticket = await repository.create_ticket(
            session.id,
            "draft",
            reason="pilot_review",
            summary=reply.ticket_summary or client_msg.text[:500],
            collected_params=reply.collected_params,
            draft_answer=reply.text,
        )
        await _ticket_created(ticket)
        await _agent_message(session.id, PILOT_TEXT)
        return

    # escalate: одно открытое обращение на диалог, повторная эскалация дополняет его
    reason = turn.escalation_reason or "agent_escalation"
    summary = reply.ticket_summary or client_msg.text[:500]
    existing = await repository.find_open_ticket(session.id, "escalation")
    if existing:
        await repository.update_ticket(
            existing.id,
            summary=f"{existing.summary}\n\n{summary}",
            collected_params={**existing.collected_params, **reply.collected_params},
        )
    else:
        ticket = await repository.create_ticket(
            session.id, "escalation", reason=reason, summary=summary, collected_params=reply.collected_params
        )
        await _ticket_created(ticket)
    await _agent_message(session.id, reply.text or HANDOFF_TEXT, reply)


async def _agent_message(session_id: str, text: str, reply: AgentReply | None = None) -> None:
    msg = await repository.add_message(
        session_id,
        "agent",
        text,
        sources=reply.sources if reply else [],
        quote_id=reply.quote_id if reply else None,
    )
    events.publish(session_id, "message_done", msg)


async def _ticket_created(ticket: Ticket) -> None:
    # SSE-поток сессии видит клиент: внутренние поля (черновик, сводка, параметры) не отдаём
    public = ticket.model_copy(update={"summary": "", "collected_params": {}, "draft_answer": None})
    events.publish(ticket.session_id, "ticket_created", public)


async def default_mode() -> Mode:
    """Режим для новых сессий: переключатель из /api/admin/mode, иначе DEFAULT_MODE из .env."""
    value = await repository.get_setting("default_mode")
    return value if value in ("autopilot", "pilot") else get_settings().default_mode
