"""Buyer-requested review of the commercial terms of one complete, priced proposal."""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints

from app.agent.quote_calculation import Conditions, Inputs, calculate, clean
from app.contracts.t04 import HandoffData, Id, MessageData
from app.core import events
from app.core.errors import ApiError, invalid_input, not_found
from app.db import repository as repo
from app.db.database import get_db
from app.services import quotes

RequiredText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ReviewedLine(Inputs):
    article: RequiredText
    vatRate: Annotated[Decimal, Field(ge=0, le=100)]


class ReviewDecision(Inputs):
    revision: Annotated[int, Field(strict=True, ge=1)]
    payment: RequiredText
    delivery: RequiredText
    validUntil: date
    lines: Annotated[dict[Id, ReviewedLine], Field(min_length=1, max_length=20)]


async def link(hid):
    return await get_db().fetchone("SELECT * FROM quote_terms_reviews WHERE handoff_id=?", (hid,))


async def state(cid, qid, revision):
    row = await quotes.latest(cid)
    if not row or row["quote_id"] != qid or row["revision"] != revision or row["reselect"]:
        return "inactive"
    await quotes.require_published(row)
    snapshot = quotes.read(row)
    if snapshot.quote.state != "needs_review" or any(d.reason or not d.unit for d in snapshot.details):
        return "inactive"
    existing = await get_db().fetchone(
        "SELECT h.status FROM quote_terms_reviews r JOIN handoffs h ON h.id=r.handoff_id "
        "WHERE r.quote_id=? AND r.revision=?",
        (qid, revision),
    )
    if existing:
        return "sent" if existing["status"] == "open" else "closed"
    return "available"


def ensure_idle(cid):
    from app.services.turn_service import is_busy

    if is_busy(cid):
        raise ApiError(409, "agent_busy", "Дождитесь проверки новых условий в чате.")


async def request_review(cid, qid, revision):
    async with get_db().tx() as conn:
        ensure_idle(cid)
        row = await quotes.latest(cid)
        snapshot = quotes.current_quote(row, qid, revision)
        await quotes.require_published(row)
        if row["reselect"] or snapshot.quote.state != "needs_review":
            raise ApiError(409, "stale_revision", quotes.STALE)
        if any(d.reason or not d.unit for d in snapshot.details):
            raise ApiError(409, "terms_pending", "Сначала менеджер должен подтвердить позиции и цены.")
        existing = await get_db().fetchone(
            "SELECT h.action_id FROM quote_terms_reviews r JOIN handoffs h ON h.id=r.handoff_id "
            "WHERE r.quote_id=? AND r.revision=?",
            (qid, revision),
        )
        if existing:
            return await repo.record_quote_response(
                conn, 202, {"conversationId": cid, "actionId": existing["action_id"]}
            )
        labels = {"payment": "оплата", "delivery": "поставка", "taxNote": "НДС", "validUntil": "срок действия"}
        missing = [label for key, label in labels.items() if not getattr(snapshot.quote.terms, key)]
        if any(not d.article for d in snapshot.details):
            missing.append("артикулы")
        action = repo.new_id("a")
        question = f"Проверить КП, версия {revision}. Требуют согласования: {', '.join(missing)}."
        handoff = await repo.create_handoff(
            cid,
            action,
            "insufficient_evidence",
            question,
            [d.product.model_dump(mode="json") for d in snapshot.details],
            conn=conn,
        )
        await conn.execute("INSERT INTO quote_terms_reviews VALUES(?,?,?)", (handoff.handoffId, qid, revision))
        notice = MessageData(
            author="agent", purpose="notice", text="КП отправлено менеджеру. Ответ придёт в этот чат.", sources=[]
        )
        written = await repo.append_events(
            cid,
            action,
            [HandoffData(handoff=handoff).model_dump(mode="json"), notice.model_dump(mode="json")],
            conn=conn,
        )
        result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action})
    events.publish(cid, written)
    return result


async def details(hid):
    ref = await link(hid)
    if not ref:
        return None
    found = await repo.get_handoff(hid)
    row = await quotes.latest(found.handoff.conversationId)
    if found.handoff.status != "open" or row["reselect"] or row["revision"] != ref["revision"]:
        return None
    snapshot = quotes.read(row)
    return {
        "revision": ref["revision"],
        "terms": snapshot.quote.terms.model_dump(),
        "lines": [
            {
                "offerId": d.product.offerId,
                "name": d.product.name,
                "article": d.article,
                "vatRate": d.conditions.vatRate if d.conditions else None,
            }
            for d in snapshot.details
        ],
    }


async def approve(hid, decision):
    async with get_db().tx() as conn:
        ref = await link(hid)
        if not ref:
            raise not_found("Запрос согласования КП не найден.")
        case = (await repo.get_handoff(hid)).handoff
        cid = case.conversationId
        ensure_idle(cid)
        row = await quotes.latest(cid)
        snapshot = quotes.current_quote(row, ref["quote_id"], decision.revision)
        if case.status != "open" or ref["revision"] != decision.revision or row["reselect"]:
            raise ApiError(409, "stale_revision", quotes.STALE)
        await quotes.require_published(row)
        await quotes.check_exceptions(cid, [d.product.offerId for d in snapshot.details])
        if decision.validUntil < date.today() or set(decision.lines) != {d.product.offerId for d in snapshot.details}:
            raise invalid_input("Проверьте срок действия и условия каждой позиции.")
        if not clean(decision.payment) or not clean(decision.delivery):
            raise invalid_input("Подтвердите условия оплаты и поставки.")
        for d in snapshot.details:
            line = decision.lines[d.product.offerId]
            if not clean(line.article):
                raise invalid_input("Укажите артикул каждой позиции.")
            d.article = line.article
            d.conditions = Conditions(
                source="manager:" + hid,
                article=line.article,
                payment=decision.payment,
                delivery=decision.delivery,
                validUntil=decision.validUntil,
                vatRate=line.vatRate,
            )
        updated = calculate(snapshot.customer, snapshot.details, ref["quote_id"], decision.revision + 1)
        if updated.quote.state != "ready":
            raise ApiError(409, "terms_pending", "Остались неподтверждённые условия КП.")
        action = repo.new_id("a")
        written = await quotes.save_revision(conn, cid, action, updated, resolved=case)
        result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action})
    events.publish(cid, written)
    return result
