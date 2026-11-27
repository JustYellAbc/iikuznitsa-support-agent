"""Commercial proposal revisions, document access and line-level manager decisions."""

import asyncio
import json
import re
from datetime import date

from app.agent.quote_calculation import (
    Customer,
    Item,
    NoSeatVariant,
    ProposalRequest,
    Snapshot,
    calculate,
    catalog_lines,
)
from app.agent.quote_pdf import render
from app.config import get_settings
from app.contracts.t04 import (
    ApproveLinePriceDecision,
    ConfirmQuoteAction,
    Document,
    DocumentData,
    HandoffData,
    MessageData,
    ProductsData,
    QuoteData,
    QuoteLineRef,
    RequestDocumentAction,
)
from app.core import events
from app.core.errors import ApiError, invalid_input, not_found
from app.db import repository as repo
from app.db.database import get_db

STALE = "Условия изменились. Откройте последнюю версию расчёта."
NO_QUOTE = "Расчёт не найден."


async def latest(cid):
    return await get_db().fetchone(
        "SELECT * FROM quote_revisions WHERE conversation_id=? ORDER BY revision DESC LIMIT 1", (cid,)
    )


def read(row) -> Snapshot:
    snapshot = Snapshot.model_validate_json(row["snapshot"])
    if row["confirmed"]:
        snapshot.quote.state = "confirmed"
    return snapshot


def document(row):
    return Document(
        documentId=row["document_id"],
        quoteId=row["quote_id"],
        revision=row["revision"],
        kind="quote_pdf",
        url="/api/v1/documents/" + row["document_id"],
    )


async def published(cid, qid, revision):
    return await get_db().fetchval(
        "SELECT 1 FROM events WHERE conversation_id=? AND "
        "json_extract(data,'$.quote.quoteId')=? AND json_extract(data,'$.quote.revision')=? LIMIT 1",
        (cid, qid, revision),
    )


async def require_published(row):
    if row and not await published(row["conversation_id"], row["quote_id"], row["revision"]):
        raise ApiError(409, "terms_pending", "Ревизию КП проверяет менеджер.")


async def save_revision(conn, cid, action_id, snapshot, *, resolved=None):
    q = snapshot.quote
    blob = await asyncio.to_thread(render, snapshot)
    doc_id = repo.new_id("d")
    await conn.execute(
        "INSERT INTO quote_revisions(conversation_id,quote_id,revision,snapshot,action_id,document_id,pdf) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, q.quoteId, q.revision, snapshot.model_dump_json(), action_id, doc_id, blob),
    )
    data = []
    old = await get_db().fetchall(
        "SELECT h.id FROM handoffs h JOIN quote_handoffs l ON h.id=l.handoff_id WHERE h.status='open' AND l.quote_id=?",
        (q.quoteId,),
    )
    if resolved:
        await repo.resolve_handoff(resolved.handoffId, conn=conn)
        data.append(HandoffData(handoff=resolved.model_copy(update={"status": "resolved"})))
    active = {(d.product.offerId, d.reason) for d in snapshot.details}
    carry = {}
    for record in old:
        h = (await repo.get_handoff(record["id"])).handoff
        if h.status == "open" and (h.quoteLine.offerId, h.reason) in active:
            carry[(h.quoteLine.offerId, h.reason)] = h
    quote_event = QuoteData(quote=q)
    if (await repo.get_conversation(cid)).session.mode == "pilot":
        await repo.create_draft(cid, action_id, [quote_event.model_dump(mode="json")], conn=conn)
        data.append(
            MessageData(
                author="agent",
                purpose="notice",
                text="КП подготовлено. Менеджер проверит его перед отправкой.",
                sources=[],
            )
        )
    else:
        data.append(quote_event)
    for d in snapshot.details:
        if not d.reason:
            continue
        request = (
            "Согласовать конечную цену за упаковку в рублях"
            if d.reason == "price_request"
            else "Проверить доступность и условия поставки"
        )
        question = f"{request}. КП, версия {q.revision}: {d.product.name}, ID {d.product.offerId}, {d.packages} уп."
        h = carry.get((d.product.offerId, d.reason))
        if h:
            h.question = question
            h.products = [d.product]
            await conn.execute(
                "UPDATE handoffs SET question=?,products=?,updated_at=? WHERE id=?",
                (question, json.dumps([d.product.model_dump(mode="json")]), repo.now_iso(), h.handoffId),
            )
        else:
            h = await repo.create_handoff(
                cid, action_id, d.reason, question, [d.product.model_dump(mode="json")], conn=conn
            )
        h.quoteLine = QuoteLineRef(quoteId=q.quoteId, revision=q.revision, offerId=d.product.offerId)
        await conn.execute(
            "INSERT INTO quote_handoffs VALUES(?,?,?,?) "
            "ON CONFLICT(handoff_id) DO UPDATE SET revision=excluded.revision",
            (h.handoffId, q.quoteId, q.revision, d.product.offerId),
        )
        data.append(HandoffData(handoff=h))
    return await repo.append_events(cid, action_id, [d.model_dump(mode="json") for d in data], conn=conn)


async def from_message(cid: str, text: str):
    """Unambiguous single-SKU purchase or pack count. Mixed intents stay in the normal agent flow."""
    seats = re.fullmatch(
        r"(?:теперь\s+|вместо \d+ (?:нужно |на )?)?(?:нужно |нужны |на )?([1-9]\d{0,4})\s+"
        r"(?:мест[ао]?|пользовател(?:я|ей|ь))(?:\s+для\s+(?:обоих|всех)\s+(?:продуктов|позиций))?[.! ]*",
        text.strip(),
        re.I,
    )
    if seats and (current := await latest(cid)):
        old = read(current)
        req = ProposalRequest(
            customer=old.customer,
            items=[Item(offerId=d.product.offerId, packages=d.packages) for d in old.details],
            baseRevision=old.quote.revision,
        )
        return await prepare(cid, req, customer_message=text, seats=int(seats[1]))
    quantity = re.fullmatch(
        r"(?:теперь\s+|(?:измените|поменяйте)\s+(?:количество\s+)?на\s+)?"
        r"([1-9]\d{0,4})\s+упаков(?:ка|ки|ок|ку)[.! ]*",
        text.strip(),
        re.I,
    )
    order = re.fullmatch(
        r"(?:да[, ]+)?(?:бер[её]м|оформите|пришлите|подготовьте|нужно|хочу)\s+"
        r"(?:кп|коммерческое предложение)(?:\s+на\s+([1-9]\d{0,4})\s+упаков(?:ка|ки|ок|ку))?"
        r"\s*[,;]?\s*(?:для\s+)?((?:ООО|ПАО|АО|АНО|ФГУП|МУП|ГУП)\s+"
        r"(?:«[^«»\n]{1,150}»|\"[^\"\n]{1,150}\"|[а-яёa-z0-9-]+))\s*[,;]\s*ИНН\s*[: ]\s*([0-9]{10})"
        r"\s*[,; ]+\s*КПП\s*[: ]\s*([0-9]{4}[0-9A-Z]{2}[0-9]{3})[.! ]*",
        text.strip(),
        re.I,
    )
    if not (quantity or order):
        return None
    products = await repo.last_products(cid)
    if len(products) != 1:
        return None
    current = await latest(cid)
    if order:
        packs, company, inn, kpp = order.groups()
        customer = Customer(company=company, inn=inn, kpp=kpp.upper())
        count = int(packs or 1)
    else:
        if not current:
            return None
        old = read(current)
        if len(old.details) != 1 or old.details[0].product.offerId != products[0]["offerId"]:
            return None
        count = int(quantity[1])
        customer = old.customer
    req = ProposalRequest(
        customer=customer,
        items=[Item(offerId=products[0]["offerId"], packages=count)],
        baseRevision=current["revision"] if current else 0,
    )
    return await prepare(cid, req, customer_message=text)


async def prepare(cid, request: ProposalRequest, customer_message: str | None = None, seats: int | None = None):
    from app.services.turn_service import is_busy

    if is_busy(cid):
        raise ApiError(409, "agent_busy", "Дождитесь ответа на предыдущий вопрос.")
    async with get_db().tx() as conn:
        result, written = await prepare_revision(conn, cid, request, customer_message, seats)
    events.publish(cid, written)
    return result


async def prepare_revision(conn, cid, request, customer_message, seats):
    ids = [i.offerId for i in request.items]
    if len(ids) != len(set(ids)):
        raise invalid_input("Каждую позицию укажите один раз с нужным количеством упаковок.")
    current = await latest(cid)
    await require_published(current)
    revision = current["revision"] if current else 0
    if request.baseRevision != revision:
        raise ApiError(409, "stale_revision", STALE)
    # The buyer explicitly selects from products shown in their own chat; older cards allow multi-item offers.
    rows = await get_db().fetchall(
        "SELECT DISTINCT json_extract(p.value,'$.offerId') AS id FROM events e,"
        " json_each(e.data,'$.products') p WHERE e.conversation_id=? AND json_extract(e.data,'$.type')='products'",
        (cid,),
    )
    if set(ids) - {r["id"] for r in rows}:
        raise invalid_input("Сначала подберите эти позиции в текущем чате.")
    s = get_settings()
    try:
        details = await asyncio.to_thread(
            catalog_lines, s.catalog_index_path, request.items, s.data_dir / "config" / "quote_terms.json", seats
        )
    except NoSeatVariant:
        await conn.execute(
            "UPDATE quote_revisions SET reselect=1 WHERE conversation_id=? AND revision=?", (cid, revision)
        )
        action_id = repo.new_id("a")
        written = await repo.append_events(
            cid,
            action_id,
            [
                MessageData(author="customer", purpose="input", text=customer_message, sources=[]).model_dump(
                    mode="json"
                ),
                MessageData(
                    author="agent",
                    purpose="clarification",
                    text=f"На {seats} мест нужно заново подобрать варианты. "
                    "Уточните товар, затем подтвердите новые позиции в форме КП.",
                    sources=[],
                ).model_dump(mode="json"),
            ],
            conn=conn,
        )
        result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
        return result, written
    old = read(current) if current else None
    if old:
        for fresh in details:
            prior = next((d for d in old.details if d.product.offerId == fresh.product.offerId), None)
            if (
                prior
                and prior.decision
                and prior.product == fresh.product
                and prior.packages == fresh.packages
                and prior.feedDate == fresh.feedDate
                and old.customer == request.customer
            ):
                fresh.unit, fresh.reason, fresh.decision = prior.unit, prior.reason, prior.decision
        if old.details == details and old.customer == request.customer and not current["reselect"]:
            if not customer_message:
                return await repo.record_quote_response(
                    conn, 202, {"conversationId": cid, "actionId": current["action_id"]}
                ), []
            action_id = repo.new_id("a")
            written = await repo.append_events(
                cid,
                action_id,
                [
                    MessageData(author="customer", purpose="input", text=customer_message, sources=[]).model_dump(
                        mode="json"
                    ),
                    MessageData(
                        author="agent", purpose="notice", text=f"Условия совпадают с версией {revision}.", sources=[]
                    ).model_dump(mode="json"),
                ],
                conn=conn,
            )
            result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
            return result, written
    qid = current["quote_id"] if current else repo.new_id("q")
    action_id = repo.new_id("a")
    snapshot = calculate(request.customer, details, qid, revision + 1)
    written = []
    if customer_message:
        written = await repo.append_events(
            cid,
            action_id,
            [
                {
                    "type": "message",
                    "author": "customer",
                    "purpose": "input",
                    "text": customer_message,
                    "sources": [],
                }
            ],
            conn=conn,
        )
    if seats is not None:
        data = ProductsData(products=[d.product for d in details]).model_dump(mode="json")
        written.extend(await repo.append_events(cid, action_id, [data], conn=conn))
    written.extend(await save_revision(conn, cid, action_id, snapshot))
    return await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id}), written


def current_quote(row, qid, revision):
    if row is None or row["quote_id"] != qid:
        raise not_found(NO_QUOTE)
    if row["revision"] != revision:
        raise ApiError(409, "stale_revision", STALE)
    return read(row)


async def quote_action(cid, action: ConfirmQuoteAction | RequestDocumentAction):
    async with get_db().tx() as conn:
        row = await latest(cid)
        snapshot = current_quote(row, action.quoteId, action.revision)
        await require_published(row)
        if row["reselect"]:
            raise ApiError(409, "stale_revision", "Покупатель изменил потребность. Требуется актуальный подбор.")
        q = snapshot.quote
        if q.state == "needs_review" or (q.terms.validUntil and q.terms.validUntil < date.today().isoformat()):
            raise ApiError(409, "terms_pending", "Для выпуска документа требуется согласовать действующие условия.")
        if isinstance(action, RequestDocumentAction):
            if action.kind != "quote_pdf":
                if q.state != "confirmed":
                    raise ApiError(409, "confirmation_required", "Подтвердите текущую ревизию КП.")
                raise not_found("Демо-счёт ещё не выпущен.")
            doc = document(row)
            if row["issued_action"]:
                return await repo.record_quote_response(conn, 200, doc.model_dump(mode="json"))
            action_id = repo.new_id("a")
            await conn.execute(
                "UPDATE quote_revisions SET issued_action=? WHERE quote_id=? AND revision=?",
                (action_id, q.quoteId, q.revision),
            )
            written = await repo.append_events(
                cid, action_id, [DocumentData(document=doc).model_dump(mode="json")], conn=conn
            )
        elif row["confirmed"]:
            entry = await get_db().fetchone(
                "SELECT action_id FROM events WHERE conversation_id=? AND "
                "json_extract(data,'$.quote.revision')=? AND json_extract(data,'$.quote.state')='confirmed'",
                (cid, q.revision),
            )
            return await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": entry[0]})
        else:
            action_id = repo.new_id("a")
            q.state = "confirmed"
            await conn.execute(
                "UPDATE quote_revisions SET confirmed=1 WHERE quote_id=? AND revision=?", (q.quoteId, q.revision)
            )
            written = await repo.append_events(cid, action_id, [QuoteData(quote=q).model_dump(mode="json")], conn=conn)
        await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
    events.publish(cid, written)
    return 202, {"conversationId": cid, "actionId": action_id}


async def approve_price(handoff, decision: ApproveLinePriceDecision):
    cid = handoff.conversationId
    ref = handoff.quoteLine
    if not ref or ref.quoteId != decision.quoteId or ref.offerId != decision.offerId:
        raise not_found("Строка расчёта не найдена.")
    async with get_db().tx() as conn:
        row = await latest(cid)
        snapshot = current_quote(row, decision.quoteId, decision.revision)
        await require_published(row)
        if row["reselect"]:
            raise ApiError(409, "stale_revision", STALE)
        if ref.revision != decision.revision:
            raise ApiError(409, "stale_revision", STALE)
        line = next(d for d in snapshot.details if d.product.offerId == ref.offerId)
        if line.reason != "price_request":
            raise ApiError(409, "terms_pending", "Подтверждение цены не подтверждает доступность.")
        line.unit = decision.unitPriceKopecks
        line.decision = handoff.handoffId
        line.reason = None if line.product.available else "availability"
        updated = calculate(snapshot.customer, snapshot.details, ref.quoteId, ref.revision + 1)
        action_id = repo.new_id("a")
        written = await save_revision(conn, cid, action_id, updated, resolved=handoff)
        await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
    events.publish(cid, written)
    return 202, {"conversationId": cid, "actionId": action_id}
