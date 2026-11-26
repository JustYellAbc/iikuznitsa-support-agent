"""Use the T06 importer and the public API; never seed product events in the conversation."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from catalog.index import KNOWLEDGE, build
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.catalog_selection import Selection, select, selection_for
from app.agent.handoff import OFFER_HELP
from app.config import get_settings


@pytest.fixture
def index(tmp_path):
    path = get_settings().data_dir / "private" / "catalog.sqlite3"
    shop = ET.Element("yml_catalog", date="2026-01-01")
    categories = ET.SubElement(shop, "categories")
    ET.SubElement(categories, "category", id="1").text = "PaperDesk"
    offers = ET.SubElement(shop, "offers")
    family = "MDaemon Messaging Server"
    for code, name, price, size, kind in [
        ("new20", family, "1234.56", "20", "Полная версия"),
        ("new5", family, "321", "5", "Полная версия"),
        ("renew", family + " продление действующей подписки", "875", "20", "Продление"),
        ("late", family + " продление просроченной подписки", "975", "20", "Продление"),
        ("desk", "PaperDesk Pro", "790", None, "Полная версия"),
        ("verify", "Widget Server", "120", None, "Полная версия"),
        ("ask", "Tool Suite", "0", None, "Полная версия"),
    ]:
        row = ET.SubElement(offers, "offer", id=code, available="false" if code in {"verify", "ask"} else "true")
        for key, value in {
            "name": name,
            "price": price,
            "currencyId": "RUB",
            "categoryId": "1",
            "url": "https://example.org/store/" + code,
        }.items():
            ET.SubElement(row, key).text = value
        for key, value in {
            "К-во пользователей до": size,
            "Тип лицензии": kind,
            "Пользователь": "Юрлицо",
            "Срок действия": "12 мес.",
        }.items():
            if value:
                ET.SubElement(row, "param", name=key).text = value
    feed = tmp_path / "test.xml"
    ET.ElementTree(shop).write(feed, encoding="utf-8")
    build(feed, path, KNOWLEDGE)
    return path


def say(api, cid, message):
    receipt = api.say(cid, message)
    assert receipt.status_code == 202
    api.idle()
    return [e["data"] for e in api.events(cid) if e["actionId"] == receipt.json()["actionId"]]


def test_selection_survives_multiple_turns_and_reaches_manager(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    first = say(api, cid, "Нужен Мдеймон на 20 человек, лицензия навсегда?")[-1]
    assert first["purpose"] == "clarification" and first["sources"]
    assert "физлицо" in first["text"] and "впервые" in first["text"] and "сколько пользователей" not in first["text"]
    second = say(api, cid, "Оформляем на компанию")[-1]
    assert "впервые" in second["text"] and "физлицо" not in second["text"]
    _, cards, answer = say(api, cid, "Покупаем впервые")
    [p] = cards["products"]
    assert (p["offerId"], p["unitPriceKopecks"], p["licenseTerm"]) == ("new20", 123456, "12 мес.")
    assert (
        answer["sources"] == [p["source"]] and "1 234,56 ₽" in answer["text"] and "проверки условий" in answer["text"]
    )
    say(api, cid, "Закупка тендерная, нужны документы")
    api.login()
    [handoff] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (handoff["conversationId"], handoff["reason"], handoff["products"]) == (cid, "tender", [p])


@pytest.mark.parametrize("status,offer", [("ещё действует", "renew"), ("уже истекла", "late")])
def test_renewals_require_subscription_status(stand_api, index, status, offer):  # noqa: F811
    cid = stand_api.new_conversation()
    assert "истекла" in say(stand_api, cid, "Продление MDaemon на 20 мест для юрлица")[-1]["text"]
    _, cards, _ = say(stand_api, cid, status)
    assert cards["products"][0]["offerId"] == offer


def test_new_unsuccessful_search_clears_handoff_context(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    assert say(api, cid, "SKU desk")[1]["products"][0]["offerId"] == "desk"
    assert say(api, cid, "Ищу AbsentMaker")[-1]["text"] == OFFER_HELP
    res = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    assert res.status_code == 202
    api.login()
    [case] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert case["products"] == []


def test_approximate_name_is_confirmed_before_selection(stand_api, index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    initial = say(api, cid, "Подберите PaperDsek")
    assert initial[-1]["purpose"] == "clarification" and "paperdesk" in initial[-1]["text"]
    assert all(e["type"] != "products" for e in initial)
    assert say(api, cid, "Да")[1]["products"][0]["offerId"] == "desk"


def test_standard_and_unsupported_constraints(index):
    first = selection_for("MDaemon Standard на 20 мест", None)
    assert "не подтверждено" in select(index, first).text
    for text in ["Покупаем впервые как физлицо", "Компания, на 36 месяцев", "Нужна редакция Ultimate"]:
        answer = select(index, first.qualifiers(text, followup=True))
        assert answer.text == OFFER_HELP and not answer.products
    assert select(Path(index).with_suffix(".missing"), first).text == OFFER_HELP


def test_need_and_named_request_stay_distinct():
    assert selection_for("Нужен почтовый сервер для 20 человек на Windows", None) is None
    assert selection_for("Нужна лицензия на КОМПАС-3D", None).name == "КОМПАС-3D"
    assert selection_for("компас", None).name == "компас"
    narrowed = selection_for("V25", Selection("КОМПАС-3D"))
    assert (narrowed.name, narrowed.edition) == ("КОМПАС-3D", "v25")


def test_ordinal_option_preserves_stated_buyer():
    selected = selection_for("Второй вариант", Selection("PaperDesk", buyer="Юрлицо"), options=["aa", "bb"])
    assert (selected.name, selected.code, selected.buyer) == ("bb", True, "Юрлицо")


@pytest.mark.parametrize(
    "name,state,amount", [("Widget Server", "availability_check", 12000), ("Tool Suite", "on_request", None)]
)
def test_independent_product_families_use_feed_price_states(index, name, state, amount):
    [p] = select(index, Selection(name)).products
    assert (p.priceState, p.unitPriceKopecks) == (state, amount)
