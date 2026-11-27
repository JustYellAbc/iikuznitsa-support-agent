"""Commercial proposal revisions, document access and line-level manager decisions."""

import asyncio
import json
import re
from datetime import date

from app.agent.quote_calculation import (
    Conditions,
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
BUYER_FIELDS = (
    r"((?:ООО|ПАО|АО|АНО|ФГУП|МУП|ГУП)\s+(?:«[^«»\n]{1,150}»|\"[^\"\n]{1,150}\"|[а-яёa-z0-9-]+))"
    r"\s*[,;]\s*ИНН\s*[: ]\s*([0-9]{10})\s*[,; ]+\s*КПП\s*[: ]\s*([0-9]{4}[0-9A-Z]{2}[0-9]{3})"
)
CHANGE_QUOTE = r"(?:измените|поменяйте|пересчитайте|обновите|переделайте)\s+(?:наше\s+)?(?:кп|коммерческое предложение)"
QUANTITY_EDIT = re.compile(
    r"\b(?:вместо|изменим|поменяем|замените|пересчитайте|измените|поменяйте|бер[её]м|возьм[её]м)\b"
    r"|\b(?:теперь|нужно|надо)\s+(?:по\s+|нуж\w*\s+|\d|одн|дв[аеу]|тр[иеё]|четыр|пят|шес|сем|вос|дев|десят)",
    re.I,
)


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


async def check_exceptions(cid, offer_ids):
    rows = await get_db().fetchall(
        "SELECT products FROM handoffs WHERE conversation_id=? AND status='open' "
        "AND reason IN ('tender','special_terms','outside_catalog')",
        (cid,),
    )
    for row in rows:
        products = json.loads(row["products"])
        if not products or any(p["offerId"] in offer_ids for p in products):
            raise ApiError(409, "terms_pending", "Сначала дождитесь решения менеджера по особым условиям этой закупки.")


async def invalidate_exception(conn, handoff):
    if handoff.reason not in ("tender", "special_terms", "outside_catalog"):
        return
    row = await latest(handoff.conversationId)
    if row and (
        not handoff.products
        or {p.offerId for p in handoff.products}.intersection(d.product.offerId for d in read(row).details)
    ):
        await invalidate_revision(conn, handoff.conversationId, row["revision"])


async def apply_deal_conditions(cid, customer, details):
    rows = await get_db().fetchall(
        "SELECT id, products FROM handoffs WHERE conversation_id=? AND status='resolved' "
        "AND reason IN ('tender','special_terms','outside_catalog') ORDER BY updated_at DESC",
        (cid,),
    )
    for line in details:
        for row in rows:
            products = json.loads(row["products"])
            if products and all(p["offerId"] != line.product.offerId for p in products):
                continue
            # A negotiated deal needs its own verified terms; free-text replies do not authorize final documents.
            path = get_settings().data_dir / "private" / "quote_terms" / (row["id"] + ".json")
            terms = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            offer = (
                terms.get("offers", {}).get(line.product.offerId)
                if terms.get("customer") == customer.model_dump()
                else None
            )
            rule = Conditions.model_validate(offer) if offer else None
            line.conditions = rule if rule and rule.validUntil and rule.validUntil >= date.today() else None
            if line.conditions and line.conditions.article:
                line.article = line.conditions.article
            break


async def check_line_revision(handoff):
    if handoff.quoteLine:
        row = await latest(handoff.conversationId)
        if row is None or row["revision"] != handoff.quoteLine.revision or row["reselect"]:
            raise ApiError(409, "stale_revision", STALE)


async def save_revision(conn, cid, action_id, snapshot, *, resolved=None, selection=False):
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
    buyer_events = [quote_event]
    if selection:
        products = [d.product for d in snapshot.details]
        if [p.model_dump(mode="json") for p in products] != await repo.last_products(cid):
            buyer_events.append(ProductsData(products=products))
    if (await repo.get_conversation(cid)).session.mode == "pilot":
        await repo.create_draft(cid, action_id, [e.model_dump(mode="json") for e in buyer_events], conn=conn)
        data.append(
            MessageData(
                author="agent",
                purpose="notice",
                text="КП подготовлено. Менеджер проверит его перед отправкой.",
                sources=[],
            )
        )
    else:
        data.extend(buyer_events)
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


async def invalidate_revision(conn, cid, revision):
    await conn.execute("UPDATE quote_revisions SET reselect=1 WHERE conversation_id=? AND revision=?", (cid, revision))
    await conn.execute(
        "DELETE FROM drafts WHERE conversation_id=? AND status='pending' AND action_id="
        "(SELECT action_id FROM quote_revisions WHERE conversation_id=? AND revision=?)",
        (cid, cid, revision),
    )


async def reset_selection(conn, cid, revision):
    await invalidate_revision(conn, cid, revision)
    await conn.execute(
        "INSERT INTO quote_selection_resets(conversation_id,after_seq) "
        "SELECT ?,COALESCE(MAX(seq),0) FROM events WHERE conversation_id=? "
        "ON CONFLICT(conversation_id) DO UPDATE SET after_seq=excluded.after_seq",
        (cid, cid),
    )


async def selectable_products(cid):
    rows = await get_db().fetchall(
        """SELECT json_extract(p.value,'$.offerId') AS id, json_extract(p.value,'$.name') AS name
           FROM events e, json_each(e.data, CASE json_extract(e.data,'$.type')
               WHEN 'products' THEN '$.products' WHEN 'quote' THEN '$.quote.lines' END) p
           WHERE e.conversation_id=? AND json_extract(e.data,'$.type') IN ('products','quote')
             AND e.seq > COALESCE((SELECT after_seq FROM quote_selection_resets WHERE conversation_id=?),0)
           ORDER BY e.seq,p.key""",
        (cid, cid),
    )
    return {row["id"]: row["name"] for row in rows}


async def invalidate_changed_selection(conn, cid):
    """A published new selection cannot revive an earlier acceptance, even after A -> B -> A."""
    row = await get_db().fetchone(
        "SELECT revision,snapshot,reselect FROM quote_revisions WHERE conversation_id=? ORDER BY revision DESC LIMIT 1",
        (cid,),
    )
    if row and not row["reselect"]:
        quote = Snapshot.model_validate_json(row["snapshot"]).quote
        selected = {p["offerId"] for p in await repo.last_products(cid)}
        if selected != {line.offerId for line in quote.lines}:
            await invalidate_revision(conn, cid, row["revision"])


async def clarification(conn, cid, text, reply):
    action_id = repo.new_id("a")
    messages = [
        MessageData(author="customer", purpose="input", text=text, sources=[]),
        MessageData(author="agent", purpose="clarification", text=reply, sources=[]),
    ]
    written = await repo.append_events(cid, action_id, [m.model_dump(mode="json") for m in messages], conn=conn)
    result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
    return result, written


async def clarify_quantity(cid, current, text):
    async with get_db().tx() as conn:
        await require_published(current)
        await invalidate_revision(conn, cid, current["revision"])
        result, written = await clarification(
            conn,
            cid,
            text,
            "Уточните новые позиции и количество. Откройте «Изменить КП», выберите строки и укажите число упаковок.",
        )
    events.publish(cid, written)
    return result


async def from_message(cid: str, text: str):
    """Unambiguous single-SKU purchase or pack count. Mixed intents stay in the normal agent flow."""
    seats = re.fullmatch(
        rf"(?:(?:{CHANGE_QUOTE})\s+|теперь\s+|вместо \d+ (?:нужно |на )?)?(?:нужно |нужны |на )?([1-9]\d{{0,4}})\s+"
        r"(?:мест[ао]?|пользовател(?:я|ей|ь))(?:\s+для\s+(?:обоих|всех)\s+(?:продуктов|позиций))?"
        rf"(?:\s+для\s+{BUYER_FIELDS})?[.! ]*",
        text.strip(),
        re.I,
    )
    if seats and (current := await latest(cid)):
        old = read(current)
        req = ProposalRequest(
            customer=Customer(company=seats[2], inn=seats[3], kpp=seats[4].upper()) if seats[2] else old.customer,
            items=[Item(offerId=d.product.offerId, packages=d.packages) for d in old.details],
            baseRevision=old.quote.revision,
        )
        return await prepare(cid, req, customer_message=text, seats=int(seats[1]))
    quantity = re.fullmatch(
        r"(?:теперь\s+|(?:измените|поменяйте)\s+(?:количество\s+)?на\s+)?"
        r"(?:по\s+)?([1-9]\d{0,5})\s+упаков(?:ка|ки|ок|ку)"
        r"(?:\s+(?:для\s+)?(кажд(?:ой|ого)|обеих|обоих|всех)"
        r"(?:\s+(?:позиций|позиции|товаров|товара|строк|строки))?)?[.! ]*",
        text.strip(),
        re.I,
    )
    order = re.fullmatch(
        r"(?:да[, ]+)?(?:бер[её]м|оформите|пришлите|подготовьте|нужно|хочу)\s+"
        r"(?:кп|коммерческое предложение)(?:\s+на\s+([1-9]\d{0,4})\s+упаков(?:ка|ки|ок|ку))?"
        rf"\s*[,;]?\s*(?:для\s+)?{BUYER_FIELDS}[.! ]*",
        text.strip(),
        re.I,
    )
    if not (quantity or order):
        if re.match(CHANGE_QUOTE + r"\b", text.strip(), re.I):
            # Unparsed explicit change goes through normal product discovery, but old terms are no longer actionable.
            async with get_db().tx() as conn:
                if current := await latest(cid):
                    await reset_selection(conn, cid, current["revision"])
        elif (
            QUANTITY_EDIT.search(text)
            and re.search(r"\b(?:упаков\w*|пач\w*|количеств\w*|мест[ао]?|пользовател\w*|штук\w*|шт)\b", text, re.I)
            and (current := await latest(cid))
        ):
            return await clarify_quantity(cid, current, text)
        return None
    products = await repo.last_products(cid)
    current = await latest(cid)
    if order:
        if len(products) != 1:
            return None
        packs, company, inn, kpp = order.groups()
        customer = Customer(company=company, inn=inn, kpp=kpp.upper())
        count = int(packs or 1)
    else:
        if not current:
            return None
        old = read(current)
        if (
            current["reselect"]
            or {d.product.offerId for d in old.details} != {p["offerId"] for p in products}
            or len(old.details) > 1
            and not quantity[2]
            or int(quantity[1]) > 100000
        ):
            return await clarify_quantity(cid, current, text)
        return await prepare(
            cid,
            ProposalRequest(
                customer=old.customer,
                items=[Item(offerId=d.product.offerId, packages=int(quantity[1])) for d in old.details],
                baseRevision=old.quote.revision,
            ),
            customer_message=text,
        )
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
    await check_exceptions(cid, ids)
    if len(ids) != len(set(ids)):
        raise invalid_input("Каждую позицию укажите один раз с нужным количеством упаковок.")
    current = await latest(cid)
    if current and not current["reselect"]:
        await require_published(current)
    revision = current["revision"] if current else 0
    if request.baseRevision != revision:
        raise ApiError(409, "stale_revision", STALE)
    # Seat changes resolve the old lines against the catalogue. Other inputs must come from the current selection.
    if seats is None and set(ids).difference(await selectable_products(cid)):
        raise invalid_input("Сначала подберите эти позиции в чате под текущие условия.")
    s = get_settings()
    try:
        details = await asyncio.to_thread(
            catalog_lines, s.catalog_index_path, request.items, s.data_dir / "config" / "quote_terms.json", seats
        )
    except NoSeatVariant:
        await reset_selection(conn, cid, revision)
        return await clarification(
            conn,
            cid,
            customer_message,
            f"На {seats} мест нужно заново подобрать варианты. "
            "Уточните товар, затем подтвердите новые позиции в форме КП.",
        )
    except ApiError as exc:
        if not customer_message or not current or exc.code != "invalid_input":
            raise
        await invalidate_revision(conn, cid, revision)
        return await clarification(
            conn, cid, customer_message, f"{exc.message} Пересмотрите позиции в форме «Изменить КП»."
        )
    await apply_deal_conditions(cid, request.customer, details)
    old = read(current) if current else None
    if old:
        market_fields = {"product", "packages", "article", "conditions", "feedDate"}
        if (
            old.customer == request.customer
            and [d.model_dump(include=market_fields) for d in old.details]
            == [d.model_dump(include=market_fields) for d in details]
            and not current["reselect"]
        ):
            for fresh, prior in zip(details, old.details, strict=True):
                if prior.decision:
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
    written.extend(await save_revision(conn, cid, action_id, snapshot, selection=True))
    return await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id}), written


def current_quote(row, qid, revision):
    if row is None or row["quote_id"] != qid:
        raise not_found(NO_QUOTE)
    if row["revision"] != revision:
        raise ApiError(409, "stale_revision", STALE)
    return read(row)


async def actionable_quote(cid, qid, revision):
    from app.services.turn_service import is_busy

    if is_busy(cid):
        raise ApiError(409, "agent_busy", "Дождитесь проверки новых условий в чате.")
    row = await latest(cid)
    snapshot = current_quote(row, qid, revision)
    await require_published(row)
    await check_exceptions(cid, [d.product.offerId for d in snapshot.details])
    if row["reselect"]:
        raise ApiError(409, "stale_revision", "Покупатель изменил потребность. Требуется актуальный подбор.")
    q = snapshot.quote
    if q.state == "needs_review" or (q.terms.validUntil and q.terms.validUntil < date.today().isoformat()):
        raise ApiError(409, "terms_pending", "Для выпуска документа требуется согласовать действующие условия.")
    return row, snapshot


async def quote_action(cid, action: ConfirmQuoteAction | RequestDocumentAction):
    async with get_db().tx() as conn:
        row, snapshot = await actionable_quote(cid, action.quoteId, action.revision)
        q = snapshot.quote
        if isinstance(action, RequestDocumentAction):
            if action.kind != "quote_pdf":
                raise invalid_input("Ожидался запрос PDF коммерческого предложения.")
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
        await check_exceptions(cid, [d.product.offerId for d in snapshot.details])
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
