"""Needs-based recommendation turns share the normal journal, handoff, and pilot publication."""

import asyncio
import re
from dataclasses import asdict
from time import perf_counter

from app.agent.catalog_selection import CODE, LATIN, MDAEMON, result
from app.agent.handoff import CONFIRMED, HUMAN_REQUEST, OFFER_HELP, bare_request, route_action
from app.agent.needs_brief import Brief, initial_need, open_intake, update_brief
from app.agent.needs_catalog import recommend
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest, StepTrace
from app.db.repository import list_turns, new_id


def named_product(text):
    # Platform names are brief fields; an explicit item or other brand starts the named-product flow.
    if CODE.search(text) or MDAEMON.search(text) or re.fullmatch(r"\d{3,12}", text.strip()) or "«" in text:
        return True
    platforms = {"windows", "server", "linux", "postfix", "exchange", "r2", "smtp", "email", "e-mail"}
    return any(word.strip(".-").casefold() not in platforms for match in LATIN.findall(text) for word in match.split())


async def needs_turn(session, history, text, emit_step, *, action_type, finding):
    if action_type != "message" or finding or bare_request(text.casefold()) or named_product(text):
        return None
    start = perf_counter()
    prior = None
    if history and history[-1].role == "agent":
        turns = await list_turns(session.id)
        for turn in reversed(turns):
            if turn.decision != "answer" or not turn.steps:
                break
            step = turn.steps[0]
            if step.tool == "needs_search":
                prior = step.args
            if step.tool != "product_facts":
                break
    if not prior and not (initial_need(text) or open_intake(text)):
        return None
    previous = Brief(**prior["brief"]) if prior else Brief(goal="mail" if initial_need(text) else "discovery")
    body = HUMAN_REQUEST.sub("", text)
    brief = update_brief(previous, body)
    choice = ""
    if prior:
        ordinal = re.search(r"\b(?:бер[её]м|выбира\w*|покажи\w*)?\s*(перв\w*|втор\w*|трет\w*)\s+вариант", body, re.I)
        if ordinal:
            idx = ("перв", "втор", "трет").index(ordinal[1][:4].casefold())
            choices = prior.get("choices", [])
            if idx >= len(choices):
                return None
            choice = choices[idx]
        elif previous == brief and not initial_need(body) and brief.goal != "discovery":
            return None

    index = get_settings().data_dir / "private" / "catalog.sqlite3"
    if prior and brief.goal == "discovery":
        hit = result(OFFER_HELP)
    else:
        hit = await asyncio.to_thread(recommend, index, brief, choice)
    route = route_action(history, text, [], action_type=action_type, finding=finding)
    elapsed = int((perf_counter() - start) * 1000)
    args = {
        "brief": asdict(brief),
        "choices": hit.evidence.data if hit.purpose == "answer" else [],
        "replaces_products": True,
    }
    step = StepTrace(
        tool="needs_search",
        args=args,
        source_ref=hit.evidence.source_ref,
        result_summary=hit.text[:450],
        latency_ms=elapsed,
    )
    steps = [step]
    escalate = bool(route and not route.clarification)
    if escalate:
        steps.append(StepTrace(tool="handoff_policy", args={"reason": route.reason}, result_summary=CONFIRMED))
    for step in steps:
        await emit_step(step)
    return AgentReply(
        turn=AgentTurn(
            turn_id=new_id("turn"),
            session_id=session.id,
            steps=steps,
            decision="escalate" if escalate else "answer",
            escalation_reason=route.reason if escalate else None,
            total_cost_rub=0,
            total_latency_ms=elapsed,
        ),
        text=CONFIRMED if escalate else hit.text,
        purpose="notice" if escalate else hit.purpose,
        products=hit.products,
        sources=hit.sources,
        handoff=HandoffRequest(reason=route.reason, question=route.question, products=hit.products)
        if escalate
        else None,
    )
