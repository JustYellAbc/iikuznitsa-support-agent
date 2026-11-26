import sqlite3
from xml.etree import ElementTree as ET

import pytest
from catalog.index import KNOWLEDGE, build
from tests.conftest import api as stand_api  # noqa: F401
from tests.conftest import client as client
from tests.conftest import env as env

from app.agent.handoff import OFFER_HELP
from app.agent.test_catalog_selection import say
from app.config import get_settings

START = "Хотим защитить почту и файлы для 50 сотрудников"
TERMS = (
    "Коммерческое юрлицо, покупаем впервые на год. Почта на Postfix, 50 адресов. "
    "Файлы на одном Windows Server 2019. Доступна отдельная Windows-машина. Бюджет до 100 000 ₽."
)


@pytest.fixture
def need_index(tmp_path):
    root = ET.Element("yml_catalog", date="2026-01-01")
    ET.SubElement(ET.SubElement(root, "categories"), "category", id="1").text = "ПО"
    items = ET.SubElement(root, "offers")
    examples = [
        ("mail", "MDaemon Messaging Server", "1200", 20, "Windows", ""),
        ("gw", "Security Gateway for Email Servers", "500", 50, "Windows", ""),
        (
            "addr",
            "Kaspersky Security для почтовых серверов базовая лицензия по количеству почтовых адресов",
            "12.19",
            None,
            "Linux",
            "Кол-во от 50 до 99 шт.",
        ),
        (
            "files",
            "Dr.Web Server Security Suite Антивирус на 1 устройство",
            "99",
            1,
            "Windows Server 2019",
            "Кол-во от 1 до 1 шт.",
        ),
    ]
    for sku, name, price, capacity, os, sales in examples:
        offer = ET.SubElement(items, "offer", id=sku, available="true")
        for k, v in {
            "name": name,
            "description": "Платформа: " + os,
            "url": "https://example.org/" + sku,
            "price": price,
            "categoryId": "1",
            "currencyId": "RUB",
            "sales_notes": sales,
        }.items():
            ET.SubElement(offer, k).text = v
        for k, v in {
            "Срок действия": "12 мес.",
            "Тип лицензии": "Базовая" if sku == "files" else "Полная версия",
            "Пользователь": "Юрлицо",
            "Тип организации": "Коммерческая",
            "Способ доставки": "Электронный",
            "К-во пользователей до": str(capacity) if capacity else "",
        }.items():
            ET.SubElement(offer, "param", name=k).text = v
    xml = tmp_path / "needs.xml"
    ET.ElementTree(root).write(xml, encoding="utf-8")
    path = get_settings().data_dir / "private/catalog.sqlite3"
    build(xml, path, KNOWLEDGE)
    return path


def test_mailbox_intake_before_selection_and_tender(stand_api, need_index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    opening = say(api, cid, "Нужно что-то для корпоративной почты")
    assert "новые почтовые ящики" in opening[-1]["text"] and all(e["type"] != "products" for e in opening)
    answer = say(api, cid, "Коммерческое юрлицо, покупаем впервые. Заводим почту на Windows, 20 пользователей.")
    assert [p["offerId"] for p in answer[-2]["products"]] == ["mail"]
    assert "1 200 ₽" in answer[-1]["text"] and len(answer[-1]["sources"]) == 2
    card = say(api, cid, "Закупка тендерная. Пришлите документы.")[-1]["handoff"]
    assert card["reason"] == "tender" and [p["offerId"] for p in card["products"]] == ["mail"]


def test_compare_recalculate_and_select(stand_api, need_index):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    first = say(api, cid, START)[-1]["text"]
    assert all(word in first for word in ("адресов", "серверах", "бюджета"))
    answer = say(api, cid, TERMS)
    assert {p["offerId"] for p in answer[-2]["products"]} == {"addr", "gw", "files"}
    assert "599 ₽" in answer[-1]["text"] and "708,50 ₽" in answer[-1]["text"]
    with sqlite3.connect(need_index) as db:
        db.execute("UPDATE offers SET price='20' WHERE id='addr'")
    changed = say(api, cid, "Отдельную Windows-машину не предоставим.")
    assert "599 ₽" not in changed[-1]["text"] and "1 099 ₽" in changed[-1]["text"]
    assert {p["offerId"] for p in changed[-2]["products"]} == {"addr", "files"}
    russian = say(api, cid, "А есть российский аналог шлюза?")[-1]
    assert "конкретной поставки" in russian["text"] and any("registry" in s["url"] for s in russian["sources"])


@pytest.mark.parametrize(
    "change,expected",
    [
        ("Предел бюджета до 605 ₽", {"gw", "files"}),
        ("Берём второй вариант", {"addr", "files"}),
        ("Файлы будут на Linux", set()),
        ("Бюджет до 99 ₽", set()),
    ],
)
def test_card_matches_latest_viable_configuration(stand_api, need_index, change, expected):  # noqa: F811
    api = stand_api
    cid = api.new_conversation()
    say(api, cid, START)
    say(api, cid, TERMS)
    response = say(api, cid, change)[-1]
    assert response["purpose"] == ("answer" if expected else "clarification")
    card = say(api, cid, "Позовите оператора")[-1]["handoff"]
    assert {p["offerId"] for p in card["products"]} == expected


def test_missing_file_coverage_cannot_produce_full_bundle(stand_api, need_index):  # noqa: F811
    with sqlite3.connect(need_index) as db:
        db.execute("UPDATE offers SET available=0, price_state='verify_availability' WHERE id='files'")
    cid = stand_api.new_conversation()
    say(stand_api, cid, START)
    answer = say(stand_api, cid, TERMS)
    assert answer[-1]["text"] == OFFER_HELP and "файловая лицензия" in answer[-1]["sources"][0]["label"]
    assert all(e["type"] != "products" for e in answer)
