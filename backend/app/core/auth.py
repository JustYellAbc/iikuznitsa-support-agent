"""Cookie-сессии support_session: роль customer или manager назначает сервер.

Покупатель получает сессию при первом createConversation, менеджер — через
POST /api/v1/auth/manager/login. В БД хранится только SHA-256 токена.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Request, Response

from app.config import get_settings
from app.core.errors import forbidden_manager, unauthenticated
from app.db import repository

COOKIE = "support_session"
BUYER_COOKIE = "support_buyer_session"
COOKIE_PATH = "/api/v1"

RoleName = Literal["customer", "manager"]


@dataclass(frozen=True)
class Principal:
    role: RoleName
    id: str  # владелец диалогов покупателя или "manager:<логин>"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _principal(token: str | None) -> Principal | None:
    if not token:
        return None
    found = await repository.get_auth_session(_hash(token))
    return Principal(found.role, found.principal) if found else None


def buyer_view(request: Request) -> bool:
    return request.query_params.get("view") == "buyer"


async def current_principal(request: Request) -> Principal | None:
    if not buyer_view(request):
        return await _principal(request.cookies.get(COOKIE))
    # Старый покупатель может продолжить свой диалог. Права менеджера в виджет не переходят.
    principal = await _principal(request.cookies.get(BUYER_COOKIE) or request.cookies.get(COOKIE))
    return principal if principal and principal.role == "customer" else None


async def require_principal(request: Request) -> Principal:
    principal = await current_principal(request)
    if principal is None:
        raise unauthenticated()
    return principal


async def require_manager(request: Request) -> Principal:
    principal = await require_principal(request)
    if principal.role != "manager":
        raise forbidden_manager()
    return principal


async def create_session(role: RoleName, principal_id: str) -> tuple[Principal, str]:
    """Новая серверная сессия; токен уходит клиенту только в cookie (set_session_cookie)."""
    token = secrets.token_urlsafe(32)
    await repository.create_auth_session(_hash(token), role, principal_id, datetime.now(UTC) + _ttl(role))
    return Principal(role, principal_id), token


def set_session_cookie(request: Request, response: Response, token: str, role: RoleName, *, name: str = COOKIE) -> None:
    response.set_cookie(
        name,
        token,
        max_age=int(_ttl(role).total_seconds()),
        path=COOKIE_PATH,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )


async def preserve_legacy_buyer(request: Request, response: Response) -> None:
    """При входе менеджера сохраняем прежнюю покупательскую сессию этого браузера."""
    if request.cookies.get(BUYER_COOKIE):
        return
    token = request.cookies.get(COOKIE)
    if token:
        previous = await _principal(token)
        if previous and previous.role == "customer":
            set_session_cookie(request, response, token, "customer", name=BUYER_COOKIE)


def _ttl(role: RoleName) -> timedelta:
    s = get_settings()
    return timedelta(hours=s.manager_session_hours if role == "manager" else s.customer_session_hours)


async def drop_session(request: Request, response: Response) -> None:
    name = COOKIE
    if buyer_view(request):
        name = BUYER_COOKIE
        if name not in request.cookies and await current_principal(request):
            name = COOKIE  # ранее выданная сессия покупателя
    token = request.cookies.get(name)
    if token:
        await repository.delete_auth_session(_hash(token))
    response.delete_cookie(name, path=COOKIE_PATH)


def check_manager_credentials(username: str, password: str) -> bool:
    s = get_settings()
    if not s.operator_password:
        return False
    user_ok = secrets.compare_digest(username.encode(), s.operator_user.encode())
    pass_ok = secrets.compare_digest(password.encode(), s.operator_password.encode())
    return user_ok and pass_ok
