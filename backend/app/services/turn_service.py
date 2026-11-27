"""Ход агента: реплика покупателя -> agent/runner.py -> события журнала T04, черновик или обращение."""

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from app.agent import runner
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest, Message, Mode, Session, StepTrace
from app.contracts.t04 import HandoffData, MessageData, ProductsData
from app.core import events
from app.core.errors import ApiError
from app.core.llm import BudgetExceededError
from app.db import repository
from app.db.database import get_db
from app.services import quotes

log = logging.getLogger(__name__)

HANDOFF_TEXT = "Передал ваш вопрос оператору — он ответит здесь же, в этом чате."
PILOT_TEXT = "Готовлю ответ, оператор проверит его и пришлёт сюда."

_busy: set[str] = set()
_tasks: set[asyncio.Task] = set()


def is_busy(conversation_id: str) -> bool:
    return conversation_id in _busy


def start_turn(session: Session, action_id: str, text: str, history: list[Message]) -> None:
    """Запускает ход в фоне. Вызывающий проверяет is_busy() под локом приёма команд."""
    _busy.add(session.id)
    task = asyncio.create_task(_run(session, action_id, text, history))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_idle() -> None:
    """Дождаться завершения всех фоновых ходов (тесты, остановка приложения)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def default_mode() -> Mode:
    """Режим для новых диалогов: переключатель /api/v1/manager/mode, иначе DEFAULT_MODE из .env."""
    value = await repository.get_setting("default_mode")
    return value if value in ("autopilot", "pilot") else get_settings().default_mode


async def _run(session: Session, action_id: str, text: str, history: list[Message]) -> None:
    started = time.perf_counter()
    steps: list[StepTrace] = []

    async def emit_step(step: StepTrace) -> None:
        steps.append(step)

    try:
        try:
            reply = await runner.run_turn(session, history, text, emit_step)
        except BudgetExceededError:
            reply = _fallback(session, steps, started, "budget_exceeded", text)
        except Exception:
            log.exception("Agent turn failed for conversation %s", session.id)
            reply = _fallback(session, steps, started, "agent_error", text)
        await _apply(session, action_id, reply)
    except Exception:
        log.exception("Failed to persist turn for conversation %s", session.id)
    finally:
        _busy.discard(session.id)


def _fallback(session: Session, steps: list[StepTrace], started: float, reason: str, question: str) -> AgentReply:
    turn = AgentTurn(
        turn_id=repository.new_id("turn"),
        session_id=session.id,
        steps=steps,
        decision="escalate",
        escalation_reason=reason,
        total_cost_rub=round(sum(s.cost_rub for s in steps), 6),
        total_latency_ms=int((time.perf_counter() - started) * 1000),
    )
    # question видят обе роли: только вопрос покупателя, без внутренних причин
    return AgentReply(
        turn=turn,
        text=HANDOFF_TEXT,
        purpose="notice",
        handoff=HandoffRequest(reason="technical", question=question[:8000]),
    )


def _reply_events(reply: AgentReply) -> list[dict[str, Any]]:
    datas: list[dict[str, Any]] = []
    if reply.products:
        datas.append(ProductsData(products=reply.products).model_dump(mode="json"))
    if reply.text.strip():
        datas.append(
            MessageData(
                author="agent", purpose=reply.purpose, text=reply.text.strip(), sources=reply.sources
            ).model_dump(mode="json")
        )
    return datas


async def _publish(conversation_id: str, action_id: str, datas: list[dict[str, Any]]) -> None:
    if datas:
        async with get_db().tx() as conn:
            written = await append_reply(conn, conversation_id, action_id, datas)
        events.publish(conversation_id, written)


async def append_reply(conn, cid, action_id, datas):
    written = await repository.append_events(cid, action_id, datas, conn=conn)
    if any(d["type"] in ("products", "handoff") or d.get("purpose") in ("answer", "clarification") for d in datas):
        await quotes.invalidate_changed_selection(conn, cid)
    return written


async def _apply(session: Session, action_id: str, reply: AgentReply) -> None:
    cid = session.id
    turn = reply.turn
    current = await repository.get_conversation(cid)
    if turn.decision == "answer" and current is not None and current.session.mode == "pilot":
        turn = turn.model_copy(update={"decision": "draft"})
    for step in turn.steps:
        step.args["journal_action_id"] = action_id
    await repository.save_turn(turn)

    if turn.decision == "answer":
        await _publish(cid, action_id, _reply_events(reply))
        return

    if turn.decision == "draft":
        await repository.create_draft(cid, action_id, _reply_events(reply))
        await _publish(
            cid,
            action_id,
            [MessageData(author="agent", purpose="notice", text=PILOT_TEXT, sources=[]).model_dump(mode="json")],
        )
        return

    # escalate: одно открытое обращение на причину; повтор не создаёт новую карточку
    request = reply.handoff or HandoffRequest(
        reason="insufficient_evidence", question=(reply.text or "Нужна помощь оператора.")[:8000]
    )
    datas = _reply_events(reply)
    async with get_db().tx() as conn:
        existing = await repository.find_open_handoff(cid, request.reason)
        if existing is None:
            handoff = await repository.create_handoff(
                cid,
                action_id,
                request.reason,
                request.question,
                [p.model_dump(mode="json") for p in request.products],
                conn=conn,
            )
            datas.append(HandoffData(handoff=handoff).model_dump(mode="json"))
        written = await append_reply(conn, cid, action_id, datas) if datas else []
    events.publish(cid, written)


async def send_draft(draft_id: str, text: str | None) -> list[dict[str, Any]] | None:
    """Отправка черновика менеджером: события уходят в журнал под исходной квитанцией.

    None — черновика нет или он уже отправлен.
    """
    async with get_db().tx() as conn:
        draft = await repository.get_draft(draft_id)
        if draft is None or draft.status != "pending":
            return None
        datas = [dict(d) for d in draft.events]
        for data in datas:
            if data.get("type") == "quote":
                await quotes.check_exceptions(
                    draft.conversation_id, [line["offerId"] for line in data["quote"]["lines"]]
                )
        messages = [d for d in datas if d.get("type") == "message"]
        if text is not None and not messages:
            datas.append(MessageData(author="agent", purpose="answer", text=text, sources=[]).model_dump(mode="json"))
        for message in messages:
            if text is not None and text.strip() != message["text"].strip():
                message.update(text=text, sources=[])
                continue
            for source in message["sources"]:
                path = urlsplit(source["url"]).path
                _, route, patch_id = path.rpartition("/api/v1/knowledge/")
                if route and patch_id.startswith("p_") and not await repository.live_article(conn, patch_id):
                    raise ApiError(409, "stale_revision", "Статья снята или заменена. Проверьте и измените ответ.")
        await repository.mark_draft_sent(draft_id, conn=conn)
        written = await append_reply(conn, draft.conversation_id, draft.action_id, datas)
    events.publish(draft.conversation_id, written)
    return written
