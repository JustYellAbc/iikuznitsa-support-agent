import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import security
from app.services import turn_service

OPERATOR = ("manager", "test-secret")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("OPERATOR_USER", OPERATOR[0])
    monkeypatch.setenv("OPERATOR_PASSWORD", OPERATOR[1])
    monkeypatch.setenv("DAILY_BUDGET_RUB", "100")
    monkeypatch.setenv("DEFAULT_MODE", "autopilot")
    monkeypatch.chdir(tmp_path)  # не подхватывать локальный .env разработчика
    get_settings.cache_clear()
    security.ip_limiter.reset()
    security.conversation_limiter.reset()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Api:
    """Клиент с независимыми cookie-сессиями по ролям (customer, manager, other_customer...)."""

    def __init__(self, client: TestClient):
        self.client = client
        self.tokens: dict[str, str] = {}
        self._keys = 0

    def call(
        self,
        role: str,
        method: str,
        url: str,
        *,
        json: Any = None,
        key: str | None = None,
        params: dict | None = None,
        auto_key: bool = True,
    ):
        headers = {}
        if role in self.tokens:
            headers["Cookie"] = f"support_session={self.tokens[role]}"
        if key is None and auto_key and method == "POST":
            self._keys += 1
            key = f"k{self._keys}"
        if key is not None:
            headers["Idempotency-Key"] = key
        self.client.cookies.clear()
        response = self.client.request(method, url, json=json, params=params, headers=headers)
        token = response.cookies.get("support_session")
        if token:
            self.tokens[role] = token
        return response

    def login(self, role: str = "manager", credentials: tuple[str, str] = OPERATOR):
        r = self.call(
            role, "POST", "/api/v1/auth/manager/login", json={"username": credentials[0], "password": credentials[1]}
        )
        assert r.status_code == 200, r.text
        return r

    def new_conversation(self, role: str = "customer") -> str:
        r = self.call(role, "POST", "/api/v1/conversations")
        assert r.status_code == 201, r.text
        return r.json()["conversationId"]

    def say(self, conversation_id: str, text: str, role: str = "customer", key: str | None = None, **extra):
        return self.call(
            role,
            "POST",
            f"/api/v1/conversations/{conversation_id}/actions",
            json={"type": "message", "text": text, **extra},
            key=key,
        )

    def events(self, conversation_id: str, role: str = "customer", after: int = 0) -> list[dict]:
        r = self.call(role, "GET", f"/api/v1/conversations/{conversation_id}/events", params={"after": after})
        assert r.status_code == 200, r.text
        return r.json()["events"]

    def idle(self):
        self.client.portal.call(turn_service.wait_idle)


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def api(client):
    return Api(client)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("condition not met in time")
