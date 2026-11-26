"""ЗОНА ВАЛЕРЫ. Здесь только минимальный stub для сквозного запуска стенда.

Контракт, который каркас ожидает от агента:

    async def run_turn(session, history, user_text, emit_step) -> AgentReply

- session   — диалог (session.id = conversationId T04);
- history   — сообщения журнала ДО текущей реплики (по возрастанию времени);
- user_text — текущая реплика клиента;
- emit_step — await emit_step(StepTrace) фиксирует шаг трейса (GET /api/v1/manager/conversations/{id}/trace);
  все шаги также нужно вернуть в reply.turn.steps — каркас сохранит ход в БД.

Ответ (AgentReply) каркас пишет в журнал T04: products -> message -> handoff.
- sources — [Source(url="https://...", label="Карточка Softline")]: только https-ссылки;
- products — Product.from_catalog(...) из contracts/t04.py: цены в копейках, priceState по правилам T04;
- handoff — HandoffRequest(reason=<одна из 8 причин T04>, question=..., products=...) при decision="escalate".

LLM — только через app.core.llm.llm_call(). BudgetExceededError можно не ловить:
каркас (services/turn_service.py) сам передаст вопрос оператору (reason="technical").
Режим pilot тоже обрабатывает каркас: decision="answer" превратится в черновик.
"""

import time
from collections.abc import Awaitable, Callable

from app.contracts.schemas import AgentReply, AgentTurn, Message, Session, StepTrace
from app.core.llm import llm_call
from app.db.repository import new_id

EmitStep = Callable[[StepTrace], Awaitable[None]]

SYSTEM_PROMPT = "Ты — ассистент интернет-магазина Softline. Это тестовый stub каркаса: ответь одной фразой."


async def run_turn(session: Session, history: list[Message], user_text: str, emit_step: EmitStep) -> AgentReply:
    started = time.perf_counter()
    resp = await llm_call(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}],
        step="agent_stub",
        profile="smart",
        session_id=session.id,
    )
    step = resp.to_step_trace()
    await emit_step(step)

    turn = AgentTurn(
        turn_id=new_id("turn"),
        session_id=session.id,
        steps=[step],
        decision="answer",
        total_cost_rub=step.cost_rub,
        total_latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return AgentReply(turn=turn, text=f"Эхо: {user_text}\n\n(stub агента; модель ответила: {resp.content})")
