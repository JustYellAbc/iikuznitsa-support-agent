"""Use the T06 importer and the public API; never seed product events in the conversation."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from catalog.index import KNOWLEDGE, build
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.catalog_selection import Selection, select, selection_for
from app.agent.handoff import OFFER_HELP, offers_operator
from app.config import get_settings


def test_quote_name_does_not_hide_an_actual_customer_type():
    assert not Selection("").qualifiers("Нужна копия коммерческого предложения.", followup=False).organization
    assert (
        Selection("").qualifiers("Коммерческое предложение для коммерческой компании.", followup=False).organization
        == "Коммерческая"
    )


@pytest.fixture
def index(tmp_path, request):
    path = get_settings().data_dir / "private" / "catalog.sqlite3"
    shop = ET.Element("yml_catalog", date="2026-01-01")
    categories = ET.SubElement(shop, "categories")
    ET.SubElement(categories, "category", id="1").text = "PaperDesk"
    offers = ET.SubElement(shop, "offers")
    family = "MDaemon Messaging Server"
    extra_offers = []
    if getattr(request, "param", None) == "resize":
        extra_offers = [
            ("19578", family, "94341", "40", "Полная версия"),
            ("380385", "Windows 11 Pro", "30448", None, "Полная версия"),
        ]
    for code, name, price, size, kind in [
        *extra_offers,
        ("new20", family, "1234.56", "20", "Полная версия"),
        ("new5", family, "321", "5", "Полная версия"),
        ("renew", family + " продление подписки на обновления", "875", "20", "Продление"),
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
            "Версия": "Pro 9" if code == "desk" else None,
            "Способ доставки": "Электронный" if code == "desk" else None,
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
    assert offers_operator(answer["text"])
    [ref] = answer["sources"]
    assert ref["url"] == p["source"]["url"] and "12 мес" in answer["text"] and "право использования" in answer["text"]
    say(api, cid, "Закупка тендерная, нужны документы")
    api.login()
    [handoff] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (handoff["conversationId"], handoff["reason"], handoff["products"]) == (cid, "tender", [p])


@pytest.mark.parametrize(
    ("change", "combined", "expected"),
    [
        ("Продлеваем, подписка действует.", True, ("renew", 87500)),
        ("Теперь нужно продление действующей подписки.", False, ("renew", 87500)),
        ("Теперь нужно продление действующей подписки.", True, ("renew", 87500)),
        ("Продлеваем, подписка действует.", False, ("renew", 87500)),
        ("Продлеваем, подписка уже не действует.", True, ("late", 97500)),
        ("Оформляем на физлицо.", True, None),
        ("Оформляем на физлицо.", False, None),
        ("Продлеваем.", True, None),
        ("Нужен PaperDesk.", True, ("desk", 79000)),
        ("Нужен Новатор.", True, None),
        ("Нужно продление лицензии на Dr.Web, подписка действует.", True, None),
        ("Нужно продление для Adobe InCopy, подписка действует.", True, None),
        ("Продлеваем, подписка действует. Это тендер.", True, ("renew", 87500)),
        ("Нужен Adobe InCopy для тендера.", True, None),
        ("Закупаем по тендеру. Пришлите пакет документов. Обращение T11-7433510c.", True, ("new20", 123456)),
        ("Закупаем по тендеру. Обращение T11-7433510c, нужен PaperDesk.", True, ("desk", 79000)),
        ("Продлеваем для тендера, подписка действует. Спасибо.", True, ("renew", 87500)),
    ],
)
def test_changed_terms_in_manager_card(stand_api, index, change, combined, expected):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    say(api, cid, "Нужен MDaemon на 20 пользователей")
    assert say(api, cid, "Компания, покупаем впервые.")[1]["products"][0]["offerId"] == "new20"
    call = "Позовите оператора."
    text = f"{change} {call}" if combined else change
    response = say(api, cid, text)
    if not combined:
        if expected:
            assert response[-1]["purpose"] == "answer"
        response = say(api, cid, call)
    assert response[-1]["type"] == "handoff"
    api.login()
    [card] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    reason = "tender" if "тендер" in change else "customer_request"
    assert (card["conversationId"], card["reason"], card["question"]) == (cid, reason, text)
    assert [(p["offerId"], p["unitPriceKopecks"]) for p in card["products"]] == ([expected] if expected else [])
    if combined and expected is None:
        close = f"/api/v1/manager/handoffs/{card['handoffId']}/resolution"
        assert api.call("manager", "POST", close, json={"type": "close", "reply": "Проверили"}).status_code == 202
        assert (
            api.call(
                "customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"}
            ).status_code
            == 202
        )
        [reopened] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
        assert reopened["products"] == []


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


@pytest.mark.parametrize(
    "text,months,expired",
    [
        ("Сколько стоит лицензия на год?", 12, None),
        ("Покупаем на 24 месяца. На какой срок она действует?", 24, None),
        ("На какой срок лицензия действует — на 12 месяцев или бессрочно?", None, None),
        ("Подписка действует. Для продления варианты на год или бессрочно?", None, False),
    ],
)
def test_stated_conditions_survive_questions_about_alternatives(text, months, expired):
    selection = Selection("example").qualifiers(text, followup=True)
    assert (selection.months, selection.expired) == (months, expired)


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


def test_renewal_type_conflict_is_scoped_to_matching_platform_and_capacity(index):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(index)) as db:
        db.execute("UPDATE offers SET params=json_set(params, '$.Платформа', json_array('Linux'))")
        db.execute(
            "UPDATE offers SET name=name || ' продление просроченной подписки', "
            "params=json_set(params, '$.Платформа', json_array('Windows')) WHERE id='new5'"
        )
        db.execute("DELETE FROM offers WHERE id IN ('renew','late')")
        db.commit()
    for seats, platforms in [(20, ["Windows"]), (5, ["Linux"])]:
        hit = select(index, Selection("MDaemon Messaging Server", kind="Продление", seats=seats, platforms=platforms))
        assert hit.text == OFFER_HELP and not hit.products and not hit.sources
    hit = select(index, Selection("MDaemon Messaging Server", kind="Продление", seats=5, platforms=["Windows"]))
    assert "другой тип" in hit.text and all(source.url.endswith("/new5") for source in hit.sources)
    current = select(
        index, Selection("MDaemon Messaging Server", kind="Продление", seats=5, platforms=["Windows"], expired=False)
    )
    assert current.text == OFFER_HELP and not current.sources
