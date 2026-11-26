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
COOKIE_PATH = "/api/v1"

RoleName = Literal["customer", "manager"]


@dataclass(frozen=True)
class Principal:
    role: RoleName
    id: str  # владелец диалогов покупателя или "manager:<логин>"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_principal(request: Request) -> Principal | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    found = await repository.get_auth_session(_hash(token))
    return Principal(found.role, found.principal) if found else None


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


def set_session_cookie(request: Request, response: Response, token: str, role: RoleName) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(_ttl(role).total_seconds()),
        path=COOKIE_PATH,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )


def _ttl(role: RoleName) -> timedelta:
    s = get_settings()
    return timedelta(hours=s.manager_session_hours if role == "manager" else s.customer_session_hours)


async def drop_session(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        await repository.delete_auth_session(_hash(token))
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


def check_manager_credentials(username: str, password: str) -> bool:
    s = get_settings()
    if not s.operator_password:
        return False
    user_ok = secrets.compare_digest(username.encode(), s.operator_user.encode())
    pass_ok = secrets.compare_digest(password.encode(), s.operator_password.encode())
    return user_ok and pass_ok
