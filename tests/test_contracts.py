"""Стыки контура: замороженные схемы, эндпоинты, ToolResult и сквозной ход агента."""

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agent import runner
from app.contracts import schemas
from app.core import events
from app.core.llm import BudgetExceededError
from app.services import turn_service
from tests.conftest import OPERATOR, wait_for

# Заморозка: изменение полей ломает фронт и агента. Меняем только по согласованию команды.
FROZEN = {
    "ToolResult": ["ok", "data", "source_ref", "error"],
    "Session": ["id", "client_context", "mode", "created_at"],
    "Message": ["id", "session_id", "role", "text", "sources", "quote_id", "created_at"],
    "StepTrace": ["tool", "args", "result_summary", "source_ref", "tokens_in", "tokens_out", "cost_rub", "latency_ms"],
    "AgentTurn": [
        "turn_id",
        "session_id",
        "steps",
        "decision",
        "escalation_reason",
        "total_cost_rub",
        "total_latency_ms",
    ],
    "Ticket": [
        "id",
        "session_id",
        "type",
        "status",
        "reason",
        "summary",
        "collected_params",
        "draft_answer",
        "operator_answer",
        "suggested_patch_id",
        "created_at",
    ],
    "KBPatch": ["id", "ticket_id", "question", "answer", "scope", "status", "approved_by", "created_at"],
    "LLMCall": ["id", "ts", "session_id", "step", "model", "tokens_in", "tokens_out", "cost_rub", "latency_ms"],
    "QuoteItem": ["sku", "name", "qty", "term", "unit_price", "line_total", "vat_amount"],
    "Quote": [
        "id",
        "session_id",
        "version",
        "customer_name",
        "items",
        "total",
        "total_vat",
        "currency",
        "payment_terms",
        "delivery_terms",
        "valid_until",
        "status",
        "pdf_url",
    ],
    "AgentReply": ["turn", "text", "sources", "quote_id", "ticket_summary", "collected_params"],
}

AUTH_PATHS = [
    ("get", "/api/operator/tickets"),
    ("get", "/api/kb/patches"),
    ("get", "/api/admin/mode"),
    ("get", "/api/telemetry/summary"),
]


@pytest.mark.parametrize("name", FROZEN)
def test_frozen_fields(name):
    assert list(getattr(schemas, name).model_fields) == FROZEN[name]


def test_tool_result_defaults_and_strictness():
    r = schemas.ToolResult(ok=True)
    assert (r.data, r.source_ref, r.error) == (None, None, None)
    with pytest.raises(ValidationError):
        schemas.ToolResult(ok=True, unknown=1)


def test_literals_are_enforced():
    with pytest.raises(ValidationError):
        schemas.Session(id="s", mode="manual", created_at="2026-11-26T00:00:00Z")
    with pytest.raises(ValidationError):
        schemas.AgentTurn(turn_id="t", session_id="s", steps=[], decision="maybe", total_cost_rub=0, total_latency_ms=0)


def test_quote_money_serialised_as_decimal_strings():
    item = schemas.QuoteItem(
        sku="19576",
        name="MDaemon",
        qty=1,
        unit_price=Decimal("77556.00"),
        line_total=Decimal("77556.00"),
        vat_amount=Decimal("12926.00"),
    )
    quote = schemas.Quote(
        id="q1",
        session_id="s",
        version=1,
        customer_name="ООО Тест",
        items=[item],
        total=Decimal("77556.00"),
        total_vat=Decimal("12926.00"),
        payment_terms="100% предоплата",
        delivery_terms="5 рабочих дней",
        valid_until=date(2026, 12, 10),
        pdf_url="/api/quotes/q1/pdf",
    )
    data = quote.model_dump(mode="json")
    assert data["total"] == "77556.00"
    assert data["items"][0]["unit_price"] == "77556.00"
    assert data["status"] == "draft" and data["currency"] == "RUB"
    assert schemas.Quote.model_validate_json(quote.model_dump_json()) == quote


def test_openapi_contains_all_endpoints(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    for path in [
        "/health",
        "/api/sessions",
        "/api/sessions/{session_id}",
        "/api/sessions/{session_id}/messages",
        "/api/sessions/{session_id}/events",
        "/api/quotes/{quote_id}/pdf",
        "/api/operator/tickets",
        "/api/operator/tickets/{ticket_id}/reply",
        "/api/kb/patches",
        "/api/kb/patches/{patch_id}/approve",
        "/api/kb/patches/{patch_id}/reject",
        "/api/admin/mode",
        "/api/telemetry/summary",
    ]:
        assert path in paths, path


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "offers_count": 0, "kb_count": 0, "today_spend_rub": 0.0}


@pytest.mark.parametrize("method,path", AUTH_PATHS)
def test_manager_endpoints_require_basic_auth(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, auth=("manager", "wrong")).status_code == 401
    assert getattr(client, method)(path, auth=OPERATOR).status_code == 200


def test_manager_endpoints_closed_without_password(client, monkeypatch):
    from app.config import get_settings

    get_settings().operator_password = ""
    assert client.get("/api/telemetry/summary", auth=("manager", "")).status_code == 401


def _new_session(client, **ctx):
    r = client.post("/api/sessions", json={"client_context": ctx})
    assert r.status_code == 201
    return r.json()


def _idle_state(client, sid):
    def check():
        state = client.get(f"/api/sessions/{sid}").json()
        return state if not state["busy"] and state["messages"][-1]["role"] != "client" else None

    return wait_for(check)


def test_end_to_end_turn_is_traced_and_costed(client):
    session = _new_session(client, page="https://store.softline.ru/")
    assert session["mode"] == "autopilot"
    sid = session["id"]

    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "Нужен MDaemon на 20 пользователей"})
    assert r.status_code == 202
    assert r.json()["role"] == "client"

    state = _idle_state(client, sid)
    assert [m["role"] for m in state["messages"]] == ["client", "agent"]
    [turn] = state["turns"]
    assert turn["decision"] == "answer"
    assert turn["steps"][0]["tool"] == "llm:agent_stub"
    assert turn["steps"][0]["tokens_in"] > 0 and turn["total_cost_rub"] > 0

    summary = client.get("/api/telemetry/summary", auth=OPERATOR).json()
    [step] = summary["steps"]
    assert step["step"] == "agent_stub" and step["calls"] == 1
    assert summary["today_spend_rub"] == pytest.approx(turn["total_cost_rub"], abs=1e-4)
    assert summary["recent_calls"][0]["session_id"] == sid
    assert client.get("/health").json()["today_spend_rub"] > 0


def test_unknown_session_and_validation(client):
    assert client.get("/api/sessions/s_missing").status_code == 404
    assert client.post("/api/sessions/s_missing/messages", json={"text": "hi"}).status_code == 404
    sid = _new_session(client)["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": ""}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "x" * 5000}).status_code == 422
    assert client.post("/api/sessions", json={"client_context": {"x": "y" * 5000}}).status_code == 422


def test_pilot_mode_creates_draft_and_operator_sends_it(client):
    r = client.post("/api/admin/mode", json={"mode": "pilot"}, auth=OPERATOR)
    assert r.json()["default_mode"] == "pilot"
    sid = _new_session(client)["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "Какие сроки доставки?"})
    state = _idle_state(client, sid)
    assert state["turns"][0]["decision"] == "draft"
    assert state["messages"][-1]["text"] == turn_service.PILOT_TEXT
    [public_ticket] = state["open_tickets"]
    assert public_ticket["draft_answer"] is None  # клиент не видит черновик

    [item] = client.get("/api/operator/tickets?status=open", auth=OPERATOR).json()
    ticket = item["ticket"]
    assert ticket["type"] == "draft" and ticket["draft_answer"].startswith("Эхо:")
    assert item["turns"][0]["steps"]

    r = client.post(
        f"/api/operator/tickets/{ticket['id']}/reply", json={"send_draft": True, "propose_patch": True}, auth=OPERATOR
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resolved" and r.json()["suggested_patch_id"]
    messages = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert messages[-1]["role"] == "operator" and messages[-1]["text"] == ticket["draft_answer"]
    again = client.post(f"/api/operator/tickets/{ticket['id']}/reply", json={"text": "x"}, auth=OPERATOR)
    assert again.status_code == 409

    [patch] = client.get("/api/kb/patches?status=proposed", auth=OPERATOR).json()
    r = client.post(f"/api/kb/patches/{patch['id']}/approve", auth=OPERATOR)
    assert r.json()["status"] == "approved" and r.json()["approved_by"] == "manager"
    assert client.get("/health").json()["kb_count"] == 1
    assert client.post(f"/api/kb/patches/{patch['id']}/reject", auth=OPERATOR).status_code == 409


def test_session_mode_switch(client):
    sid = _new_session(client)["id"]
    r = client.post("/api/admin/mode", json={"mode": "pilot", "session_id": sid}, auth=OPERATOR)
    assert r.json()["session"]["mode"] == "pilot" and r.json()["default_mode"] == "autopilot"
    assert (
        client.post("/api/admin/mode", json={"mode": "pilot", "session_id": "s_nope"}, auth=OPERATOR).status_code == 404
    )


def test_budget_exceeded_escalates_instead_of_500(client, monkeypatch):
    async def broke(*args, **kwargs):
        raise BudgetExceededError("лимит")

    monkeypatch.setattr(runner, "run_turn", broke)
    sid = _new_session(client)["id"]
    for text in ["Первый вопрос", "Повтор"]:
        assert client.post(f"/api/sessions/{sid}/messages", json={"text": text}).status_code == 202
        state = _idle_state(client, sid)
    assert state["messages"][-1]["text"] == turn_service.HANDOFF_TEXT
    assert state["turns"][-1]["escalation_reason"] == "budget_exceeded"
    tickets = client.get("/api/operator/tickets", auth=OPERATOR).json()
    assert len(tickets) == 1  # повторная эскалация дополняет открытое обращение
    assert tickets[0]["ticket"]["type"] == "escalation"


def test_busy_session_rejects_parallel_message(client, monkeypatch):
    release = asyncio.Event()
    real = runner.run_turn

    async def slow(*args, **kwargs):
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner, "run_turn", slow)
    sid = _new_session(client)["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "раз"}).status_code == 202
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "два"}).status_code == 409
    client.portal.call(release.set)
    _idle_state(client, sid)


def test_turn_limit(client, monkeypatch):
    from app.config import get_settings

    get_settings().max_turns_per_session = 1
    sid = _new_session(client)["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "раз"})
    _idle_state(client, sid)
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "два"}).status_code == 429


def test_rate_limit_per_session(client):
    from app.config import get_settings

    get_settings().rate_limit_session_msg_per_min = 1
    sid = _new_session(client)["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "раз"})
    _idle_state(client, sid)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "два"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_rate_limit_per_ip(client):
    from app.config import get_settings

    get_settings().rate_limit_ip_per_min = 3
    codes = [client.post("/api/sessions", json={}).status_code for _ in range(4)]
    assert codes == [201, 201, 201, 429]
    assert client.get("/health").status_code == 200  # health не лимитируется


def test_quote_pdf_missing(client):
    assert client.get("/api/quotes/q_missing/pdf").status_code == 404


@pytest.mark.anyio
async def test_sse_stream_delivers_published_events():
    gen = events.stream("s_sse")
    assert await anext(gen) == ": connected\n\n"
    step = schemas.StepTrace(
        tool="search_catalog",
        args={"query": "mdaemon"},
        result_summary="5 найдено",
        source_ref="https://store.softline.ru/x",
    )
    events.publish("s_sse", "step_trace", step)
    chunk = await anext(gen)
    assert chunk.startswith("event: step_trace\ndata: ")
    assert '"source_ref": "https://store.softline.ru/x"' in chunk
    await gen.aclose()
    assert "s_sse" not in events._subscribers
