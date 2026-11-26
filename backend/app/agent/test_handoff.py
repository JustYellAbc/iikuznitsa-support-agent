"""T12 policy and integration checks against the stand's real SQLite queue."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from tests.conftest import OPERATOR, wait_for
from tests.conftest import client as stand_client  # noqa: F401 — shared pytest fixture
from tests.conftest import env as env  # shared autouse fixture

from app.agent.handoff import OFFER_HELP, route_action
from app.contracts.schemas import Message
from app.db import repository
from app.db.database import get_db


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Позвать оператора", "customer_request"),
        ("Соедините меня с живым человеком, пожалуйста", "customer_request"),
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
    assert route.reason == reason and not route.offer_help
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


def send(stand, sid, text):
    path = f"/api/sessions/{sid}"
    assert stand.post(path + "/messages", json={"text": text}).status_code == 202
    return wait_for(lambda: (state if not (state := stand.get(path).json())["busy"] else None))


def test_handoff_round_trip(stand_client):  # noqa: F811
    stand = stand_client
    sid = stand.post("/api/sessions", json={}).json()["id"]
    for text in ["Позвать оператора", "Соедините с менеджером"]:
        state = send(stand, sid, text)
        assert state["turns"][-1]["decision"] == "escalate"
        assert len(state["open_tickets"]) == 1
    path = "/api/operator/tickets"
    assert stand.get(path).status_code == 401
    [entry] = stand.get(path, auth=OPERATOR).json()
    ticket = entry["ticket"]
    assert ticket["session_id"] == sid and ticket["reason"] == "customer_request"
    assert ticket["collected_params"]["products"] == []
    assert len([m for m in entry["messages"] if m["role"] == "client"]) == 2
    assert stand.post(f"{path}/{ticket['id']}/reply", auth=OPERATOR, json={"text": "Помогу здесь."}).is_success
    assert stand.get(f"/api/sessions/{sid}").json()["messages"][-1]["text"] == "Помогу здесь."


def test_concurrent_button_clicks_share_one_ticket(stand_client):  # noqa: F811
    stand = stand_client
    sid = stand.post("/api/sessions", json={}).json()["id"]
    path = f"/api/sessions/{sid}"
    together = Barrier(4)

    def click():
        together.wait(timeout=5)
        return stand.post(path + "/messages", json={"text": "Позвать оператора"}).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        clicks = [pool.submit(click) for _ in range(4)]
        statuses = [result.result() for result in clicks]
    assert 202 in statuses and set(statuses) <= {202, 409}
    wait_for(lambda: not stand.get(path).json()["busy"])
    [entry] = stand.get("/api/operator/tickets", auth=OPERATOR).json()
    assert entry["ticket"]["session_id"] == sid


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Требуется комплект документов для тендера", "tender"),
        ("Сколько стоит лицензия?", "price_request"),
        ("Какой срок действия лицензии?", "insufficient_evidence"),
    ],
)
def test_product_and_question_reach_operator(stand_client, question, expected):  # noqa: F811
    stand = stand_client
    url = "https://store.softline.ru/test/example-100/"

    async def seed():
        async with get_db().tx() as conn:
            await conn.execute("INSERT INTO offers(offer_id,name,url) VALUES(?,?,?)", ("100", "Example", url))

    stand.portal.call(seed)
    sid = stand.post("/api/sessions", json={"client_context": {"page": url, "price": 1}}).json()["id"]
    state = send(stand, sid, question)
    if expected == "insufficient_evidence":
        assert not state["open_tickets"] and state["messages"][-1]["text"] == OFFER_HELP
        send(stand, sid, "Да, пожалуйста")
    [entry] = stand.get("/api/operator/tickets", auth=OPERATOR).json()
    ticket = entry["ticket"]
    assert ticket["reason"] == expected
    assert ticket["collected_params"]["question"] == question
    [product] = ticket["collected_params"]["products"]
    assert product["offerId"] == "100" and product["source"]["url"] == url
    assert product["priceState"] == "on_request" and product["unitPriceKopecks"] is None


def test_product_from_dialog_takes_precedence_over_entry_page(stand_client):  # noqa: F811
    stand = stand_client
    entry = "https://store.softline.ru/entry/"
    selected = "https://store.softline.ru/alt-n-technologies-ltd/-19576/"
    sid = stand.post("/api/sessions", json={"client_context": {"page": entry}}).json()["id"]

    async def seed():
        async with get_db().tx() as conn:
            await conn.executemany(
                "INSERT INTO offers(offer_id,name,url,available,has_price,price) VALUES(?,?,?,?,?,?)",
                [("entry", "Другая позиция", entry, 0, 0, None), ("19576", "MDaemon", selected, 1, 1, 77556)],
            )
        await repository.add_message(sid, "client", "Нужна почта на 20 пользователей")
        await repository.add_message(sid, "agent", "Нашёл MDaemon на 20 пользователей.", sources=[selected])

    stand.portal.call(seed)
    send(stand, sid, "Теперь нужны документы для тендера")
    [card] = stand.get("/api/operator/tickets", auth=OPERATOR).json()
    ticket = card["ticket"]
    assert ticket["reason"] == "tender"
    assert ticket["collected_params"]["question"] == "Теперь нужны документы для тендера"
    [product] = ticket["collected_params"]["products"]
    assert (product["offerId"], product["unitPriceKopecks"], product["priceState"]) == ("19576", 7755600, "listed")
    assert card["messages"][0]["text"] == "Нужна почта на 20 пользователей"
