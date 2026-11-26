import sqlite3

import pytest
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.handoff import OFFER_HELP
from app.agent.product_answers import DELIVERY_GUIDE
from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say


def test_known_facts_follow_the_selected_offer_and_keep_selection(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    question = "Какая версия и как доставляется лицензия?"
    assert "артикул" in say(api, cid, question)[-1]["text"]
    answer = say(api, cid, "SKU desk")[-1]
    assert answer["purpose"] == "answer"
    assert all(word in answer["text"] for word in ("Pro 9", "Электронный", "электронной почте"))
    assert [s["url"] for s in answer["sources"]] == ["https://example.org/store/desk", DELIVERY_GUIDE]
    changed = say(api, cid, "Какая версия у Widget Server?")[-1]
    assert changed["text"] == OFFER_HELP and "Pro 9" not in str(changed)
    assert changed["sources"][0]["url"].endswith("/verify")


def test_partial_answer_offers_real_handoff_and_preserves_original_question(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    say(api, cid, "Нужен MDaemon на 20 пользователей")
    say(api, cid, "Для компании, впервые")
    question = "Что точно входит на год? Можно ли пользоваться после?"
    answer = say(api, cid, question)[-1]
    assert answer["text"] == OFFER_HELP
    [ref] = answer["sources"]
    assert "12 мес" in ref["label"] and "право использования" in ref["label"] and ref["url"].endswith("/new20")
    api.login()
    assert api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] == []
    say(api, cid, "Да")
    [card] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (card["question"], card["reason"], card["products"][0]["offerId"]) == (
        question,
        "insufficient_evidence",
        "new20",
    )


def test_fact_questions_do_not_reset_the_purchase_parameters(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    say(api, cid, "Нужен MDaemon на 20 пользователей")
    say(api, cid, "Для компании, впервые")
    answer = say(api, cid, "Какой тип лицензии?")[-1]
    assert "Полная версия" in answer["text"]
    changed = say(api, cid, "Продлеваем, подписка действует. Позовите оператора.")[-1]["handoff"]
    assert [p["offerId"] for p in changed["products"]] == ["renew"]


@pytest.mark.parametrize(
    "question,fragment",
    [
        ("Как получить электронный ключ?", "электронной почте"),
        ("Как происходит доставка оборудования?", "адрес"),
    ],
)
def test_general_delivery_uses_the_matching_article(stand_api, index, question, fragment):  # noqa: F811
    cid = stand_api.new_conversation()
    response = say(stand_api, cid, question)[-1]
    assert response["purpose"] == "answer" and fragment in response["text"]
    assert [s["url"] for s in response["sources"]] == [DELIVERY_GUIDE]


def test_compatibility_and_order_status_cannot_be_inferred_from_delivery_method(stand_api, index):  # noqa: F811
    cid = stand_api.new_conversation()
    say(stand_api, cid, "SKU desk")
    for question, label in [("Совместим ли с Windows?", "совместимость"), ("Когда придёт мой заказ?", "состояние")]:
        msg = say(stand_api, cid, question)[-1]
        assert msg["text"] == OFFER_HELP
        assert msg["sources"][0]["url"].endswith("/desk") and label in msg["sources"][0]["label"]


def test_missing_knowledge_article_does_not_supply_delivery_instructions(stand_api, index):  # noqa: F811
    with sqlite3.connect(index) as db:
        db.execute("DELETE FROM knowledge WHERE source = ?", (DELIVERY_GUIDE,))
    cid = stand_api.new_conversation()
    say(stand_api, cid, "SKU desk")
    msg = say(stand_api, cid, "Как доставляется лицензия?")[-1]
    assert "Электронный" in msg["text"] and "почте" not in str(msg)
    assert [ref["url"] for ref in msg["sources"]] == ["https://example.org/store/desk"]


@pytest.mark.parametrize(
    "question,offer,amount",
    [
        ("А на 5 пользователей какая цена и срок?", "new5", "321"),
        ("А если продлеваем действующую подписку, какая цена и срок?", "renew", "875"),
        ("Какой срок для компании?", "new20", "12 мес"),
        ("Какой срок для управляющей компании?", "new20", "12 мес"),
        ("Какой срок для небольшой частной организации?", "new20", "12 мес"),
        ("Какой срок для юридического лица?", "new20", "12 мес"),
    ],
)
def test_question_with_qualifiers_updates_answer_and_handoff(stand_api, index, question, offer, amount):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    say(api, cid, "Нужен MDaemon на 20 пользователей для компании, покупаем впервые")
    response = say(api, cid, question)[-1]
    assert amount in response["text"] and response["sources"][0]["url"].endswith("/" + offer)
    card = say(api, cid, "Позовите оператора")[-1]["handoff"]
    [p] = card["products"]
    assert (card["question"], p["offerId"], p["source"]["url"]) == (question, offer, response["sources"][0]["url"])


@pytest.mark.parametrize("clarify", [False, True])
def test_unavailable_delivery_hands_original_question_to_manager(stand_api, index, clarify):  # noqa: F811
    cid = stand_api.new_conversation()
    question = "Сколько стоит и как получить электронный ключ?"
    if clarify:
        assert "артикул" in say(stand_api, cid, question)[-1]["text"]
        events = say(stand_api, cid, "SKU verify")
    else:
        question = "SKU verify, какая цена и как доставляется?"
        events = say(stand_api, cid, question)
    notice, card = events[-2], events[-1]["handoff"]
    assert "Наличие требует подтверждения" in notice["text"] and "120 ₽" in notice["text"]
    assert card["question"] == question and card["reason"] == "availability"
    assert [p["offerId"] for p in card["products"]] == ["verify"]


@pytest.mark.parametrize("button", [True, False])
def test_partial_fact_consent_preserves_question_and_drops_removed_offer(stand_api, index, button):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    question = "Какая версия и какой срок лицензии?"
    say(api, cid, question)
    assert say(api, cid, "SKU verify")[-1]["text"] == OFFER_HELP
    with sqlite3.connect(index) as db:
        db.execute("DELETE FROM offers WHERE id='verify'")
    if button:
        assert (
            api.call(
                "customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"}
            ).status_code
            == 202
        )
    else:
        say(api, cid, "Да")
    api.login()
    [card] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert card["question"] == question and card["products"] == []


@pytest.mark.parametrize("unavailable", [True, False])
def test_price_request_uses_current_snapshot(stand_api, index, unavailable):  # noqa: F811
    cid = stand_api.new_conversation()
    sku = "desk" if unavailable else "ask"
    say(stand_api, cid, "SKU " + sku)
    with sqlite3.connect(index) as db:
        db.execute(
            "UPDATE offers SET price_state=?, available=?, price=? WHERE id=?",
            ("request_price" if unavailable else "listed", int(not unavailable), "0" if unavailable else "250", sku),
        )
    events = say(stand_api, cid, "Какая цена и срок?")
    if unavailable:
        card = events[-1]["handoff"]
        assert card["reason"] == "price_request" and card["products"][0]["unitPriceKopecks"] is None
    else:
        assert events[-1]["purpose"] == "answer" and "250 ₽" in events[-1]["text"]


@pytest.mark.parametrize(
    "question",
    [
        "А сколько стоит мойофис?",
        "Какой срок у компас-3d?",
        "Сколько стоит лицензия на мойофис?",
        "Сколько стоит Новатор?",
    ],
)
def test_lowercase_new_product_never_inherits_old_price_or_handoff(stand_api, index, question):  # noqa: F811
    cid = stand_api.new_conversation()
    say(stand_api, cid, "SKU desk")
    response = say(stand_api, cid, question)[-1]
    assert "790" not in str(response) and "/desk" not in str(response)
    assert say(stand_api, cid, "Позовите оператора")[-1]["handoff"]["products"] == []
