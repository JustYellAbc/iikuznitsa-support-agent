from threading import Event

import pytest

from app.config import get_settings
from app.services import invoices
from tests.test_quotes import command, last_quote, request, setup_quote


def bill(api, cid, quote, key=None):
    return api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/actions",
        key=key,
        json={
            "type": "request_document",
            "kind": "demo_invoice",
            "quoteId": quote["quoteId"],
            "revision": quote["revision"],
        },
    )


def issued(api, cid):
    api.client.portal.call(invoices.wait_idle)
    return [
        e
        for e in api.events(cid)
        if e["data"]["type"] == "document" and e["data"]["document"]["kind"] == "demo_invoice"
    ]


def test_invoice_requires_accepted_current_terms_and_preserves_prior_download(api, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_session_msg_per_min", 50)
    cid = setup_quote(api, tmp_path, ["t1", "x"], rules=True)
    assert request(api, cid, [("t1", 2), ("x", 1)]).status_code == 202
    pending = last_quote(api, cid)
    assert bill(api, cid, pending).json()["code"] == "terms_pending"
    api.login()
    [case] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (
        api.call(
            "manager",
            "POST",
            f"/api/v1/manager/handoffs/{case['handoffId']}/resolution",
            json={"type": "approve_line_price", **case["quoteLine"], "unitPriceKopecks": 12015},
        ).status_code
        == 202
    )
    ready = last_quote(api, cid)
    assert ready["totalKopecks"] == 32015
    assert bill(api, cid, ready).json()["code"] == "confirmation_required"
    assert command(api, cid, ready, "confirm_quote").status_code == 202
    first = bill(api, cid, ready, "first")
    assert first.status_code == 202
    [event] = issued(api, cid)
    doc = event["data"]["document"]
    assert (event["actionId"], doc["quoteId"], doc["revision"]) == (first.json()["actionId"], ready["quoteId"], 2)
    replay = bill(api, cid, ready, "first")
    assert (replay.status_code, replay.json()) == (202, first.json())
    repeat = bill(api, cid, ready)
    assert (repeat.status_code, repeat.json()) == (200, doc)
    pdf = api.call("customer", "GET", doc["url"])
    assert pdf.content.startswith(b"%PDF") and "demo-invoice" in pdf.headers["Content-Disposition"]
    assert api.call("manager", "GET", doc["url"]).content == pdf.content
    api.new_conversation("other")
    assert api.call("other", "GET", doc["url"]).status_code == 404
    assert api.call("anonymous", "GET", doc["url"]).status_code == 401
    assert request(api, cid, [("t1", 3)], 2).status_code == 202
    assert bill(api, cid, ready).json()["code"] == "stale_revision"
    new = last_quote(api, cid)
    assert bill(api, cid, new).json()["code"] == "confirmation_required"
    assert command(api, cid, new, "confirm_quote").status_code == 202
    assert bill(api, cid, new).status_code == 202
    docs = issued(api, cid)
    assert [e["data"]["document"]["revision"] for e in docs] == [2, 3]
    assert docs[1]["data"]["document"]["documentId"] != doc["documentId"]
    assert api.call("customer", "GET", doc["url"]).content == pdf.content


@pytest.mark.parametrize("change", [False, True])
def test_overlapping_requests_share_receipt_and_recheck_before_issue(api, tmp_path, monkeypatch, change):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    assert request(api, cid, [("t1", 1)]).status_code == 202
    quote = last_quote(api, cid)
    assert command(api, cid, quote, "confirm_quote").status_code == 202
    rendering, release = Event(), Event()
    original = invoices.quote_pdf.render

    def paused(snapshot, *, invoice_number):
        rendering.set()
        assert release.wait(10)
        return original(snapshot, invoice_number=invoice_number)

    monkeypatch.setattr(invoices.quote_pdf, "render", paused)
    try:
        first = bill(api, cid, quote)
        assert first.status_code == 202 and rendering.wait(5)
        concurrent = bill(api, cid, quote, "during")
        assert (concurrent.status_code, concurrent.json()) == (202, first.json())
        if change:
            assert request(api, cid, [("t1", 2)], 1).status_code == 202
    finally:
        release.set()
    docs = issued(api, cid)
    if change:
        assert docs == []
        [failure] = [e for e in api.events(cid) if e["actionId"] == first.json()["actionId"]]
        assert (failure["data"]["type"], failure["data"]["error"]["code"]) == ("action_failed", "stale_revision")
    else:
        assert len(docs) == 1
    replay = bill(api, cid, quote, "during")
    assert (replay.status_code, replay.json()) == (202, first.json())


def test_reserved_invoice_resumes_after_restart(api, tmp_path, monkeypatch):
    cid = setup_quote(api, tmp_path, ["t1"], rules=True)
    assert request(api, cid, [("t1", 1)]).status_code == 202
    quote = last_quote(api, cid)
    assert command(api, cid, quote, "confirm_quote").status_code == 202
    with monkeypatch.context() as patch:
        patch.setattr(invoices, "start", lambda doc_id: None)
        first = bill(api, cid, quote)
    assert first.status_code == 202 and not issued(api, cid)
    api.client.portal.call(invoices.resume_pending)
    [e] = issued(api, cid)
    assert first.json()["actionId"] == e["actionId"]
    api.client.portal.call(invoices.resume_pending)
    assert issued(api, cid) == [e]
