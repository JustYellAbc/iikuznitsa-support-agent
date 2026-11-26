import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, StreamingResponse

from app.config import get_settings
from app.contracts.schemas import Health, Message, MessageCreate, Session, SessionCreate, SessionState
from app.core import events
from app.core.llm import today_spend_rub
from app.db import repository
from app.services import turn_service

router = APIRouter()

Id = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
MAX_CONTEXT_BYTES = 4096


async def _session_or_404(session_id: str) -> Session:
    session = await repository.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    return session


@router.get("/health", response_model=Health, tags=["public"])
async def health() -> Health:
    return Health(
        status="ok",
        offers_count=await repository.count_offers(),
        kb_count=await repository.count_kb_articles(),
        today_spend_rub=round(await today_spend_rub(), 4),
    )


@router.post("/api/sessions", response_model=Session, status_code=status.HTTP_201_CREATED, tags=["public"])
async def create_session(body: SessionCreate) -> Session:
    if len(json.dumps(body.client_context, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "client_context слишком большой")
    return await repository.create_session(body.client_context, await turn_service.default_mode())


@router.get("/api/sessions/{session_id}", response_model=SessionState, tags=["public"])
async def get_session(session_id: Id) -> SessionState:
    session = await _session_or_404(session_id)
    open_tickets = [t for t in await repository.list_tickets(session_id=session_id) if t.status != "resolved"]
    return SessionState(
        session=session,
        messages=await repository.list_messages(session_id),
        turns=await repository.list_turns(session_id),
        open_tickets=[
            t.model_copy(update={"summary": "", "collected_params": {}, "draft_answer": None}) for t in open_tickets
        ],
        busy=turn_service.is_busy(session_id),
    )


@router.post(
    "/api/sessions/{session_id}/messages",
    response_model=Message,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["public"],
    responses={
        409: {"description": "Агент ещё отвечает на предыдущую реплику"},
        429: {"description": "Превышен лимит ходов или частоты запросов"},
    },
)
async def post_message(body: MessageCreate, session_id: Id) -> Message:
    """Сохраняет реплику клиента и запускает ход агента; результат приходит в SSE /events."""
    s = get_settings()
    session = await _session_or_404(session_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пустое сообщение")
    if len(text) > s.max_message_chars:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Сообщение длиннее {s.max_message_chars} символов")
    if turn_service.is_busy(session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Агент ещё отвечает на предыдущее сообщение")
    if await repository.count_client_messages(session_id) >= s.max_turns_per_session:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Достигнут лимит сообщений в диалоге — начните новый или дождитесь менеджера",
        )
    msg = await repository.add_message(session_id, "client", text)
    turn_service.start_turn(session, msg)
    return msg


@router.get(
    "/api/sessions/{session_id}/events",
    tags=["public"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE: step_trace, message_done, ticket_created, operator_reply (см. SseEvent)",
        }
    },
)
async def session_events(session_id: Id) -> StreamingResponse:
    await _session_or_404(session_id)
    return StreamingResponse(
        events.stream(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/api/quotes/{quote_id}/pdf",
    tags=["public"],
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def quote_pdf(quote_id: Id) -> FileResponse:
    found = await repository.get_quote(quote_id)
    if found is None or not found[1]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "КП не найдено")
    quote, pdf_path = found
    path = Path(pdf_path)
    quotes_dir = get_settings().quotes_dir.resolve()
    if not path.resolve().is_relative_to(quotes_dir) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PDF ещё не сформирован")
    return FileResponse(path, media_type="application/pdf", filename=f"KP_{quote.id}_v{quote.version}.pdf")
