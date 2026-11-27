"""Commercial proposal revisions, document access and line-level manager decisions."""

import asyncio
import re
from datetime import date

from app.agent.quote_calculation import Customer, Item, ProposalRequest, Snapshot, calculate, catalog_lines
from app.agent.quote_pdf import render
from app.config import get_settings
from app.contracts.t04 import (
    ApproveLinePriceDecision,
    ConfirmQuoteAction,
    Document,
    DocumentData,
    HandoffData,
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


async def save_revision(conn, cid, action_id, snapshot):
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
    for record in old:
        h = (await repo.get_handoff(record["id"])).handoff
        await repo.resolve_handoff(h.handoffId, conn=conn)
        h.status = "resolved"
        data.append(HandoffData(handoff=h))
    data.append(QuoteData(quote=q))
    for d in snapshot.details:
        if not d.reason:
            continue
        request = (
            "Согласовать конечную цену за упаковку в рублях"
            if d.reason == "price_request"
            else "Проверить доступность и условия поставки"
        )
        h = await repo.create_handoff(
            cid,
            action_id,
            d.reason,
            f"{request}. КП, версия {q.revision}: {d.product.name}, ID {d.product.offerId}, {d.packages} уп.",
            [d.product.model_dump(mode="json")],
            conn=conn,
        )
        h.quoteLine = QuoteLineRef(quoteId=q.quoteId, revision=q.revision, offerId=d.product.offerId)
        await conn.execute(
            "INSERT INTO quote_handoffs VALUES(?,?,?,?)", (h.handoffId, q.quoteId, q.revision, d.product.offerId)
        )
        data.append(HandoffData(handoff=h))
    if q.state == "ready":
        data.append(
            DocumentData(
                document=Document(
                    documentId=doc_id,
                    quoteId=q.quoteId,
                    revision=q.revision,
                    kind="quote_pdf",
                    url="/api/v1/documents/" + doc_id,
                )
            )
        )
    return await repo.append_events(cid, action_id, [d.model_dump(mode="json") for d in data], conn=conn)


async def from_message(cid: str, text: str):
    """Unambiguous single-SKU purchase or pack count. Mixed intents stay in the normal agent flow."""
    quantity = re.fullmatch(
        r"(?:теперь\s+|(?:измените|поменяйте)\s+(?:количество\s+)?на\s+)?"
        r"([1-9]\d{0,4})\s+упаков(?:ка|ки|ок|ку)[.! ]*",
        text.strip(),
        re.I,
    )
    order = re.fullmatch(
        r"(?:да[, ]+)?(?:бер[её]м|оформите|пришлите|подготовьте|нужно|хочу)\s+"
        r"(?:кп|коммерческое предложение)(?:\s+на\s+([1-9]\d{0,4})\s+упаков(?:ка|ки|ок|ку))?"
        r"\s*[,;]?\s*(?:для\s+)?(.{1,180}?)\s*[,;]\s*ИНН\s*[: ]\s*([0-9]{10})"
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


async def prepare(cid, request: ProposalRequest, customer_message: str | None = None):
    from app.services.turn_service import is_busy

    if is_busy(cid):
        raise ApiError(409, "agent_busy", "Дождитесь ответа на предыдущий вопрос.")
    ids = [i.offerId for i in request.items]
    if len(ids) != len(set(ids)):
        raise invalid_input("Каждую позицию укажите один раз с нужным количеством упаковок.")
    async with get_db().tx() as conn:
        current = await latest(cid)
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
        details = await asyncio.to_thread(
            catalog_lines, s.catalog_index_path, request.items, s.data_dir / "config" / "quote_terms.json"
        )
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
            if old.details == details and old.customer == request.customer:
                return 202, {"conversationId": cid, "actionId": current["action_id"]}
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
        written.extend(await save_revision(conn, cid, action_id, snapshot))
    events.publish(cid, written)
    return 202, {"conversationId": cid, "actionId": action_id}


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
        q = snapshot.quote
        if q.state == "needs_review" or (q.terms.validUntil and q.terms.validUntil < date.today().isoformat()):
            raise ApiError(409, "terms_pending", "Для выпуска документа требуется согласовать действующие условия.")
        if isinstance(action, RequestDocumentAction):
            if action.kind != "quote_pdf":
                if q.state != "confirmed":
                    raise ApiError(409, "confirmation_required", "Подтвердите текущую ревизию КП.")
                raise not_found("Демо-счёт ещё не выпущен.")
            return 200, document(row).model_dump(mode="json")
        if row["confirmed"]:
            entry = await get_db().fetchone(
                "SELECT action_id FROM events WHERE conversation_id=? AND "
                "json_extract(data,'$.quote.revision')=? AND json_extract(data,'$.quote.state')='confirmed'",
                (cid, q.revision),
            )
            return 202, {"conversationId": cid, "actionId": entry[0]}
        action_id = repo.new_id("a")
        q.state = "confirmed"
        await conn.execute(
            "UPDATE quote_revisions SET confirmed=1 WHERE quote_id=? AND revision=?", (q.quoteId, q.revision)
        )
        written = await repo.append_events(cid, action_id, [QuoteData(quote=q).model_dump(mode="json")], conn=conn)
    events.publish(cid, written)
    return 202, {"conversationId": cid, "actionId": action_id}


async def approve_price(handoff, decision: ApproveLinePriceDecision):
    cid = handoff.conversationId
    ref = handoff.quoteLine
    if not ref or ref.quoteId != decision.quoteId or ref.offerId != decision.offerId:
        raise not_found("Строка расчёта не найдена.")
    async with get_db().tx() as conn:
        snapshot = current_quote(await latest(cid), decision.quoteId, decision.revision)
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
        written = await save_revision(conn, cid, action_id, updated)
    events.publish(cid, written)
    return 202, {"conversationId": cid, "actionId": action_id}
