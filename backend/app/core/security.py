"""Защита публичного стенда: rate-limit по IP и сессии, Basic Auth для менеджерских эндпоинтов.

Счётчики живут в памяти процесса, поэтому backend запускается одним воркером uvicorn.
IP клиента берётся из scope["client"]; за Caddy uvicorn работает с --proxy-headers.
"""

import json
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

_SESSION_MSG_RE = re.compile(r"^/api/sessions/([^/]+)/messages/?$")
_WINDOW_S = 60.0


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
session_limiter = SlidingWindow()


class RateLimitMiddleware:
    """Ограничивает публичный API (/api/sessions*, /api/quotes*). Менеджерские пути закрыты Basic Auth."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if not (path.startswith("/api/sessions") or path.startswith("/api/quotes")):
            await self.app(scope, receive, send)
            return

        s = get_settings()
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        retry = ip_limiter.hit(f"ip:{ip}", s.rate_limit_ip_per_min)
        if retry is None and scope["method"] == "POST" and (m := _SESSION_MSG_RE.match(path)):
            retry = session_limiter.hit(f"session:{m.group(1)}", s.rate_limit_session_msg_per_min)
        if retry is not None:
            await _too_many(send, retry)
            return
        await self.app(scope, receive, send)


async def _too_many(send: Send, retry_after: float) -> None:
    body = json.dumps({"detail": "Слишком много запросов, попробуйте чуть позже"}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"retry-after", str(max(1, int(retry_after) + 1)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


_basic = HTTPBasic(realm="Softline operator", auto_error=False)


def require_operator(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """Basic Auth для /api/operator/*, /api/kb/*, /api/admin/*, /api/telemetry/*. Возвращает логин."""
    s = get_settings()
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется авторизация менеджера",
        headers={"WWW-Authenticate": 'Basic realm="Softline operator"'},
    )
    if not s.operator_password or credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(credentials.username.encode(), s.operator_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), s.operator_password.encode())
    if not (user_ok and pass_ok):
        raise unauthorized
    return credentials.username
