"""Передача оператору и резервная ветка первого стенда.

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

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from app.agent.catalog_selection import Selection, select, selection_for
from app.agent.handoff import CONFIRMED, ActionType, SearchFinding, route_action
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest, Message, Session, StepTrace
from app.contracts.t04 import Product
from app.core.llm import llm_call
from app.db.repository import last_products, list_turns, new_id

EmitStep = Callable[[StepTrace], Awaitable[None]]

SYSTEM_PROMPT = "Ты — ассистент интернет-магазина Softline. Это тестовый stub каркаса: ответь одной фразой."


async def run_turn(
    session: Session,
    history: list[Message],
    user_text: str,
    emit_step: EmitStep,
    *,
    action_type: ActionType = "message",
    products: list[Product] | None = None,
    finding: SearchFinding | None = None,
) -> AgentReply:
    started = time.perf_counter()
    if products is None:
        products = [Product.model_validate(p) for p in await last_products(session.id)]
    # Explicit requests for a human or a tender take precedence over a new lookup.
    route = route_action(history, user_text, [], action_type=action_type, finding=finding)
    previous = None
    suggestion = ""
    options = []
    if history and not route:
        turns = await list_turns(session.id)
        if turns and turns[-1].steps and turns[-1].steps[0].tool == "catalog_search":
            args = turns[-1].steps[0].args
            previous = Selection(**args["selection"])
            suggestion = args.get("suggested_name", "")
            options = args.get("options", [])

    selected = None if route else selection_for(user_text, previous, suggestion, options)
    hit = None
    steps = []
    if selected:
        index = get_settings().data_dir / "private" / "catalog.sqlite3"
        hit = await asyncio.to_thread(select, index, selected)
        steps.append(
            StepTrace(
                tool="catalog_search",
                args={"selection": asdict(selected), "suggested_name": hit.suggestion, "options": hit.evidence.data},
                source_ref=hit.evidence.source_ref,
                result_summary=hit.evidence.error or hit.text[:450],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        products = hit.products
    # Price/availability decisions must use products from the current search when present.
    if not hit or hit.purpose == "answer":
        route = route_action(history, user_text, products, action_type=action_type, finding=finding)
    if route:
        steps.append(
            StepTrace(
                tool="handoff_policy",
                args={"reason": route.reason},
                result_summary=route.clarification or "Передача оператору",
            )
        )
    if hit or route:
        for step in steps:
            await emit_step(step)
        escalate = route is not None and not route.clarification
        return AgentReply(
            turn=AgentTurn(
                turn_id=new_id("turn"),
                session_id=session.id,
                steps=steps,
                decision="escalate" if escalate else "answer",
                escalation_reason=route.reason if escalate else None,
                total_cost_rub=0,
                total_latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            text=(route.clarification or CONFIRMED) if route else hit.text,
            purpose=("notice" if escalate else "clarification") if route else hit.purpose,
            products=hit.products if hit else [],
            sources=hit.sources if hit else [],
            handoff=HandoffRequest(reason=route.reason, question=route.question, products=products)
            if escalate
            else None,
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
