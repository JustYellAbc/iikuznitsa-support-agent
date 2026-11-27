"""Regression cases found during the buyer, manager and recovery review of T16."""

import xml.etree.ElementTree as ET

import pytest
from catalog.index import KNOWLEDGE

from app.config import get_settings
from app.db import repository
from app.services.feed_refresh import refresh
from tests.test_feed_refresh import LIMITS, write_feed
from tests.test_quotes import command, last_quote, request, setup_quote


def test_mixed_terms_or_seats_are_not_customer_name(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    for qualifier in ("на 40 пользователей", "с постоплатой", "для тендера"):
        api.say(cid, f"Пришлите КП {qualifier} для ООО «Тест», ИНН 7700000001, КПП 770001001")
        api.idle()
    assert not any(e["data"]["type"] == "quote" for e in api.events(cid))
    api.login()
    reasons = {h["reason"] for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]}
    assert {"special_terms", "tender"} <= reasons


def test_quote_moderation_blocks_both_pdf_paths_and_confirmation(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    api.login()
    api.client.portal.call(repository.set_conversation_mode, cid, "pilot")
    api.say(cid, "Пришлите КП для ООО «Проверка», ИНН 7700000001, КПП 770001001")
    api.idle()
    assert all(e["data"]["type"] != "quote" for e in api.events(cid))
    [d] = api.call("manager", "GET", "/api/v1/manager/drafts").json()
    q = d["events"][0]["quote"]
    preview = f"/api/v1/conversations/{cid}/quotes/{q['quoteId']}/1/preview.pdf"
    assert api.call("customer", "GET", preview).status_code == 404
    assert api.call("manager", "GET", preview).status_code == 200
    assert command(api, cid, q, "confirm_quote").json()["code"] == "terms_pending"
    assert api.call("manager", "POST", f"/api/v1/manager/drafts/{d['id']}/send", json={}).status_code == 200
    assert last_quote(api, cid)["state"] == "ready"
    assert api.call("customer", "GET", preview).status_code == 404
    act = {"type": "request_document", "quoteId": q["quoteId"], "revision": q["revision"], "kind": "quote_pdf"}
    assert api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json=act).status_code == 202
    assert api.call("customer", "GET", preview).status_code == 200


def test_quote_receipt_rolls_back_together_with_revision(api, tmp_path, monkeypatch):
    cid = setup_quote(api, tmp_path, ["t1"])
    original = repository.save_idempotent

    async def fail(*args, **kwargs):
        raise RuntimeError("receipt interrupted")

    monkeypatch.setattr(repository, "save_idempotent", fail)
    with pytest.raises(RuntimeError, match="receipt interrupted"):
        request(api, cid, [("t1", 1)], key="retry")
    monkeypatch.setattr(repository, "save_idempotent", original)
    assert request(api, cid, [("t1", 1)], key="retry").status_code == 202
    assert [e["data"]["quote"]["revision"] for e in api.events(cid) if e["data"]["type"] == "quote"] == [1]


@pytest.mark.parametrize(
    "resize,confirm",
    [
        ("Теперь нужно 40 мест для обоих продуктов.", False),
        ("Обновите все строки на 40 мест.", False),
        ("Уточнили объём: пересчитайте прежний комплект на 40 пользователей (включая модуль).", True),
    ],
)
def test_seats_change_all_licenses_and_preserve_pack_quantities(api, tmp_path, resize, confirm):
    pairs = [
        ("sv20", 20, "Server", 100),
        ("sv40", 40, "Server", 150),
        ("cn20", 20, "Connector", 50),
        ("cn40", 40, "Connector", 75),
    ]
    feed = write_feed(
        tmp_path / "seats.xml",
        "2026-11-25 10:00",
        [(id, "true", str(price), f"{name}, {seats} пользователей") for id, seats, name, price in pairs],
    )
    xml = ET.parse(feed)
    for node, (_, seats, name, _) in zip(xml.findall(".//offer"), pairs, strict=True):
        node.set("group_id", name)
        ET.SubElement(node, "vendor").text = "Тест"
        ET.SubElement(node, "param", name="К-во пользователей до").text = str(seats)
    xml.write(feed, encoding="utf-8")
    refresh(feed, get_settings().catalog_index_path, KNOWLEDGE, LIMITS)
    cid = api.new_conversation()
    for sku in ("sv20", "cn20"):
        api.say(cid, f"Артикул {sku}")
        api.idle()
    assert request(api, cid, [("sv20", 1), ("cn20", 2)]).status_code == 202
    api.say(cid, resize)
    if confirm:
        api.idle()
        assert last_quote(api, cid)["revision"] == 1
        assert "На 40 пользователей" in api.events(cid)[-1]["data"]["text"]
        api.say(cid, "На 40 пользователей")
    api.idle()
    q = last_quote(api, cid)
    assert (q["revision"], q["knownSubtotalKopecks"]) == (2, 30000)
    assert [(r["offerId"], r["packages"]) for r in q["lines"]] == [("sv40", 1), ("cn40", 2)]
    api.say(cid, "Измените КП на 20 пользователей для ООО «Проверка», ИНН 7700000001, КПП 770001001")
    api.idle()
    revised = last_quote(api, cid)
    assert [(r["offerId"], r["packages"]) for r in revised["lines"]] == [("sv20", 1), ("cn20", 2)]
    assert command(api, cid, q, "confirm_quote").json()["code"] == "stale_revision"
    api.say(cid, "Теперь нужно 45 мест для обоих продуктов.")
    api.idle()
    assert command(api, cid, revised, "confirm_quote").json()["code"] == "stale_revision"


def test_form_requires_fresh_catalog_selection_after_unmatched_seat_count(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    assert request(api, cid, [("t1", 1)]).status_code == 202
    revised = last_quote(api, cid)
    assert api.say(cid, "Теперь нужно 37 мест").status_code == 202
    api.idle()
    current_url = f"/api/v1/conversations/{cid}/quotes/current"
    form = api.call("customer", "GET", current_url).json()
    assert form["requiresReselection"] and form["items"] == form["choices"] == []
    assert request(api, cid, [("t1", 1)], revised["revision"]).status_code == 422
    doc = {
        "type": "request_document",
        "kind": "quote_pdf",
        "quoteId": revised["quoteId"],
        "revision": revised["revision"],
    }
    assert api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json=doc).status_code == 409
    assert last_quote(api, cid) == revised
    assert api.say(cid, "Артикул t2").status_code == 202
    api.idle()
    form = api.call("customer", "GET", current_url).json()
    assert form["requiresReselection"] and form["items"] == []
    assert [item["offerId"] for item in form["choices"]] == ["t2"]
    assert request(api, cid, [("t2", 1)], revised["revision"]).status_code == 202
    form = api.call("customer", "GET", current_url).json()
    assert not form["requiresReselection"] and form["items"] == [{"offerId": "t2", "packages": 1}]
    assert [item["offerId"] for item in form["choices"]] == ["t2"]


def test_open_special_terms_block_form_confirmation_and_issue(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    assert request(api, cid, [("t1", 1)]).status_code == 202
    q = last_quote(api, cid)
    api.say(cid, "Пришлите КП с постоплатой для ООО «Проверка», ИНН 7700000001, КПП 770001001")
    api.idle()
    assert request(api, cid, [("t1", 2)], 1).json()["code"] == "terms_pending"
    assert command(api, cid, q, "confirm_quote").json()["code"] == "terms_pending"
    act = {"type": "request_document", "kind": "quote_pdf", "quoteId": q["quoteId"], "revision": q["revision"]}
    assert (
        api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json=act).json()["code"] == "terms_pending"
    )


def test_removed_line_does_not_accept_replies_from_either_role(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1", "t2"])
    assert request(api, cid, [("t1", 1), ("t2", 1)]).status_code == 202
    api.login()
    [case] = [h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h.get("quoteLine")]
    assert request(api, cid, [("t1", 1)], 1).status_code == 202
    assert api.say(cid, "Новый вопрос", handoffId=case["handoffId"]).json()["code"] == "stale_revision"
    api.say(cid, "Теперь 3 упаковки")
    api.idle()
    q = last_quote(api, cid)
    assert q["revision"] == 3 and [(r["offerId"], r["packages"]) for r in q["lines"]] == [("t1", 3)]
    api.say(cid, "Какая цена?")
    api.idle()
    assert "100 ₽" in api.events(cid)[-1]["data"]["text"]
    assert (
        api.call(
            "manager",
            "POST",
            f"/api/v1/manager/handoffs/{case['handoffId']}/resolution",
            json={"type": "reply", "reply": "Получил"},
        ).json()["code"]
        == "stale_revision"
    )


def test_manager_free_text_cannot_reconfirm_general_terms(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    request(api, cid, [("t1", 1)])
    q = last_quote(api, cid)
    api.say(cid, "Нужна постоплата")
    api.idle()
    api.login()
    [h] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (
        api.call(
            "manager",
            "POST",
            f"/api/v1/manager/handoffs/{h['handoffId']}/resolution",
            json={"type": "close", "reply": "Согласована постоплата через 10 дней."},
        ).status_code
        == 202
    )
    assert command(api, cid, q, "confirm_quote").json()["code"] == "stale_revision"
    assert request(api, cid, [("t1", 1)], 1).status_code == 202
    new = last_quote(api, cid)
    assert new["revision"] == 2 and new["state"] == "needs_review" and new["terms"]["payment"] is None
