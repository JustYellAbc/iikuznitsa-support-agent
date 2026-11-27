"""T16 through the customer and manager HTTP interfaces."""

import json
from datetime import date, timedelta

import pytest
from catalog.index import KNOWLEDGE

from app.config import get_settings
from app.services.feed_refresh import refresh
from tests.test_feed_refresh import BASE, LIMITS, write_feed

COMPANY = {"company": "ООО «Проверка»", "inn": "7700000001", "kpp": "770001001"}


def test_chat_builds_and_recalculates_single_sku(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"])
    for text, revision, packs in [
        ("Пришлите КП для ООО «Проверка», ИНН 7700000001, КПП 770001001", 1, 1),
        ("Теперь 3 упаковки", 2, 3),
    ]:
        accepted = api.say(cid, text)
        assert accepted.status_code == 202, accepted.text
        api.idle()
        q = last_quote(api, cid)
        assert (q["revision"], q["lines"][0]["packages"], q["knownSubtotalKopecks"]) == (revision, packs, 10000 * packs)
        assert any(e["data"].get("text") == text for e in api.events(cid))


def setup_quote(api, tmp_path, ids, *, rules=False):
    source = write_feed(tmp_path / "feed.xml", "2026-11-25 10:00", [*BASE, ("x", "true", "0", "Проверка лицензии")])
    refresh(source, get_settings().catalog_index_path, KNOWLEDGE, LIMITS)
    if rules:
        path = tmp_path / "config/quote_terms.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    i: {
                        "article": "SUP-" + i,
                        "source": "Условия тестовой сделки",
                        "payment": "Предоплата",
                        "delivery": "Тестовая электронная поставка",
                        "vatRate": "10",
                        "validUntil": (date.today() + timedelta(days=5)).isoformat(),
                    }
                    for i in ids
                }
            )
        )
    cid = api.new_conversation()
    for sku in ids:
        assert api.say(cid, f"Артикул {sku}").status_code == 202
        api.idle()
    return cid


def request(api, cid, items, revision=0, **kwargs):
    return api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/quotes",
        json={
            "baseRevision": revision,
            "customer": COMPANY,
            "items": [{"offerId": i, "packages": q} for i, q in items],
        },
        **kwargs,
    )


def last_quote(api, cid):
    return [e["data"]["quote"] for e in api.events(cid) if e["data"]["type"] == "quote"][-1]


def command(api, cid, q, kind, **kwargs):
    return api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/actions",
        json={"type": kind, "quoteId": q["quoteId"], "revision": q["revision"], **kwargs},
    )


def test_draft_history_access_identity_and_selection(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"])
    api.say(cid, "Пришлите КП")
    api.idle()
    assert "упаковок" in api.events(cid)[-1]["data"]["text"]
    assert request(api, cid, [("t3", 1)]).status_code == 422
    first = request(api, cid, [("t1", 2)], key="first")
    assert first.status_code == 202, first.text
    assert request(api, cid, [("t1", 2)], key="first").json() == first.json()
    assert request(api, cid, [("t1", 2)], 1).json() == first.json()
    q = last_quote(api, cid)
    assert (q["knownSubtotalKopecks"], q["totalKopecks"], q["state"]) == (20000, None, "needs_review")
    path = f"/api/v1/conversations/{cid}/quotes/{q['quoteId']}/1/preview.pdf"
    pdf = api.call("customer", "GET", path)
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-")
    api.new_conversation("other")
    assert api.call("other", "GET", path).status_code == 404
    assert command(api, cid, q, "confirm_quote").json()["code"] == "terms_pending"
    assert request(api, cid, [("t1", 3)], 1).status_code == 202
    assert request(api, cid, [("t1", 4)], 1).json()["code"] == "stale_revision"
    assert last_quote(api, cid)["knownSubtotalKopecks"] == 30000
    assert api.call("customer", "GET", path).content == pdf.content


def test_version_notes_compare_published_snapshots_and_keep_customer_fields_private(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1"])
    assert request(api, cid, [("t1", 1)]).status_code == 202
    customer = {**COMPANY, "company": "ООО Вторая компания", "kpp": "770002002"}
    for rev, packs in [(1, 1), (2, 3)]:
        r = api.call(
            "customer",
            "POST",
            f"/api/v1/conversations/{cid}/quotes",
            json={"baseRevision": rev, "customer": customer, "items": [{"offerId": "t1", "packages": packs}]},
        )
        assert r.status_code == 202, r.text
    url = f"/api/v1/conversations/{cid}/quotes/history"
    res = api.call("customer", "GET", url)
    v2, v3 = res.json()["revisions"]
    assert (v2["previous"], v2["revision"], v3["previous"], v3["revision"]) == (1, 2, 2, 3)
    assert v2["changes"] == ["Изменены реквизиты — название организации.", "Изменены реквизиты — КПП."]
    assert v3["changes"] == ["Позиция t1 · количество 1 → 3 уп."]
    assert customer["company"] not in res.text and customer["kpp"] not in res.text
    api.new_conversation("other")
    assert api.call("other", "GET", url).status_code == 404
    api.login()
    assert api.call(
        "manager", "POST", "/api/v1/manager/mode", json={"mode": "pilot", "conversation_id": cid}
    ).is_success
    assert request(api, cid, [("t1", 42)], 3).is_success
    assert api.call("customer", "GET", url).json() == res.json()


def test_quote_approval_publishes_new_revision_and_exact_document(api, tmp_path):
    cid = setup_quote(api, tmp_path, ["t1", "x"], rules=True)
    assert request(api, cid, [("t1", 2), ("x", 1)]).status_code == 202
    q = last_quote(api, cid)
    assert q["knownSubtotalKopecks"] == 20000 and q["lines"][1]["unitPriceKopecks"] is None
    api.login()
    [h] = [h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h.get("quoteLine")]
    decision = {"type": "approve_line_price", **h["quoteLine"], "unitPriceKopecks": 12015}
    path = f"/api/v1/manager/handoffs/{h['handoffId']}/resolution"
    assert api.call("manager", "POST", path, json=decision, key="approve").status_code == 202
    assert api.call("manager", "POST", path, json=decision, key="approve").status_code == 202
    latest = last_quote(api, cid)
    assert (latest["state"], latest["revision"], latest["totalKopecks"]) == ("ready", 2, 32015)
    assert command(api, cid, q, "confirm_quote").json()["code"] == "stale_revision"
    act = {"type": "request_document", "kind": "quote_pdf", "quoteId": q["quoteId"], "revision": 2}
    doc = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json=act)
    assert doc.status_code == 202
    issued = [e for e in api.events(cid) if e["data"]["type"] == "document"]
    assert len(issued) == 1 and issued[0]["actionId"] == doc.json()["actionId"]
    doc = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json=act)
    assert doc.status_code == 200
    assert (
        api.call("manager", "GET", doc.json()["url"]).content == api.call("customer", "GET", doc.json()["url"]).content
    )
    assert command(api, cid, latest, "confirm_quote").status_code == 202
    assert last_quote(api, cid)["state"] == "confirmed"
    assert request(api, cid, [("t1", 2), ("x", 3)], 2).status_code == 202
    changed = last_quote(api, cid)
    assert changed["state"] == "needs_review" and changed["lines"][1]["unitPriceKopecks"] is None


@pytest.mark.parametrize("sku,reason", [("t2", "price_request"), ("t3", "availability")])
def test_unavailable_stays_pending(api, tmp_path, sku, reason):
    cid = setup_quote(api, tmp_path, [sku], rules=True)
    assert request(api, cid, [(sku, 1)]).status_code == 202
    q = last_quote(api, cid)
    assert q["totalKopecks"] is None
    api.login()
    [h] = [h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h.get("quoteLine")]
    assert h["reason"] == reason
    res = api.call(
        "manager",
        "POST",
        f"/api/v1/manager/handoffs/{h['handoffId']}/resolution",
        json={"type": "approve_line_price", **h["quoteLine"], "unitPriceKopecks": 125},
    )
    if reason == "availability":
        assert res.json()["code"] == "terms_pending"
    else:
        assert res.status_code == 202 and last_quote(api, cid)["state"] == "needs_review"
        assert api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"][0]["reason"] == "availability"
