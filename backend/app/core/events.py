"""In-process шина событий журнала для SSE-расширения (основной способ по T04 — опрос events?after=)."""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

KEEPALIVE_S = 15.0
_QUEUE_SIZE = 256

_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)


def publish(conversation_id: str, events: list[dict[str, Any]]) -> None:
    for q in list(_subscribers.get(conversation_id, ())):
        for event in events:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                break  # медленный клиент догонит историю опросом events?after=


def format_sse(event: dict[str, Any]) -> str:
    return f"id: {event['seq']}\nevent: event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream(
    conversation_id: str, after: int, load: Callable[[str, int], Awaitable[tuple[list[dict], bool]]]
) -> AsyncIterator[str]:
    """Сначала подписка, затем история из журнала после `after`, затем живые события без дублей."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _subscribers[conversation_id].add(q)
    last = after
    try:
        yield ": connected\n\n"
        while True:
            page, has_more = await load(conversation_id, last)
            for event in page:
                last = event["seq"]
                yield format_sse(event)
            if not has_more:
                break
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event["seq"] > last:
                last = event["seq"]
                yield format_sse(event)
    finally:
        _subscribers[conversation_id].discard(q)
        if not _subscribers[conversation_id]:
            _subscribers.pop(conversation_id, None)
