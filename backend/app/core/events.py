"""In-process шина событий для SSE: подписчики на session_id получают события ходов и ответов менеджера."""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from app.contracts.schemas import SseEvent, SseEventType

KEEPALIVE_S = 15.0
_QUEUE_SIZE = 256

_subscribers: dict[str, set[asyncio.Queue[SseEvent]]] = defaultdict(set)


def publish(session_id: str, type: SseEventType, payload: BaseModel | dict[str, Any]) -> None:
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    event = SseEvent(type=type, payload=data)
    for q in list(_subscribers.get(session_id, ())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # медленный клиент теряет события; полное состояние доступно через GET /api/sessions/{id}


def format_sse(event: SseEvent) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.payload, ensure_ascii=False)}\n\n"


async def stream(session_id: str) -> AsyncIterator[str]:
    q: asyncio.Queue[SseEvent] = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _subscribers[session_id].add(q)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield format_sse(event)
    finally:
        _subscribers[session_id].discard(q)
        if not _subscribers[session_id]:
            _subscribers.pop(session_id, None)
