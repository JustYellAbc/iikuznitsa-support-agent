"""Стыки контура T05: внутренние контракты, правила T04, доступ, ключи повтора и расширения стенда."""

import asyncio

import pytest
from pydantic import ValidationError

from app.agent import runner
from app.contracts import schemas
from app.contracts.t04 import Product
from app.core import events
from app.core.llm import BudgetExceededError
from app.services import turn_service
from tests.conftest import Api

# Заморозка внутренних контрактов: на них опираются агент и консоль. Менять — по согласованию команды.
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
    "HandoffRequest": ["reason", "question", "products"],
    "AgentReply": ["turn", "text", "purpose", "sources", "products", "handoff"],
    "KBPatch": ["id", "handoff_id", "question", "answer", "scope", "status", "approved_by", "created_at"],
    "LLMCall": ["id", "ts", "session_id", "step", "model", "tokens_in", "tokens_out", "cost_rub", "latency_ms"],
}

MANAGER_GETS = [
    "/api/v1/manager/handoffs",
    "/api/v1/manager/telemetry",
    "/api/v1/manager/mode",
    "/api/v1/manager/drafts",
    "/api/v1/manager/kb/patches",
]


@pytest.mark.parametrize("name", FROZEN)
def test_frozen_fields(name):
    assert list(getattr(schemas, name).model_fields) == FROZEN[name]


def test_tool_result_strict():
    assert schemas.ToolResult(ok=True).source_ref is None
    with pytest.raises(ValidationError):
        schemas.ToolResult(ok=True, unknown=1)


@pytest.mark.parametrize(
    "offer,available,price,term,state,kopecks,license_term",
    [
        ("19576", True, "77556", "12 мес.", "listed", 7755600, "12 мес."),
        ("48093", False, "1523654", None, "availability_check", 152365400, None),
        ("504", False, "0", "12 мес.", "on_request", None, "12 мес."),
        ("61557", True, "4900", "Не определено", "listed", 490000, None),
    ],
)
def test_product_price_states_follow_t04(offer, available, price, term, state, kopecks, license_term):
    p = Product.from_catalog(offer, "X", f"https://store.softline.ru/{offer}/", available, price, term)
    assert (p.priceState, p.unitPriceKopecks, p.licenseTerm) == (state, kopecks, license_term)


def test_product_rejects_inconsistent_price_state():
    with pytest.raises(ValidationError):
        Product(
            offerId="1",
            name="X",
            source={"url": "https://x.ru", "label": "x"},
            available=True,
            priceState="listed",
            unitPriceKopecks=None,
            licenseTerm=None,
        )


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok", "offers_count": 0, "kb_count": 0, "today_spend_rub": 0.0}
    paths = client.get("/api/openapi.json").json()["paths"]
    for path in [
        "/api/v1/conversations",
        "/api/v1/conversations/{conversationId}/actions",
        "/api/v1/conversations/{conversationId}/events",
        "/api/v1/manager/handoffs",
        "/api/v1/manager/handoffs/{handoffId}/resolution",
        "/api/v1/documents/{documentId}",
        "/api/v1/auth/manager/login",
        "/api/v1/manager/telemetry",
        "/api/v1/manager/mode",
        "/api/v1/manager/conversations/{conversationId}/trace",
        "/api/v1/manager/kb/patches",
    ]:
        assert path in paths, path


# --- Доступ и ошибки ---------------------------------------------------------------------


@pytest.mark.parametrize("path", MANAGER_GETS)
def test_manager_routes_need_manager_session(api: Api, path):
    assert api.call("anon", "GET", path).json() == {"code": "unauthenticated", "message": "Требуется вход."}
    api.new_conversation("customer")
    r = api.call("customer", "GET", path)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    api.login()
    assert api.call("manager", "GET", path).status_code == 200


def test_login_rejects_wrong_or_unset_password(api: Api, client):
    r = api.call("x", "POST", "/api/v1/auth/manager/login", json={"username": "manager", "password": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated" and "x" not in api.tokens
    from app.config import get_settings

    get_settings().operator_password = ""
    r = api.call("x", "POST", "/api/v1/auth/manager/login", json={"username": "manager", "password": ""})
    assert r.status_code == 422
    r = api.call("x", "POST", "/api/v1/auth/manager/login", json={"username": "manager", "password": "test-secret"})
    assert r.status_code == 401


def test_session_cookie_attributes(api: Api, client):
    r = api.call("customer", "POST", "/api/v1/conversations")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "path=/api/v1" in cookie and "samesite=lax" in cookie


def test_logout_drops_session(api: Api):
    api.login()
    assert api.call("manager", "POST", "/api/v1/auth/logout").status_code == 204
    assert api.call("manager", "GET", "/api/v1/manager/telemetry").status_code == 401


@pytest.mark.parametrize("first_role", ["manager", "buyer", "legacy_customer"])
def test_buyer_widget_and_manager_share_browser(client, first_role):
    conversations = "/api/v1/conversations"
    manager_url = "/api/v1/manager/handoffs"
    buyer = {"view": "buyer"}

    def start(params):
        response = client.post(conversations, params=params, headers={"Idempotency-Key": "open"})
        assert response.status_code == 201
        return response.json()["conversationId"]

    if first_role != "manager":
        cid = start(buyer if first_role == "buyer" else {})
        assert client.get(manager_url).status_code in (401, 403)
    assert client.post("/api/v1/auth/manager/login", json={"username": "manager", "password": "test-secret"}).is_success
    if first_role == "manager":
        cid = start(buyer)
    route = f"{conversations}/{cid}"
    assert client.get(f"{route}/events", params=buyer).is_success
    assert (
        client.post(
            f"{route}/actions",
            params=buyer,
            headers={"Idempotency-Key": "message"},
            json={"type": "message", "text": "Тест"},
        ).status_code
        == 202
    )
    assert client.get(manager_url).status_code == 200
    assert client.get(manager_url, params=buyer).status_code == 403
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get(manager_url).status_code == 401
    assert client.get(f"{route}/events", params=buyer).status_code == 200


def test_buyer_view_cannot_borrow_manager_access_or_log_manager_out(api, client):
    foreign = api.new_conversation()
    api.say(foreign, "Тендерная закупка, нужны документы.")
    api.idle()
    api.login()
    events_url = f"/api/v1/conversations/{foreign}/events"
    assert client.get(events_url).status_code == 200
    buyer = {"view": "buyer"}
    assert client.get(events_url, params=buyer).status_code == 401
    assert client.post("/api/v1/auth/logout", params=buyer).status_code == 204
    assert client.post("/api/v1/conversations", params=buyer, headers={"Idempotency-Key": "open"}).status_code == 201
    assert client.get(events_url, params=buyer).status_code == 404
    assert client.post("/api/v1/auth/logout", params=buyer).status_code == 204
    assert client.get(events_url, params=buyer).status_code == 401
    assert client.get(events_url).status_code == 200


def test_errors_use_t04_format(api: Api):
    cid = api.new_conversation()
    r = api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/actions",
        json={"type": "message", "text": "hi"},
        auto_key=False,
    )
    assert r.status_code == 422 and r.json()["code"] == "invalid_input"
    r = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "nope"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_input"
    r = api.call("customer", "GET", "/api/v1/nowhere")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = api.call("customer", "GET", f"/api/v1/conversations/{cid}/events", params={"after": -1})
    assert r.json()["code"] == "invalid_input"
    r = api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/actions",
        json={"type": "confirm_quote", "quoteId": "q1", "revision": 1},
    )
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert api.call("customer", "GET", "/api/v1/documents/d1").status_code == 404


def test_customer_sees_only_own_conversations(api: Api):
    cid = api.new_conversation("alice")
    api.new_conversation("bob")
    assert api.call("bob", "GET", f"/api/v1/conversations/{cid}/events").status_code == 404
    assert (
        api.call("bob", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"}).status_code
        == 404
    )
    api.login()
    # оператор видит диалог только после обращения
    assert api.call("manager", "GET", f"/api/v1/conversations/{cid}/events").status_code == 404
    assert api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").status_code == 404
    api.call("alice", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    assert api.call("manager", "GET", f"/api/v1/conversations/{cid}/events").status_code == 200
    assert api.call("manager", "POST", "/api/v1/conversations").status_code == 403


def test_idempotency_scope_and_conflict(api: Api):
    r1 = api.call("customer", "POST", "/api/v1/conversations", key="same")
    r2 = api.call("customer", "POST", "/api/v1/conversations", key="same")
    assert r1.json() == r2.json()
    other = api.call("other", "POST", "/api/v1/conversations", key="same")
    assert other.json() != r1.json()  # область ключа — пользователь
    cid = r1.json()["conversationId"]
    api.say(cid, "раз", key="m1")
    api.idle()
    r = api.say(cid, "два", key="m1")
    assert r.status_code == 409 and r.json()["code"] == "key_conflict"


# --- Сквозной ход с настоящим stub-агентом ------------------------------------------------------


def test_end_to_end_turn_is_traced_and_costed(api: Api):
    cid = api.new_conversation()
    r = api.say(cid, "Добрый день! Чем вы можете помочь?")
    assert r.status_code == 202 and r.json()["conversationId"] == cid
    api.idle()
    evs = api.events(cid)
    assert [(e["seq"], e["data"]["author"], e["data"]["purpose"]) for e in evs] == [
        (1, "customer", "input"),
        (2, "agent", "answer"),
    ]
    assert {e["actionId"] for e in evs} == {r.json()["actionId"]}

    api.login()
    api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    [turn] = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()
    assert turn["decision"] == "answer" and turn["steps"][0]["tool"] == "llm:agent_stub"
    summary = api.call("manager", "GET", "/api/v1/manager/telemetry").json()
    assert summary["steps"][0]["step"] == "agent_stub" and summary["steps"][0]["calls"] == 1
    assert summary["recent_calls"][0]["session_id"] == cid
    assert api.call("anon", "GET", "/health").json()["today_spend_rub"] > 0


def test_events_paging(api: Api, client):
    cid = api.new_conversation()
    from app.db import repository

    client.portal.call(
        repository.append_events,
        cid,
        "a_x",
        [
            {"type": "message", "author": "agent", "purpose": "notice", "text": str(i), "sources": []}
            for i in range(105)
        ],
    )
    page = api.call("customer", "GET", f"/api/v1/conversations/{cid}/events").json()
    assert len(page["events"]) == 100 and page["hasMore"] and page["nextAfter"] == 100
    page = api.call("customer", "GET", f"/api/v1/conversations/{cid}/events", params={"after": 100}).json()
    assert [e["seq"] for e in page["events"]] == [101, 102, 103, 104, 105] and not page["hasMore"]
    empty = api.call("customer", "GET", f"/api/v1/conversations/{cid}/events", params={"after": 105}).json()
    assert empty == {"events": [], "nextAfter": 105, "hasMore": False}


def test_request_operator_carries_last_products_and_dedupes(api: Api, monkeypatch):
    product = Product.from_catalog("19576", "MDaemon", "https://store.softline.ru/x-19576/", True, "77556")

    async def answer_with_product(session, history, user_text, emit_step):
        turn = schemas.AgentTurn(
            turn_id="t1" + user_text[:1],
            session_id=session.id,
            steps=[],
            decision="answer",
            total_cost_rub=0,
            total_latency_ms=0,
        )
        return schemas.AgentReply(turn=turn, text="Вот", products=[product])

    monkeypatch.setattr(runner, "run_turn", answer_with_product)
    cid = api.new_conversation()
    api.say(cid, "MDaemon")
    api.idle()
    r1 = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    r2 = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    assert r1.json() == r2.json()  # другой ключ, то же открытое обращение
    api.login()
    [handoff] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert handoff["reason"] == "customer_request" and handoff["products"][0]["offerId"] == "19576"


def test_handoff_reply_rules(api: Api):
    cid = api.new_conversation()
    api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    api.login()
    [h] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    url = f"/api/v1/manager/handoffs/{h['handoffId']}/resolution"
    assert api.call("customer", "POST", url, json={"type": "reply", "reply": "x"}).status_code == 403
    r = api.call("manager", "POST", url, json={"type": "close", "reply": "Готово: https://store.softline.ru/a/."})
    assert r.status_code == 202
    last = api.events(cid, "manager")[-2]["data"]
    assert last["sources"] == [{"url": "https://store.softline.ru/a/", "label": "store.softline.ru"}]
    r = api.call("manager", "POST", url, json={"type": "reply", "reply": "ещё"})
    assert r.status_code == 409 and r.json()["code"] == "already_resolved"
    r = api.say(cid, "ответ", handoffId=h["handoffId"])
    assert r.status_code == 409 and r.json()["code"] == "already_resolved"
    other = api.new_conversation("other")
    assert api.say(other, "чужое", role="other", handoffId=h["handoffId"]).status_code == 404
    r = api.call(
        "manager", "POST", "/api/v1/manager/handoffs/h_missing/resolution", json={"type": "reply", "reply": "x"}
    )
    assert r.status_code == 404


# --- Режим pilot, бюджет, лимиты --------------------------------------------------------------


def test_pilot_mode_drafts_and_send(api: Api):
    api.login()
    assert api.call("manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot"}).json()["default_mode"] == "pilot"
    cid = api.new_conversation()
    api.say(cid, "Какие сроки доставки?")
    api.idle()
    evs = api.events(cid)
    assert evs[-1]["data"]["purpose"] == "notice" and evs[-1]["data"]["text"] == turn_service.PILOT_TEXT
    [draft] = api.call("manager", "GET", "/api/v1/manager/drafts").json()
    assert draft["conversation_id"] == cid and draft["events"][0]["text"].startswith("Эхо:")
    assert api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()[0]["decision"] == "draft"
    r = api.call("manager", "POST", f"/api/v1/manager/drafts/{draft['id']}/send", json={"text": "Доставка 1 день"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    last = api.events(cid)[-1]
    assert last["data"]["text"] == "Доставка 1 день" and last["actionId"] == draft["action_id"]
    r = api.call("manager", "POST", f"/api/v1/manager/drafts/{draft['id']}/send", json={})
    assert r.status_code == 409


def test_conversation_mode_switch(api: Api):
    cid = api.new_conversation()
    api.login()
    r = api.call("manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot", "conversation_id": cid})
    assert r.json()["conversation"]["mode"] == "pilot" and r.json()["default_mode"] == "autopilot"
    r = api.call("manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot", "conversation_id": "c_nope"})
    assert r.status_code == 404


def test_budget_exceeded_hands_off_instead_of_500(api: Api, monkeypatch):
    async def broke(*args, **kwargs):
        raise BudgetExceededError("лимит")

    monkeypatch.setattr(runner, "run_turn", broke)
    cid = api.new_conversation()
    for text in ["Первый вопрос", "Повтор"]:
        assert api.say(cid, text).status_code == 202
        api.idle()
    datas = [e["data"] for e in api.events(cid)]
    assert [d["type"] for d in datas] == ["message", "message", "handoff", "message", "message"]
    assert datas[1]["text"] == turn_service.HANDOFF_TEXT and datas[2]["handoff"]["reason"] == "technical"
    assert datas[2]["handoff"]["question"] == "Первый вопрос"
    api.login()
    assert len(api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]) == 1
    turns = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()
    assert turns[0]["escalation_reason"] == "budget_exceeded"


def test_agent_busy_rejects_parallel_message(api: Api, client, monkeypatch):
    release = asyncio.Event()
    real = runner.run_turn

    async def slow(*args, **kwargs):
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner, "run_turn", slow)
    cid = api.new_conversation()
    try:
        assert api.say(cid, "раз").status_code == 202
        r = api.say(cid, "два", key="two")
        assert r.status_code == 409 and r.json() == {
            "code": "agent_busy",
            "message": "Агент ещё отвечает на предыдущее сообщение.",
        }
    finally:
        client.portal.call(release.set)
    api.idle()
    assert api.say(cid, "два", key="two").status_code == 202  # отказ не занял ключ


def test_turn_limit(api: Api):
    from app.config import get_settings

    get_settings().max_turns_per_session = 1
    cid = api.new_conversation()
    api.say(cid, "раз")
    api.idle()
    r = api.say(cid, "два")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "retry-after" not in r.headers


def test_rate_limit_per_conversation_and_ip(api: Api):
    from app.config import get_settings

    get_settings().rate_limit_session_msg_per_min = 1
    cid = api.new_conversation()
    api.say(cid, "раз")
    api.idle()
    r = api.say(cid, "два")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
    get_settings().rate_limit_ip_per_min = 2
    from app.core import security

    security.ip_limiter.reset()
    codes = [api.call(f"u{i}", "POST", "/api/v1/conversations").status_code for i in range(3)]
    assert codes == [201, 201, 429]
    assert api.call("anon", "GET", "/health").status_code == 200


def test_kb_patch_flow(api: Api):
    api.login()
    r = api.call("manager", "POST", "/api/v1/manager/kb/patches", json={"question": "Сроки?", "answer": "1 день"})
    assert r.status_code == 201
    pid = r.json()["id"]
    r = api.call("manager", "POST", f"/api/v1/manager/kb/patches/{pid}/approve")
    assert r.json()["status"] == "approved" and r.json()["approved_by"] == "manager"
    assert api.call("anon", "GET", "/health").json()["kb_count"] == 1
    assert api.call("manager", "POST", f"/api/v1/manager/kb/patches/{pid}/reject").status_code == 409


@pytest.mark.anyio
async def test_sse_stream_replays_then_follows():
    history = [{"seq": 1, "at": "t", "actionId": "a1", "data": {"type": "message"}}]

    async def load(cid, after):
        return [e for e in history if e["seq"] > after], False

    gen = events.stream("c_sse", 0, load)
    assert await anext(gen) == ": connected\n\n"
    assert (await anext(gen)).startswith("id: 1\nevent: event\n")
    events.publish("c_sse", [history[0], {"seq": 2, "at": "t", "actionId": "a2", "data": {"type": "message"}}])
    assert (await anext(gen)).startswith("id: 2\n")  # seq 1 уже отдан из истории — дубль пропущен
    await gen.aclose()
    assert "c_sse" not in events._subscribers
