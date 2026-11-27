"""Шесть операций контракта T04 (/api/v1) и вход менеджера."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.agent.quote_calculation import ProposalRequest
from app.contracts.schemas import ManagerLogin
from app.contracts.t04 import (
    ID_PATTERN,
    ConversationRef,
    CustomerAction,
    Error,
    EventPage,
    HandoffList,
    ManagerDecision,
    MessageAction,
    Receipt,
)
from app.core import auth, events, prefilter
from app.core.auth import Principal, require_manager, require_principal
from app.core.errors import ApiError, not_found
from app.db import repository
from app.db.database import get_db
from app.services import conversation_service as svc
from app.services import quotes

router = APIRouter(prefix="/api/v1")

PathId = Annotated[str, Path(min_length=1, max_length=128, pattern=ID_PATTERN)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=ID_PATTERN)]

ERRORS = {
    401: {"model": Error, "description": "Нужна сессия"},
    403: {"model": Error, "description": "Требуется другая роль"},
    404: {"model": Error, "description": "Объект отсутствует или вне области доступа"},
    409: {"model": Error, "description": "agent_busy, key_conflict, already_resolved"},
    422: {"model": Error, "description": "invalid_input"},
    429: {"model": Error, "description": "rate_limited (+ Retry-After)"},
}


def _json(result: tuple[int, dict]) -> JSONResponse:
    status, payload = result
    return JSONResponse(payload, status_code=status)


@router.post(
    "/conversations",
    tags=["T04"],
    operation_id="createConversation",
    status_code=201,
    response_model=ConversationRef,
    responses=ERRORS,
)
async def create_conversation(request: Request, key: IdempotencyKey) -> JSONResponse:
    """Покупатель открывает диалог. Без cookie сервер выдаёт сессию покупателя (Set-Cookie support_session)."""
    principal = await auth.current_principal(request)
    if principal is not None and principal.role != "customer":
        raise ApiError(403, "forbidden", "Диалог открывает покупатель.")
    token = None
    if principal is None:
        principal, token = await auth.create_session("customer", repository.new_id("u"))
    result = await svc.idempotent(
        principal, "POST", request.url.path, key, None, lambda: svc.create_conversation(principal)
    )
    response = _json(result)
    if token is not None:
        name = auth.BUYER_COOKIE if auth.buyer_view(request) else auth.COOKIE
        auth.set_session_cookie(request, response, token, "customer", name=name)
    return response


@router.post(
    "/conversations/{conversationId}/actions",
    tags=["T04"],
    operation_id="submitAction",
    status_code=202,
    response_model=Receipt,
    responses=ERRORS,
)
async def submit_action(
    request: Request,
    conversationId: PathId,
    key: IdempotencyKey,
    action: Annotated[CustomerAction, Body()],
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Реплика, вызов оператора, подтверждение расчёта или запрос документа. Результат — в журнале."""
    if principal.role != "customer":
        raise ApiError(403, "forbidden", "Команда доступна покупателю.")
    if isinstance(action, MessageAction):
        # До Idempotency-Key, журнала и очереди хода: отказ ничего не занимает и не пишет.
        prefilter.check_message_length(action.text)
    body = action.model_dump(mode="json")
    return _json(
        await svc.idempotent(
            principal, "POST", request.url.path, key, body, lambda: svc.submit_action(principal, conversationId, action)
        )
    )


@router.get(
    "/conversations/{conversationId}/events",
    tags=["T04"],
    operation_id="readEvents",
    response_model=EventPage,
    responses=ERRORS,
)
async def read_events(
    conversationId: PathId,
    after: Annotated[int, Query(ge=0)] = 0,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """До 100 событий после `after`. При hasMore=true сразу читать следующую страницу с nextAfter."""
    return JSONResponse(await svc.read_events(principal, conversationId, after))


@router.get(
    "/conversations/{conversationId}/events/stream",
    tags=["T05 расширения"],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE: `event: event`, data — Event"}},
)
async def stream_events(
    conversationId: PathId,
    after: Annotated[int, Query(ge=0)] = 0,
    principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    """Расширение T05: те же события журнала потоком SSE (id = seq). Основной способ — опрос readEvents."""
    await svc.conversation_for(principal, conversationId)
    return StreamingResponse(
        events.stream(conversationId, after, repository.list_events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/manager/handoffs", tags=["T04"], operation_id="listHandoffs", response_model=HandoffList, responses=ERRORS
)
async def list_handoffs(principal: Principal = Depends(require_manager)) -> JSONResponse:
    """Очередь открытых обращений, новые первыми."""
    return JSONResponse(await svc.list_handoffs())


@router.post(
    "/manager/handoffs/{handoffId}/resolution",
    tags=["T04"],
    operation_id="resolveHandoff",
    status_code=202,
    response_model=Receipt,
    responses=ERRORS,
)
async def resolve_handoff(
    request: Request,
    handoffId: PathId,
    key: IdempotencyKey,
    decision: Annotated[ManagerDecision, Body()],
    principal: Principal = Depends(require_manager),
) -> JSONResponse:
    """reply — вопрос в открытом обращении, close — итоговый ответ и закрытие."""
    body = decision.model_dump(mode="json")
    return _json(
        await svc.idempotent(
            principal, "POST", request.url.path, key, body, lambda: svc.resolve_handoff(handoffId, decision)
        )
    )


@router.get(
    "/documents/{documentId}",
    tags=["T04"],
    operation_id="downloadDocument",
    responses={200: {"content": {"application/pdf": {}}}, **ERRORS},
)
async def download_document(documentId: PathId, principal: Principal = Depends(require_principal)) -> Response:
    if documentId.startswith("inv_"):
        async with get_db().committed_read():
            invoice = await get_db().fetchone(
                "SELECT q.conversation_id, i.quote_id, i.revision FROM demo_invoices i "
                "JOIN quote_revisions q USING(quote_id,revision) WHERE i.document_id=? AND i.pdf IS NOT NULL",
                (documentId,),
            )
            if invoice is None:
                raise not_found("Документ не найден.")
            await svc.conversation_for(principal, invoice["conversation_id"])
            pdf = await get_db().fetchval("SELECT pdf FROM demo_invoices WHERE document_id=?", (documentId,))
        filename = f"Softline-demo-invoice-{documentId}-v{invoice['revision']}.pdf"
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    row = await get_db().fetchone(
        "SELECT conversation_id, quote_id, revision FROM quote_revisions "
        "WHERE document_id=? AND issued_action IS NOT NULL",
        (documentId,),
    )
    if row is None:
        raise not_found("Документ не найден.")
    return await quote_preview(row["conversation_id"], row["quote_id"], row["revision"], principal)


@router.get("/conversations/{conversationId}/quotes/{quoteId}/{revision}/preview.pdf", tags=["КП"])
async def quote_preview(
    conversationId: PathId,
    quoteId: PathId,
    revision: Annotated[int, Path(ge=1)],
    principal: Principal = Depends(require_principal),
) -> Response:
    await svc.conversation_for(principal, conversationId)
    async with get_db().committed_read():
        if principal.role == "customer" and not await quotes.published(conversationId, quoteId, revision):
            raise not_found("Документ не найден.")
        row = await get_db().fetchone(
            "SELECT pdf, issued_action, json_extract(snapshot,'$.quote.state') AS state FROM quote_revisions "
            "WHERE conversation_id=? AND quote_id=? AND revision=?",
            (conversationId, quoteId, revision),
        )
    if row is None or (principal.role == "customer" and row["state"] == "ready" and not row["issued_action"]):
        raise not_found("Документ не найден.")
    return Response(
        row["pdf"],
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="Softline-{quoteId}-v{revision}.pdf"',
        },
    )


@router.post("/conversations/{conversationId}/quotes", status_code=202, response_model=Receipt, tags=["КП"])
async def prepare_quote(
    request: Request,
    conversationId: PathId,
    body: ProposalRequest,
    key: IdempotencyKey,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    if principal.role != "customer":
        raise ApiError(403, "forbidden", "Выбор подтверждает покупатель.")
    await svc.conversation_for(principal, conversationId)
    return _json(
        await svc.idempotent(
            principal,
            "POST",
            request.url.path,
            key,
            body.model_dump(mode="json"),
            lambda: quotes.prepare(conversationId, body),
        )
    )


@router.get("/conversations/{conversationId}/quotes/current", tags=["КП"])
async def quote_inputs(conversationId: PathId, principal: Principal = Depends(require_principal)) -> JSONResponse:
    await svc.conversation_for(principal, conversationId)
    async with get_db().committed_read():
        row = await quotes.latest(conversationId)
        if principal.role == "customer" and row and not row["reselect"]:
            await quotes.require_published(row)
        choices = await quotes.selectable_products(conversationId)
    snapshot = quotes.read(row) if row else None
    reselect = bool(row and row["reselect"])
    return JSONResponse(
        {
            "revision": snapshot.quote.revision if snapshot else 0,
            "requiresReselection": reselect,
            "choices": [{"offerId": offer_id, "name": name} for offer_id, name in choices.items()],
            "customer": snapshot.customer.model_dump() if snapshot else None,
            "items": [{"offerId": d.product.offerId, "packages": d.packages} for d in snapshot.details]
            if snapshot and not reselect
            else [],
        },
        headers={"Cache-Control": "private, no-store"},
    )


# --- Вход менеджера ------------------------------------------------------------------------


@router.post("/auth/manager/login", tags=["T05 расширения"], responses={401: {"model": Error}})
async def manager_login(request: Request, body: ManagerLogin) -> JSONResponse:
    """Выдаёт cookie support_session с ролью manager. Неверные данные — 401 без сессии."""
    if not auth.check_manager_credentials(body.username, body.password):
        raise ApiError(401, "unauthenticated", "Неверный логин или пароль.")
    principal, token = await auth.create_session("manager", f"manager:{body.username}")
    response = JSONResponse({"role": principal.role})
    await auth.preserve_legacy_buyer(request, response)
    auth.set_session_cookie(request, response, token, "manager")
    return response


@router.post("/auth/logout", tags=["T05 расширения"], status_code=204)
async def logout(request: Request) -> Response:
    response = Response(status_code=204)
    await auth.drop_session(request, response)
    return response
