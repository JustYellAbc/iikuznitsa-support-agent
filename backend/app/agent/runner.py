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
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace

import app.agent.needs as needs
from app.agent.catalog_selection import Selection, named_request, select, selection_for
from app.agent.handoff import (
    CONFIRMED,
    HUMAN_REQUEST,
    OFFER_HELP,
    ActionType,
    SearchFinding,
    bare_request,
    handoff_reason,
    route_action,
)
from app.agent.knowledge import answer_published
from app.agent.product_answers import answer_facts, question_target, refresh_products, topics_for
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest, Message, Session, StepTrace
from app.contracts.t04 import Product
from app.core.llm import llm_call
from app.db.repository import last_products, new_id, visible_turns

EmitStep = Callable[[StepTrace], Awaitable[None]]

SYSTEM_PROMPT = "Ты — ассистент интернет-магазина Softline. Это тестовый stub каркаса: ответь одной фразой."


def commercial_clause(clause: str) -> bool:
    if not clause.strip():
        return False
    if not handoff_reason(clause.casefold()) or Selection("").qualifiers(clause, followup=True) != Selection(""):
        return True
    target = named_request(clause)
    return target is not None and not handoff_reason("нужна " + target.name.casefold())


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
    if products is None:
        products = [Product.model_validate(p) for p in await last_products(session.id)]
    # Approved procedures can interrupt an active selection. Commercial facts and
    # mandatory handoffs always keep their own source, regardless of punctuation.
    if (
        action_type == "message"
        and not topics_for(user_text + "?", bool(products))
        and not route_action(history, user_text, products, action_type=action_type, finding=finding)
    ):
        published = await answer_published(session.id, user_text, emit_step)
        if published:
            return published
    recommendation = await needs.run_turn(
        session, history, user_text, emit_step, action_type=action_type, finding=finding
    )
    if recommendation:
        return recommendation
    started = time.perf_counter()
    original_products = products
    index = get_settings().data_dir / "private" / "catalog.sqlite3"
    bare = bare_request(user_text.casefold())
    route = route_action(history, user_text, products if bare else [], action_type=action_type, finding=finding)
    search_text = user_text
    if route:
        search_text = ""
        if action_type == "message" and not bare and route.reason != "insufficient_evidence":
            # Keep the original question on the card, and look up any accompanying change of terms.
            remaining = HUMAN_REQUEST.sub(" ", user_text).strip(" .,!?:;")
            clauses = re.split(r"[!?;]|\.(?=\s|$)", remaining)
            search_text = ". ".join(c for c in clauses if commercial_clause(c))
    previous = None
    suggestion = ""
    options = []
    pending_question = ""
    awaiting_seats = False
    if history:
        turns = [t for t in await visible_turns(session.id) if not t.steps or t.steps[0].tool != "published_knowledge"]
        if turns and turns[-1].decision in {"answer", "draft"} and turns[-1].steps:
            last_step = turns[-1].steps[-1]
            pending_question = last_step.args.get("awaiting_product_for", "")
            awaiting_seats = last_step.args.get(
                "awaiting_seats", "На сколько пользователей" in last_step.result_summary
            )
            if route and (bare or route.reason == "insufficient_evidence") and last_step.tool == "product_facts":
                route = replace(route, question=last_step.args.get("question", route.question))
        for turn in reversed(turns):
            if not turn.steps:
                break
            step = turn.steps[0]
            if step.tool == "product_facts" and turn.decision in {"answer", "draft"}:
                continue
            if step.tool == "catalog_search":
                args = step.args
                previous = Selection(**args.get("followup_selection", args["selection"]))
                suggestion = args.get("suggested_name", "")
                options = args.get("options", [])
            break

    topics = topics_for(user_text, bool(products)) if not route else set()
    selected = (
        selection_for(search_text, previous, suggestion, options, awaiting_seats=awaiting_seats)
        if search_text
        else None
    )
    if topics:
        target = question_target(user_text)
        if target is None:
            selected = None
            if previous:
                updated = previous.qualifiers(user_text, followup=True)
                # A question about post-expiry rights describes the current offer.
                updated.perpetual = previous.perpetual
                if updated != previous:
                    selected = updated
        elif selected is None or selected.name.casefold() != target.name.casefold():
            selected = target.qualifiers(user_text, followup=False)
    fact_question = user_text
    if pending_question and selected and not route and not topics:
        fact_question = pending_question
        topics = topics_for(fact_question, True)
    hit = None
    steps = []
    if selected:
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
        if hit.followup:
            steps[-1].args["followup_selection"] = asdict(hit.followup)
        products = [] if route and hit.purpose != "answer" else hit.products
    if topics and (not hit or hit.purpose == "answer") and not route:
        prior_offers = products
        hit = await asyncio.to_thread(answer_facts, index, fact_question, topics, products)
        if len(products) == 1 and index.is_file():
            products = hit.products
        policy = route_action(history, fact_question, products, action_type=action_type, finding=finding)
        if policy and not policy.offer_help:
            route = policy
        steps.append(
            StepTrace(
                tool="product_facts",
                args={
                    "topics": sorted(topics),
                    "offers": [p.offerId for p in prior_offers],
                    "question": fact_question,
                    "replaces_products": prior_offers != products,
                },
                source_ref=hit.evidence.source_ref,
                result_summary=hit.text if not hit.sources else f"{hit.text} {hit.sources[0].label}"[:450],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        if hit.purpose == "clarification" and hit.text != OFFER_HELP and not hit.sources and len(products) != 1:
            steps[-1].args["awaiting_product_for"] = fact_question
    elif topics and hit and hit.purpose == "clarification" and hit.text != OFFER_HELP and not route:
        steps[-1].args["awaiting_product_for"] = fact_question
    elif not hit or hit.purpose == "answer":
        if not selected:
            products = await asyncio.to_thread(refresh_products, index, products)
        if not route:
            route = route_action(history, user_text, products, action_type=action_type, finding=finding)
    if route:
        steps.append(
            StepTrace(
                tool="handoff_policy",
                args={"reason": route.reason, "replaces_products": products != original_products},
                result_summary=route.clarification or "Передача оператору",
            )
        )
    if hit or route:
        if hit and not route and "На сколько пользователей" in hit.text:
            steps[-1].args["awaiting_seats"] = True
        for step in steps:
            await emit_step(step)
        escalate = route is not None and not route.clarification
        published_products = products if selected or products != original_products else []
        reply_text = hit.text if hit else ""
        if route:
            reply_text = route.clarification or CONFIRMED
            if escalate and hit and topics:
                if route.reason == "availability":
                    detail = hit.sources[0].label if hit.text == OFFER_HELP and hit.sources else hit.text
                    reply_text = f"{detail} {CONFIRMED}"
                elif route.reason == "price_request":
                    reply_text = f"Цена по запросу. {CONFIRMED}"
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
            text=reply_text,
            purpose=("notice" if escalate else "clarification") if route else hit.purpose,
            products=published_products,
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
