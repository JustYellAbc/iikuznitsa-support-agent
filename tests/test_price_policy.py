"""T14: политика состояния цены и доступности. Синтетика и три эталонных SKU из реального снимка."""

import json
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest
from catalog.index import KNOWLEDGE

from app.contracts.t04 import Product
from app.services import price_policy
from app.services.catalog_snapshot import read_offer
from app.services.feed_refresh import Limits, refresh
from app.services.price_policy import OfferSnapshot, assess, classify, load_availability_rule

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
    # Цена снимка в продукте T04 остаётся: priceState=availability_check требует unitPriceKopecks
    assert a.to_product().unitPriceKopecks == 10000


@pytest.mark.parametrize(
    "patch",
    [{"allow_auto_invoice": "no"}, {"policy_source": " "}, {"extra": True}],
)
def test_broken_policy_file_is_rejected(tmp_path, patch):
    data = json.loads(price_policy.POLICY_PATH.read_text(encoding="utf-8"))
    data["availability_check"].update(patch)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_availability_rule(path)


@pytest.mark.parametrize(
    "terms,expected",
    [
        (("Не определено",), None),
        (("мес.",), None),
        ((), None),
        (("12 мес.",), "12 мес."),
        (("12 мес.", "12 мес."), "12 мес."),
        (("12 мес.", "24 мес."), None),
    ],
)
def test_confirmed_term(terms, expected):
    assert price_policy.confirmed_term(terms) == expected


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
    # Один потоковый проход по фиду: берём дату выгрузки и три эталонных оффера в мини-фид
    root = shop = offers = None
    for event, node in ET.iterparse(FEED, events=("start", "end")):
        if event == "start":
            if root is None:
                root = ET.Element("yml_catalog", date=node.attrib["date"])
                shop = ET.SubElement(root, "shop")
                offers = ET.SubElement(shop, "offers")
            continue
        if node.tag == "offer":
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
