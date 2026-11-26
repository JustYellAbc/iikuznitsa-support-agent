"""Передача оператору и резервная ветка первого стенда.

Контракт, который каркас ожидает от агента:

    async def run_turn(session, history, user_text, emit_step) -> AgentReply

- history   — сообщения диалога ДО текущей реплики (по возрастанию времени);
- user_text — текущая реплика клиента;
- emit_step — await emit_step(StepTrace) публикует шаг в SSE сразу (панель трейса);
  все шаги также нужно вернуть в reply.turn.steps — каркас сохранит ход в БД.

LLM — только через app.core.llm.llm_call(). BudgetExceededError можно не ловить:
каркас (services/turn_service.py) сам переведёт диалог менеджеру.
Режим pilot тоже обрабатывает каркас: decision="answer" превратится в черновик.
"""

import time
from collections.abc import Awaitable, Callable

from app.agent.handoff import CONFIRMED, OFFER_HELP, ActionType, SearchFinding, route_action, selected_products
from app.contracts.schemas import AgentReply, AgentTurn, Message, Session, StepTrace
from app.core.llm import llm_call
from app.db.repository import new_id

EmitStep = Callable[[StepTrace], Awaitable[None]]

SYSTEM_PROMPT = "Ты — ассистент интернет-магазина Softline. Это тестовый stub каркаса: ответь одной фразой."


async def run_turn(
    session: Session,
    history: list[Message],
    user_text: str,
    emit_step: EmitStep,
    *,
    action_type: ActionType = "message",
    products: list[dict] | None = None,
    finding: SearchFinding | None = None,
) -> AgentReply:
    started = time.perf_counter()
    if products is None:
        products = await selected_products(session, history)
    route = route_action(history, user_text, products, action_type=action_type, finding=finding)
    if route:
        step = StepTrace(
            tool="handoff_policy",
            args={"reason": route.reason},
            result_summary="Предложено подключение оператора" if route.offer_help else "Передача оператору",
        )
        await emit_step(step)
        return AgentReply(
            turn=AgentTurn(
                turn_id=new_id("turn"),
                session_id=session.id,
                steps=[step],
                decision="answer" if route.offer_help else "escalate",
                escalation_reason=None if route.offer_help else route.reason,
                total_cost_rub=0,
                total_latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            text=OFFER_HELP if route.offer_help else CONFIRMED,
            ticket_summary=route.question,
            collected_params={"question": route.question, "products": products},
        )
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
