import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import security

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
    security.session_limiter.reset()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("condition not met in time")
