"""Защита публичного стенда: rate-limit по IP и по диалогу.

Счётчики живут в памяти процесса, поэтому backend запускается одним воркером uvicorn.
IP клиента берётся из scope["client"]; за Caddy uvicorn работает с --proxy-headers.
Отказ 429 rate_limited приходит до обработчика, поэтому не создаёт событий и не занимает Idempotency-Key.
"""

import json
import re
import time
from collections import defaultdict, deque

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

_ACTIONS_RE = re.compile(r"^/api/v1/conversations/([^/]+)/(?:actions|quotes)/?$")
_LIMITED_PREFIXES = ("/api/v1/conversations", "/api/v1/documents", "/api/v1/auth")
_WINDOW_S = 60.0
RATE_LIMITED_MESSAGE = "Слишком много запросов. Попробуйте чуть позже."


class SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, now: float | None = None) -> float | None:
        """Регистрирует запрос. None — разрешено, иначе через сколько секунд повторить."""
        now = time.monotonic() if now is None else now
        q = self._hits[key]
        while q and now - q[0] >= _WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            return _WINDOW_S - (now - q[0])
        q.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()


ip_limiter = SlidingWindow()
conversation_limiter = SlidingWindow()


class RateLimitMiddleware:
    """Публичные пути покупателя и вход: лимит по IP; команды диалога — ещё и лимит по conversationId."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if not path.startswith(_LIMITED_PREFIXES):
            await self.app(scope, receive, send)
            return

        s = get_settings()
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        retry = ip_limiter.hit(f"ip:{ip}", s.rate_limit_ip_per_min)
        if retry is None and scope["method"] == "POST" and (m := _ACTIONS_RE.match(path)):
            retry = conversation_limiter.hit(f"conversation:{m.group(1)}", s.rate_limit_session_msg_per_min)
        if retry is not None:
            await _too_many(send, retry)
            return
        await self.app(scope, receive, send)


async def _too_many(send: Send, retry_after: float) -> None:
    body = json.dumps({"code": "rate_limited", "message": RATE_LIMITED_MESSAGE}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(max(1, int(retry_after) + 1)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
