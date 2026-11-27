"""Issue a single demo invoice per accepted revision, using its saved commercial terms."""

import asyncio
import logging

from app.agent import quote_pdf
from app.contracts.t04 import Document, DocumentData, Error, FailedActionData, RequestDocumentAction
from app.core import events
from app.core.errors import ApiError, not_found
from app.db import repository as repo
from app.db.database import get_db
from app.services import price_policy, quotes

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def document(qid, revision, doc_id):
    return Document(
        documentId=doc_id, quoteId=qid, revision=revision, kind="demo_invoice", url="/api/v1/documents/" + doc_id
    )


async def confirmed_snapshot(cid, owner, qid, revision):
    conversation = await repo.get_conversation(cid)
    if conversation is None or conversation.owner_id != owner:
        raise not_found("Диалог не найден.")
    _, snapshot = await quotes.actionable_quote(cid, qid, revision)
    if snapshot.quote.state != "confirmed":
        raise ApiError(409, "confirmation_required", "Подтвердите текущую ревизию КП.")
    selected = {p["offerId"] for p in await repo.last_products(cid)}
    if selected != {line.offerId for line in snapshot.quote.lines}:
        raise ApiError(409, "stale_revision", quotes.STALE)
    if (
        any(not d.product.available for d in snapshot.details)
        and not price_policy.active_policy().rule.allow_auto_invoice
    ):
        raise ApiError(409, "terms_pending", "Для счёта нужно согласовать поставку.")
    return snapshot


async def request(cid: str, owner: str, action: RequestDocumentAction):
    qid, revision = action.quoteId, action.revision
    async with get_db().tx() as conn:
        await confirmed_snapshot(cid, owner, qid, revision)
        saved = await get_db().fetchone(
            "SELECT action_id, document_id, pdf IS NOT NULL AS issued FROM demo_invoices "
            "WHERE quote_id=? AND revision=?",
            (qid, revision),
        )
        if saved and saved["issued"]:
            doc = document(qid, revision, saved["document_id"])
            return await repo.record_quote_response(conn, 200, doc.model_dump(mode="json"))
        if saved:
            return await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": saved["action_id"]})
        doc_id, action_id = repo.new_id("inv"), repo.new_id("a")
        await conn.execute(
            "INSERT INTO demo_invoices(quote_id,revision,document_id,action_id,owner_id) VALUES(?,?,?,?,?)",
            (qid, revision, doc_id, action_id, owner),
        )
        result = await repo.record_quote_response(conn, 202, {"conversationId": cid, "actionId": action_id})
    start(doc_id)
    return result


def start(doc_id):
    task = asyncio.create_task(issue(doc_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_idle():
    if _tasks:
        await asyncio.gather(*tuple(_tasks))


async def resume_pending():
    for row in await get_db().fetchall("SELECT document_id FROM demo_invoices WHERE pdf IS NULL"):
        start(row["document_id"])


async def issue(doc_id):
    db = get_db()
    async with db.committed_read():
        row = await db.fetchone(
            "SELECT q.conversation_id, i.owner_id, i.quote_id, i.revision, i.action_id, q.snapshot, q.confirmed "
            "FROM demo_invoices i JOIN quote_revisions q USING(quote_id,revision) "
            "WHERE i.document_id=? AND i.pdf IS NULL",
            (doc_id,),
        )
    if row is None:
        return
    cid, owner, qid, rev, action_id = (
        row[k] for k in ("conversation_id", "owner_id", "quote_id", "revision", "action_id")
    )
    try:
        blob = await asyncio.to_thread(quote_pdf.render, quotes.read(row), invoice_number=doc_id)
        async with db.tx() as conn:
            await confirmed_snapshot(cid, owner, qid, rev)
            saved = await conn.execute(
                "UPDATE demo_invoices SET pdf=? WHERE document_id=? AND pdf IS NULL", (blob, doc_id)
            )
            if not saved.rowcount:
                return
            event = DocumentData(document=document(qid, rev, doc_id))
            written = await repo.append_events(cid, action_id, [event.model_dump(mode="json")], conn=conn)
    except Exception as exc:
        if isinstance(exc, ApiError):
            error = Error(code=exc.code, message=exc.message)
        else:
            log.exception("Could not issue demo invoice %s", doc_id)
            error = Error(code="terms_pending", message="Не удалось сформировать демо-счёт. Повторите запрос.")
        async with db.tx() as conn:
            removed = await conn.execute("DELETE FROM demo_invoices WHERE document_id=? AND pdf IS NULL", (doc_id,))
            if not removed.rowcount:
                return
            failed = FailedActionData(error=error)
            written = await repo.append_events(cid, action_id, [failed.model_dump(mode="json")], conn=conn)
    events.publish(cid, written)
