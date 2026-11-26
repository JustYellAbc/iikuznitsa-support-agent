"""T12 policy and integration checks against the stand's real SQLite queue."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from tests.conftest import api as stand_api  # noqa: F401 — pytest fixture
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.handoff import ASK_TOPIC, OFFER_HELP, route_action
from app.contracts.schemas import Message
from app.contracts.t04 import Product, ProductsData
from app.db import repository


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Соедините с оператором, лицензия не активируется", "customer_request"),
        ("Закупка по 44-ФЗ, понадобятся документы", "tender"),
        ("Нужны документы вне Store", "special_terms"),
        ("Пришлите анкету поставщика", "special_terms"),
        ("А можно запросить у партнёров?", "outside_catalog"),
        ("Запросить цену", "price_request"),
        ("Просим отсрочку", "special_terms"),
    ],
)
def test_mandatory_routes(text, reason):
    route = route_action([], text, [])
    assert route.reason == reason and route.clarification is None
    assert route.question == text


@pytest.mark.parametrize(
    "text",
    [
        "Не надо подключать оператора",
        "Это не тендер",
        "Сколько стоит лицензия?",
        "Где скачать счёт?",
        "Что можно делать с ролью оператора?",
        "Мой оператор связи",
        "Нужен Мдеймон на 20 человек",
    ],
)
def test_ordinary_requests_do_not_open_tickets(text):
    assert route_action([], text, []) is None


def test_search_outcomes_and_explicit_button():
    question = "Сможете найти эту программу?"
    assert route_action([], question, [], finding="partner_required").reason == "outside_catalog"
    assert route_action([], question, [], finding="insufficient_evidence").offer_help
    button = route_action([], "", [], action_type="request_operator")
    assert button.reason == "customer_request" and button.question
    msg = Message(id="1", session_id="demo", role="client", text=question, created_at=datetime.now(UTC))
    assert route_action([msg], "", [], action_type="request_operator").question == question
    assert route_action([msg], "Позовите оператора, лицензия не активируется", []).question.endswith("не активируется")


def rows(api, cid):
    return [e["data"] for e in api.events(cid)]


def say(api, cid, text, **extra):
    response = api.say(cid, text, **extra)
    assert response.status_code == 202, response.text
    api.idle()
    return rows(api, cid)


def queue(api):
    response = api.call("manager", "GET", "/api/v1/manager/handoffs")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def publish_product(api, cid, price):
    # Same journal event the server persists for a successful product selection (T07).
    product = Product.from_catalog("19576", "MDaemon", "https://store.softline.ru/test/19576/", True, price)
    event = ProductsData(products=[product]).model_dump(mode="json")
    api.client.portal.call(repository.append_events, cid, "selection", [event])
    return product.model_dump(mode="json")


@pytest.mark.parametrize(
    ("question", "reason", "price"),
    [
        ("Закупка тендерная, нужны документы", "tender", "77556"),
        ("Есть ли цена этой лицензии?", "price_request", None),
        ("Нужна анкета поставщика", "special_terms", "77556"),
        ("Запросите у партнёра", "outside_catalog", "77556"),
        ("Соедините с менеджером", "customer_request", "77556"),
    ],
)
def test_customer_and_manager_share_handoff(stand_api, question, reason, price):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    product = publish_product(api, cid, price)
    api.login()
    data = say(api, cid, question)
    [h] = [r["handoff"] for r in data if r["type"] == "handoff"]
    assert h["reason"] == reason and h["products"] == [product] and h["question"] == question
    say(api, cid, question)
    assert queue(api) == [h]
    hid = h["handoffId"]
    url = f"/api/v1/manager/handoffs/{hid}/resolution"
    assert api.call("manager", "POST", url, json={"type": "reply", "reply": "Уточните реквизиты"}).status_code == 202
    say(api, cid, "Уточнил", handoffId=hid)
    assert api.call("manager", "POST", url, json={"type": "close", "reply": "Отправил документы"}).status_code == 202
    assert queue(api) == []
    manager = api.events(cid, role="manager")
    assert manager == api.events(cid)
    assert [e["data"].get("text") for e in manager if e["data"].get("handoffId") == hid] == [
        "Уточните реквизиты",
        "Уточнил",
        "Отправил документы",
    ]


def test_insufficient_evidence_requires_consent_and_keeps_question(stand_api):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    api.login()
    product = publish_product(api, cid, "77556")
    question = "Какой срок действия лицензии?"
    assert say(api, cid, question)[-1]["text"] == OFFER_HELP
    assert queue(api) == []
    say(api, cid, "Да, пожалуйста")
    [h] = queue(api)
    assert (h["reason"], h["question"], h["products"]) == ("insufficient_evidence", question, [product])


def test_parallel_t09_button_retries_create_one_receipt_and_handoff(stand_api):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    path = f"/api/v1/conversations/{cid}/actions"
    together = Barrier(4)
    cookie = "support_session=" + api.tokens["customer"]

    def click(key):
        together.wait(timeout=5)
        return api.client.post(
            path, json={"type": "request_operator"}, headers={"Idempotency-Key": key, "Cookie": cookie}
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(click, ["t09-a", "t09-a", "t09-b", "t09-c"]))
    assert all(r.status_code == 202 and r.json() == replies[0].json() for r in replies)
    [h] = [r["handoff"] for r in rows(api, cid) if r["type"] == "handoff"]
    assert h["reason"] == "customer_request" and h["products"] == []
    api.login()
    assert queue(api) == [h]


@pytest.mark.parametrize(
    "request_text",
    ["Позови оператора", "Соедините меня с живым человеком, пожалуйста", "Оператор!", "Хочу поговорить с менеджером"],
)
def test_first_request_without_a_topic_then_repeat(stand_api, request_text):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    api.login()
    say(api, cid, "Добрый день!")
    first = say(api, cid, request_text)
    assert first[-1]["text"] == ASK_TOPIC and first[-1]["purpose"] == "clarification"
    assert not queue(api)
    second = say(api, cid, "Позови оператора")
    assert [e["text"] for e in second if e.get("text") == ASK_TOPIC] == [ASK_TOPIC]
    [ticket] = queue(api)
    assert ticket["reason"] == "customer_request"
    say(api, cid, request_text)
    assert queue(api) == [ticket]


def test_topic_after_clarification_then_customer_insists(stand_api):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    api.login()
    say(api, cid, "Позови оператора")
    question = "Где в заказе найти счёт?"
    reply = say(api, cid, question)[-1]
    assert reply["author"] == "agent" and reply["purpose"] == "answer"
    assert not queue(api)
    say(api, cid, "Позови оператора")
    [ticket] = queue(api)
    assert (ticket["reason"], ticket["question"]) == ("customer_request", question)


def test_tender_after_first_request_goes_straight_to_manager(stand_api):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    api.login()
    say(api, cid, "Оператор")
    question = "Закупка тендерная, нужны документы"
    say(api, cid, question)
    [ticket] = queue(api)
    assert (ticket["reason"], ticket["question"]) == ("tender", question)
