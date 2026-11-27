"""T14: политика состояния цены и доступности. Синтетика и три эталонных SKU из реального снимка."""

import json
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest
from catalog.index import KNOWLEDGE
from fastapi.testclient import TestClient

from app.contracts.t04 import Product
from app.services import price_policy
from app.services.catalog_snapshot import read_offer
from app.services.feed_refresh import Limits, refresh
from app.services.price_policy import OfferSnapshot, assess, classify, load_availability_rule
from tests.conftest import Api

FEED = Path(__file__).resolve().parents[1] / "data" / "raw" / "Mindbox.yml.gz.xml"


def snapshot(available, price, terms=("Не определено",), offer_id="1"):
    return OfferSnapshot(
        offer_id=offer_id,
        name="Тестовый товар",
        url=f"https://store.softline.ru/test/-{offer_id}/",
        available=available,
        price=price,
        currency="RUR",
        license_terms=terms,
        snapshot_date="2026-11-25 10:00",
    )


@pytest.mark.parametrize(
    "available,price,state",
    [
        (True, "4900", "listed"),
        (False, "0", "on_request"),
        (True, "0", "on_request"),
        (None, "0.00", "on_request"),
        (False, "", "on_request"),
        (False, None, "on_request"),
        (False, "abc", "on_request"),
        (False, "1523654", "availability_check"),
        (None, "100", "availability_check"),
    ],
)
def test_classify_matches_t04(available, price, state):
    assert classify(available, price) == state
    product = assess(snapshot(available, price)).to_product()
    assert product.priceState == state
    if price != "abc":  # нечисловую цену from_catalog не принимает, политика считает её «по запросу»
        assert Product.from_catalog("1", "x", "https://x.ru/", bool(available), price).priceState == state


def test_listed_price_is_shown_and_undefined_term_is_not_invented():
    a = assess(snapshot(True, "4900"))
    assert (a.price_state, a.unit_price_kopecks, a.show_price) == ("listed", 490000, True)
    assert a.license_term is None and a.to_product().licenseTerm is None
    assert a.handoff_reason is None and a.price_caveat is None


def test_on_request_never_exposes_zero_price():
    a = assess(snapshot(False, "0", ("12 мес.",)))
    assert (a.price_state, a.unit_price_kopecks, a.show_price) == ("on_request", None, False)
    assert not a.allow_quote_draft and not a.allow_auto_invoice
    assert a.handoff_reason == "price_request"
    assert a.to_product().unitPriceKopecks is None


def test_availability_check_follows_policy_file():
    a = assess(snapshot(False, "1523654"))
    assert (a.price_state, a.unit_price_kopecks, a.show_price) == ("availability_check", 152365400, True)
    assert "2026-11-25 10:00" in a.price_caveat and "менеджер" in a.price_caveat
    assert not a.allow_quote_draft and not a.allow_auto_invoice
    assert a.handoff_reason == "availability"
    rule = load_availability_rule()
    assert not rule.confirmed and "evals/t03/README.md" in rule.policy_source
    assert a.policy_source == rule.policy_source


def test_policy_switch_after_customer_answer(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "availability_check": {
                    "show_snapshot_price_with_caveat": False,
                    "allow_quote_draft": True,
                    "allow_auto_invoice": False,
                    "confirmed": True,
                    "policy_source": "Q&A с заказчиком",
                }
            }
        ),
        encoding="utf-8",
    )
    a = assess(snapshot(False, "100"), load_availability_rule(path))
    assert (a.show_price, a.price_caveat, a.allow_quote_draft, a.policy_source) == (
        False,
        None,
        True,
        "Q&A с заказчиком",
    )
    assert a.unit_price_kopecks == 10000
    # Когда показ цены выключен, покупательская карточка T04 скрывает сумму (unitPriceKopecks=None, on_request)
    product = a.to_product()
    assert (product.priceState, product.unitPriceKopecks) == ("on_request", None)


@pytest.mark.parametrize("currency", ["USD", "EUR", "", None])
def test_unsupported_currency_blocks_ruble_pricing_and_documents(currency):
    raw = OfferSnapshot(
        offer_id="usd-1",
        name="USD License",
        url="https://store.softline.ru/test/-usd-1/",
        available=True,
        price="100",
        currency=currency,
        license_terms=("12 мес.",),
        snapshot_date="2026-11-25 10:00",
    )
    a = assess(raw)
    # Исходные данные снимка сохраняются без искажений
    assert a.snapshot.price == "100"
    assert a.snapshot.currency == currency
    # Рублёвый расчёт и документы заблокированы
    assert (a.price_state, a.unit_price_kopecks, a.show_price) == ("on_request", None, False)
    assert not a.allow_quote_draft and not a.allow_auto_invoice
    assert a.handoff_reason == "price_request"
    assert a.to_product().unitPriceKopecks is None


@pytest.mark.parametrize("currency", ["RUB", "RUR", "rub"])
def test_supported_ruble_currencies_allow_pricing_and_documents(currency):
    raw = OfferSnapshot(
        offer_id="rub-1",
        name="RUB License",
        url="https://store.softline.ru/test/-rub-1/",
        available=True,
        price="100",
        currency=currency,
        license_terms=("12 мес.",),
        snapshot_date="2026-11-25 10:00",
    )
    a = assess(raw)
    assert (a.price_state, a.unit_price_kopecks, a.show_price) == ("listed", 10000, True)
    assert a.allow_quote_draft and a.allow_auto_invoice


@pytest.mark.parametrize(
    "patch",
    [{"allow_auto_invoice": "no"}, {"policy_source": " "}, {"extra": True}],
)
def test_broken_policy_file_is_rejected(tmp_path, patch):
    data = json.loads(price_policy.POLICY_PATH.read_text(encoding="utf-8"))
    data["availability_check"].update(patch)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(price_policy.PolicyError):
        load_availability_rule(path)


@pytest.mark.parametrize(
    "terms,expected",
    [
        (("Не определено",), None),
        (("мес.",), None),
        (("нет данных",), None),
        (("другое или отсутствует",), None),
        (("не указано",), None),
        ((), None),
        (("12 мес.",), "12 мес."),
        (("12 мес.", "12 мес."), "12 мес."),
        (("12 мес.", "нет данных"), "12 мес."),
        (("12 мес.", "другое или отсутствует"), "12 мес."),
        (("12 мес.", "не указано"), "12 мес."),
        (("12 мес.", "24 мес."), None),
    ],
)
def test_confirmed_term(terms, expected):
    assert price_policy.confirmed_term(terms) == expected
    a = assess(snapshot(True, "100", terms=terms))
    assert a.license_term == expected
    assert a.snapshot.license_terms == terms


# --- Эталонные SKU реального снимка (data/raw/ не в git: без фида тест пропускается) -------------

CONTROLS = {
    # available, цена XML, «Срок действия», путь карточки, состояние, копейки, показ цены, срок для покупателя
    "61557": (True, Decimal(4900), ["Не определено"], "sunrav/-61557/", "listed", 490000, True, None),
    "504": (False, Decimal(0), ["12 мес."], "adobe/65272676ba01a12-504/", "on_request", None, False, "12 мес."),
    "48093": (
        False,
        Decimal(1523654),
        ["Не определено"],
        "iceni-technology/-48093/",
        "availability_check",
        152365400,
        True,
        None,
    ),
}


@pytest.fixture(scope="module")
def real_index(tmp_path_factory):
    if not FEED.is_file():
        pytest.skip(f"нет реального фида {FEED}")
    # Один потоковый проход по фиду: дата выгрузки, категории и три эталонных оффера в мини-фид
    root = shop = offers = None
    for event, node in ET.iterparse(FEED, events=("start", "end")):
        if event == "start":
            if root is None:
                root = ET.Element("yml_catalog", date=node.attrib["date"])
                shop = ET.SubElement(root, "shop")
            continue
        if node.tag == "categories":  # офферы ссылаются на categoryId
            shop.append(node)
            offers = ET.SubElement(shop, "offers")
        elif node.tag == "offer":
            if node.get("id") in CONTROLS:
                offers.append(node)
                if len(offers) == len(CONTROLS):
                    break
            else:
                node.clear()
    directory = tmp_path_factory.mktemp("real")
    mini = directory / "controls.xml"
    ET.ElementTree(root).write(mini, encoding="utf-8")
    index = directory / "catalog.sqlite3"
    refresh(mini, index, KNOWLEDGE, Limits(min_offers=len(CONTROLS)))
    return index


@pytest.mark.parametrize("offer_id", CONTROLS)
def test_real_snapshot_controls(real_index, offer_id):
    available, price, terms, path, state, kopecks, show, term = CONTROLS[offer_id]
    offer = read_offer(real_index, offer_id)
    assert offer.available is available
    assert Decimal(offer.price) == price
    assert list(offer.license_terms) == terms
    assert offer.url == f"https://store.softline.ru/{path}"
    assert offer.snapshot_date
    a = assess(offer)
    assert (a.price_state, a.unit_price_kopecks, a.show_price, a.license_term) == (state, kopecks, show, term)
    assert a.allow_auto_invoice is (state == "listed")
    assert a.allow_quote_draft is (state == "listed")
    assert a.to_product().priceState == state


# --- Переопределение политики в $DATA_DIR без пересборки образа ------------------------------


def manager_policy(api: Api) -> dict:
    api.login()
    return api.call("manager", "GET", "/api/v1/manager/catalog").json()["availability_check"]


def write_override(data_dir: Path, text: str) -> Path:
    path = data_dir / "config" / "price_policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_override_in_data_dir_applies_after_restart(tmp_path):
    from app.main import create_app

    with TestClient(create_app()) as client:
        builtin = manager_policy(Api(client))
    assert builtin["loaded_from"] == str(price_policy.POLICY_PATH)
    assert (builtin["overridden"], builtin["confirmed"], builtin["allow_quote_draft"]) == (False, False, False)

    data = json.loads(price_policy.POLICY_PATH.read_text(encoding="utf-8"))
    data["availability_check"].update(allow_quote_draft=True, confirmed=True, policy_source="Q&A с заказчиком 27.11")
    override = write_override(tmp_path, json.dumps(data, ensure_ascii=False))
    # Без перезапуска действует политика, прочитанная при старте
    assert not assess(snapshot(False, "100")).allow_quote_draft

    with TestClient(create_app()) as client:
        status = manager_policy(Api(client))
        assert assess(snapshot(False, "100")).allow_quote_draft
    assert status["loaded_from"] == str(override)
    assert (status["overridden"], status["confirmed"], status["allow_quote_draft"]) == (True, True, True)
    assert status["policy_source"] == "Q&A с заказчиком 27.11"
    assert status["allow_auto_invoice"] is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{not json",
        "[]",
        '{"availability_check": {"show_snapshot_price_with_caveat": "true"}}',
        json.dumps({"availability_check": {"allow_auto_invoice": True}}),
    ],
)
def test_broken_override_stops_startup(tmp_path, text):
    from app.main import create_app

    override = write_override(tmp_path, text)
    with pytest.raises(price_policy.PolicyError, match=str(override)), TestClient(create_app()):
        pass


def test_buyer_views_hide_price_when_policy_disables_it(tmp_path):
    """При show_snapshot_price_with_caveat=false сумма скрыта в карточке, тексте и на «Какая цена?»."""
    from catalog.index import build

    from app.main import create_app

    data = json.loads(price_policy.POLICY_PATH.read_text(encoding="utf-8"))
    data["availability_check"].update(
        show_snapshot_price_with_caveat=False,
        confirmed=True,
        policy_source="Q&A: скрывать цену для недоступных SKU",
    )
    write_override(tmp_path, json.dumps(data, ensure_ascii=False))

    root = ET.Element("yml_catalog", date="2026-11-25 10:00")
    shop = ET.SubElement(root, "shop")
    ET.SubElement(ET.SubElement(shop, "categories"), "category", id="1").text = "ПО"
    offers = ET.SubElement(shop, "offers")
    row = ET.SubElement(offers, "offer", id="verify", available="false")
    for tag, value in {
        "name": "Widget Server",
        "price": "120",
        "currencyId": "RUR",
        "categoryId": "1",
        "url": "https://store.softline.ru/test/-verify/",
    }.items():
        ET.SubElement(row, tag).text = value
    ET.SubElement(row, "param", name="Срок действия").text = "12 мес."
    feed = tmp_path / "verify.xml"
    ET.ElementTree(root).write(feed, encoding="utf-8", xml_declaration=True)
    build(feed, tmp_path / "private" / "catalog.sqlite3", KNOWLEDGE)

    with TestClient(create_app()) as client:
        api = Api(client)
        assert manager_policy(api)["show_snapshot_price_with_caveat"] is False

        cid = api.new_conversation()
        r1 = api.say(cid, "Нужен Widget Server")
        assert r1.status_code == 202
        api.idle()
        ev1 = [e["data"] for e in api.events(cid) if e["actionId"] == r1.json()["actionId"]]
        product_events = [e for e in ev1 if e["type"] == "products"]
        assert product_events, ev1
        card = product_events[0]["products"][0]
        assert card["offerId"] == "verify"
        assert card["unitPriceKopecks"] is None
        assert card["priceState"] == "on_request"
        msg1 = [e["text"] for e in ev1 if e["type"] == "message" and e["author"] == "agent"]
        assert msg1 and "120" not in " ".join(msg1)
        assert "Цена по запросу" in " ".join(msg1)
        assert "Наличие требует подтверждения" in " ".join(msg1)

        r2 = api.say(cid, "Какая цена?")
        assert r2.status_code == 202
        api.idle()
        ev2 = [e["data"] for e in api.events(cid) if e["actionId"] == r2.json()["actionId"]]
        msg2 = [e["text"] for e in ev2 if e["type"] == "message" and e["author"] == "agent"]
        assert msg2 and "120" not in " ".join(msg2)
        assert "Цена по запросу" in " ".join(msg2)
        for ev in ev2:
            if ev["type"] == "products":
                assert all(p["unitPriceKopecks"] is None for p in ev["products"])
            elif ev["type"] == "handoff":
                assert all(p["unitPriceKopecks"] is None for p in ev["handoff"]["products"])


@pytest.mark.parametrize("show_price", [True, False])
@pytest.mark.parametrize(
    "question", ["Есть ли в наличии?", "Какая цена и есть ли в наличии?", "Доступен ли товар?", "Программа доступна?"]
)
def test_availability_handoff_keeps_question_sku_and_current_visibility(api, tmp_path, show_price, question):
    from tests.test_feed_refresh import LIMITS, write_feed

    data = json.loads(price_policy.POLICY_PATH.read_text(encoding="utf-8"))
    data["availability_check"]["show_snapshot_price_with_caveat"] = show_price
    write_override(tmp_path, json.dumps(data))
    price_policy.activate(tmp_path)
    refresh(
        write_feed(tmp_path / "stock.xml", "2026-11-25 10:00"), tmp_path / "private/catalog.sqlite3", KNOWLEDGE, LIMITS
    )
    cid = api.new_conversation()
    api.login()
    for message in ("Артикул t3", question):
        assert api.say(cid, message).status_code == 202
        api.idle()
    [card] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (card["question"], card["reason"]) == (question, "availability")
    [product] = card["products"]
    assert product["offerId"] == "t3" and product["unitPriceKopecks"] == (250050 if show_price else None)
    assert product["available"] is False
    if not show_price:
        events = json.dumps(api.events(cid), ensure_ascii=False)
        assert "250050" not in events and "2 500" not in events


@pytest.mark.parametrize(
    "sku,question",
    [
        ("t1", "Доступен ли товар?"),
        ("t3", "Можно ли расширить число доступных лицензий?"),
        ("t3", "Доступны ли обновления после покупки?"),
        ("t3", "Доступен ли перенос лицензии?"),
    ],
)
def test_stock_routing_requires_unconfirmed_stock_and_a_stock_question(api, tmp_path, sku, question):
    from tests.test_feed_refresh import LIMITS, write_feed

    refresh(
        write_feed(tmp_path / "stock.xml", "2026-11-25 10:00"), tmp_path / "private/catalog.sqlite3", KNOWLEDGE, LIMITS
    )
    cid = api.new_conversation()
    api.login()
    for message in (f"Артикул {sku}", question):
        assert api.say(cid, message).status_code == 202
        api.idle()
    assert api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] == []
    if sku == "t1":
        answer = api.events(cid)[-1]["data"]
        assert "наличии" in answer["text"].lower() and answer["sources"]
