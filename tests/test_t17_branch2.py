"""Integration tests for T17 Branch 2: disputed SKU approval -> quote_pdf -> demo_invoice and handoff deduplication."""

import base64
import json
import re
import shutil
import subprocess
import zlib
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from catalog.index import KNOWLEDGE

from app.agent.quote_pdf import money
from app.config import get_settings
from app.services.feed_refresh import refresh
from tests.test_demo_invoices import bill, issued
from tests.test_feed_refresh import LIMITS, write_feed
from tests.test_quotes import command, last_quote, request

BRANCH2_OFFERS = [
    ("61557", "true", "4900", "SunRav TestOfficePro"),
    ("504", "false", "0", "Adobe Creative Cloud Teams"),
    ("48093", "false", "1523654", "Iceni Infix PDF Editor Enterprise"),
]


def _setup_branch2(api, tmp_path, skus: list[str], *, vat_rate: str = "20") -> str:
    source = write_feed(tmp_path / "branch2.xml", "2026-11-26 10:00", BRANCH2_OFFERS)
    refresh(source, get_settings().catalog_index_path, KNOWLEDGE, LIMITS)
    terms_path = tmp_path / "config" / "quote_terms.json"
    terms_path.parent.mkdir(parents=True, exist_ok=True)
    terms_path.write_text(
        json.dumps(
            {
                sku: {
                    "article": f"SL-{sku}",
                    "source": "Тестовые коммерческие условия T17",
                    "payment": "100% предоплата",
                    "delivery": "Электронная поставка за 2 рабочих дня",
                    "vatRate": vat_rate,
                    "validUntil": (date.today() + timedelta(days=7)).isoformat(),
                }
                for sku, *_ in BRANCH2_OFFERS
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cid = api.new_conversation()
    for sku in skus:
        assert api.say(cid, f"Артикул {sku}").status_code == 202
        api.idle()
    return cid


def _decode_pdf_stream(raw: bytes, header: str) -> bytes:
    data = raw.strip()
    if "ASCII85Decode" in header:
        data = base64.a85decode(data, adobe=True)
    if "FlateDecode" in header:
        data = zlib.decompress(data)
    return data


def _unescape_pdf_literal(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            if 0x30 <= nxt <= 0x37:  # octal \ddd
                oct_digits = bytes([nxt])
                j = i + 2
                while j < len(raw) and len(oct_digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    oct_digits += bytes([raw[j]])
                    j += 1
                out.append(int(oct_digits, 8))
                i = j
                continue
            escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            out.append(escapes.get(nxt, nxt))
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def _extract_pdf_text(blob: bytes) -> str:
    """Extract readable text from ReportLab PDF using pdftotext if available, else decode ToUnicode CMaps."""
    if extractor := shutil.which("pdftotext"):
        return subprocess.run(
            [extractor, "-layout", "-enc", "UTF-8", "-", "-"], input=blob, capture_output=True, check=True
        ).stdout.decode("utf-8")

    objects: dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)endobj", blob, re.S):
        objects[int(m.group(1))] = m.group(2)

    def stream_of(obj_body: bytes) -> bytes | None:
        sm = re.search(rb"stream\r?\n(.*?)endstream", obj_body, re.S)
        if not sm:
            return None
        header = obj_body[: sm.start()].decode("latin1", errors="replace")
        return _decode_pdf_stream(sm.group(1), header)

    font_cmaps: dict[str, dict[int, str]] = {}
    for obj_body in objects.values():
        for fm in re.finditer(rb"/([A-Za-z0-9+_-]+)\s+(\d+)\s+0\s+R", obj_body):
            font_name = fm.group(1).decode("latin1")
            font_obj = objects.get(int(fm.group(2)), b"")
            tm = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", font_obj)
            if not tm:
                continue
            cmap_bytes = stream_of(objects.get(int(tm.group(1)), b""))
            if not cmap_bytes:
                continue
            cmap: dict[int, str] = {}
            for bm in re.finditer(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", cmap_bytes):
                cmap[int(bm.group(1), 16)] = chr(int(bm.group(2), 16))
            font_cmaps[font_name] = cmap

    chunks: list[str] = []
    for obj_body in objects.values():
        content = stream_of(obj_body)
        if not content or b"BT" not in content or b"Tj" not in content:
            continue
        for block in re.finditer(rb"BT\b(.*?)\bET", content, re.S):
            segment = block.group(1)
            font_match = re.search(rb"/([A-Za-z0-9+_-]+)\s+[\d.]+\s+Tf", segment)
            cmap = font_cmaps.get(font_match.group(1).decode("latin1"), {}) if font_match else {}
            for tj in re.finditer(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", segment, re.S):
                raw_bytes = _unescape_pdf_literal(tj.group(1))
                decoded = "".join(cmap.get(b, chr(b)) for b in raw_bytes)
                chunks.append(decoded)
    return "\n".join(chunks)


def _quote_pdf(api, cid, quote, key=None):
    return api.call(
        "customer",
        "POST",
        f"/api/v1/conversations/{cid}/actions",
        key=key,
        json={
            "type": "request_document",
            "kind": "quote_pdf",
            "quoteId": quote["quoteId"],
            "revision": quote["revision"],
        },
    )


def _vat_kopecks(amount_kopecks: int, rate: Decimal = Decimal("20")) -> int:
    vat = Decimal(amount_kopecks) * rate / (Decimal("100") + rate)
    return int(vat.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@pytest.mark.parametrize(
    "sku,reason,approved_unit",
    [
        ("504", "price_request", 125050),
        ("48093", "availability", 152365400),
    ],
)
def test_branch2_full_flow_for_disputed_skus(api, tmp_path, monkeypatch, sku, reason, approved_unit):
    """T17 Branch 2: disputed SKU (504 / 48093) blocked -> approve_line_price -> PDFs -> qty change reset."""
    monkeypatch.setattr(get_settings(), "rate_limit_session_msg_per_min", 50)
    cid = _setup_branch2(api, tmp_path, ["61557", sku])

    # 1. Draft quote with listed SKU + disputed SKU -> confirm_quote, quote_pdf, demo_invoice are all blocked.
    assert request(api, cid, [("61557", 2), (sku, 3)]).status_code == 202
    draft = last_quote(api, cid)
    assert draft["revision"] == 1
    assert draft["state"] == "needs_review"
    assert draft["knownSubtotalKopecks"] == 2 * 490000
    assert draft["totalKopecks"] is None
    assert draft["lines"][1]["offerId"] == sku and draft["lines"][1]["unitPriceKopecks"] is None

    assert command(api, cid, draft, "confirm_quote").json()["code"] == "terms_pending"
    assert _quote_pdf(api, cid, draft).json()["code"] == "terms_pending"
    assert bill(api, cid, draft).json()["code"] == "terms_pending"
    assert issued(api, cid) == []

    # 2. Manager sees the disputed line and dialog history, then approves price/supply via approve_line_price.
    api.login()
    queue = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert len(queue) == 1
    handoff = queue[0]
    assert handoff["reason"] == reason
    assert handoff["quoteLine"] == {"quoteId": draft["quoteId"], "revision": 1, "offerId": sku}
    assert api.call("manager", "GET", f"/api/v1/conversations/{cid}/events").status_code == 200

    approve = api.call(
        "manager",
        "POST",
        f"/api/v1/manager/handoffs/{handoff['handoffId']}/resolution",
        json={"type": "approve_line_price", **handoff["quoteLine"], "unitPriceKopecks": approved_unit},
    )
    assert approve.status_code == 202, approve.text
    assert api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] == []

    # 3. Quote revision 2 becomes ready with exact kopecks total; old rev 1 is stale; unconfirmed rev 2 blocks invoice.
    ready = last_quote(api, cid)
    expected_line1 = 2 * 490000
    expected_line2 = 3 * approved_unit
    expected_total = expected_line1 + expected_line2
    assert (ready["revision"], ready["state"], ready["totalKopecks"]) == (2, "ready", expected_total)
    assert [(line["offerId"], line["packages"], line["unitPriceKopecks"]) for line in ready["lines"]] == [
        ("61557", 2, 490000),
        (sku, 3, approved_unit),
    ]
    assert command(api, cid, draft, "confirm_quote").json()["code"] == "stale_revision"
    assert bill(api, cid, ready).json()["code"] == "confirmation_required"

    # 4. Buyer confirms rev 2 and downloads quote_pdf + demo_invoice; repeat requests return the exact same documents.
    assert command(api, cid, ready, "confirm_quote").status_code == 202
    confirmed = last_quote(api, cid)
    assert confirmed["state"] == "confirmed" and confirmed["revision"] == 2

    kp_first = _quote_pdf(api, cid, confirmed)
    assert kp_first.status_code == 202
    kp_docs = [
        e["data"]["document"]
        for e in api.events(cid)
        if e["data"]["type"] == "document" and e["data"]["document"]["kind"] == "quote_pdf"
    ]
    assert len(kp_docs) == 1
    kp_doc = kp_docs[0]
    kp_repeat = _quote_pdf(api, cid, confirmed)
    assert kp_repeat.status_code == 200 and kp_repeat.json() == kp_doc

    inv_first = bill(api, cid, confirmed)
    assert inv_first.status_code == 202
    [inv_event] = issued(api, cid)
    inv_doc = inv_event["data"]["document"]
    assert (inv_doc["quoteId"], inv_doc["revision"], inv_doc["kind"]) == (confirmed["quoteId"], 2, "demo_invoice")
    inv_repeat = bill(api, cid, confirmed)
    assert inv_repeat.status_code == 200 and inv_repeat.json() == inv_doc

    kp_pdf = api.call("customer", "GET", kp_doc["url"])
    inv_pdf = api.call("customer", "GET", inv_doc["url"])
    assert kp_pdf.status_code == 200 and kp_pdf.content.startswith(b"%PDF-")
    assert inv_pdf.status_code == 200 and inv_pdf.content.startswith(b"%PDF-")

    # 5. Verify lines, quantities, prices, VAT and total inside BOTH rendered PDFs match Decimal calculation.
    expected_vat1 = _vat_kopecks(expected_line1)
    expected_vat2 = _vat_kopecks(expected_line2)
    expected_vat_total = expected_vat1 + expected_vat2
    common_fragments = (
        "SL-61557",
        f"SL-{sku}",
        "ID 61557",
        f"ID {sku}",
        money(490000),
        money(expected_line1),
        money(approved_unit),
        money(expected_line2),
        money(expected_total),
        money(expected_vat1),
        money(expected_vat2),
        money(expected_vat_total),
    )
    kp_text = _extract_pdf_text(kp_pdf.content)
    inv_text = _extract_pdf_text(inv_pdf.content)
    assert "Коммерческое предложение" in kp_text and "Версия 2" in kp_text
    assert "Демо-счёт" in inv_text and "версия 2" in inv_text
    for qty in ("2", "3"):
        assert re.search(rf"(?<!\d){qty}(?!\d)", kp_text), f"Missing quantity {qty} in quote PDF"
        assert re.search(rf"(?<!\d){qty}(?!\d)", inv_text), f"Missing quantity {qty} in demo invoice PDF"
    for fragment in common_fragments:
        assert fragment in kp_text, f"Missing {fragment!r} in quote PDF"
        assert fragment in inv_text, f"Missing {fragment!r} in demo invoice PDF"

    # 6. Quantity change creates rev 3, resets prior approval/consent, and blocks invoice on old rev 2.
    assert request(api, cid, [("61557", 2), (sku, 4)], revision=2).status_code == 202
    rev3 = last_quote(api, cid)
    assert (rev3["revision"], rev3["state"], rev3["totalKopecks"]) == (3, "needs_review", None)
    assert rev3["lines"][1]["packages"] == 4 and rev3["lines"][1]["unitPriceKopecks"] is None
    assert bill(api, cid, confirmed).json()["code"] == "stale_revision"
    assert command(api, cid, confirmed, "confirm_quote").json()["code"] == "stale_revision"
    assert command(api, cid, rev3, "confirm_quote").json()["code"] == "terms_pending"
    assert bill(api, cid, rev3).json()["code"] == "terms_pending"
    # Previously issued rev 2 PDFs remain downloadable by owner.
    assert api.call("customer", "GET", kp_doc["url"]).content == kp_pdf.content
    assert api.call("customer", "GET", inv_doc["url"]).content == inv_pdf.content


def test_approving_one_disputed_line_does_not_unlock_another_disputed_line(api, tmp_path, monkeypatch):
    """Approving SKU 504 only unlocks 504; unapproved SKU 48093 keeps the quote in needs_review until also approved."""
    monkeypatch.setattr(get_settings(), "rate_limit_session_msg_per_min", 50)
    cid = _setup_branch2(api, tmp_path, ["504", "48093"])
    assert request(api, cid, [("504", 1), ("48093", 1)]).status_code == 202
    api.login()
    queue = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    by_sku = {h["quoteLine"]["offerId"]: h for h in queue}
    assert set(by_sku) == {"504", "48093"}
    assert (by_sku["504"]["reason"], by_sku["48093"]["reason"]) == ("price_request", "availability")

    # Approve 504 only -> rev 2 must remain needs_review because 48093 is still pending.
    h504 = by_sku["504"]
    h48093_id = by_sku["48093"]["handoffId"]
    res1 = api.call(
        "manager",
        "POST",
        f"/api/v1/manager/handoffs/{h504['handoffId']}/resolution",
        json={"type": "approve_line_price", **h504["quoteLine"], "unitPriceKopecks": 50000},
    )
    assert res1.status_code == 202
    rev2 = last_quote(api, cid)
    assert (rev2["revision"], rev2["state"], rev2["knownSubtotalKopecks"], rev2["totalKopecks"]) == (
        2,
        "needs_review",
        50000,
        None,
    )
    assert command(api, cid, rev2, "confirm_quote").json()["code"] == "terms_pending"
    assert bill(api, cid, rev2).json()["code"] == "terms_pending"

    # Only the 48093 handoff stays open, carrying the same handoffId to revision 2 (no duplicate).
    [remaining] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert remaining["handoffId"] == h48093_id
    assert remaining["quoteLine"] == {"quoteId": rev2["quoteId"], "revision": 2, "offerId": "48093"}

    # Now approve 48093 -> rev 3 becomes ready, buyer confirms and issues demo invoice.
    res2 = api.call(
        "manager",
        "POST",
        f"/api/v1/manager/handoffs/{remaining['handoffId']}/resolution",
        json={"type": "approve_line_price", **remaining["quoteLine"], "unitPriceKopecks": 152365400},
    )
    assert res2.status_code == 202
    rev3 = last_quote(api, cid)
    assert (rev3["revision"], rev3["state"], rev3["totalKopecks"]) == (3, "ready", 50000 + 152365400)
    assert command(api, cid, rev3, "confirm_quote").status_code == 202
    assert bill(api, cid, rev3).status_code == 202
    assert len(issued(api, cid)) == 1


@pytest.mark.parametrize("sku", ["504", "48093"])
def test_free_text_close_does_not_unlock_disputed_line(api, tmp_path, sku):
    cid = _setup_branch2(api, tmp_path, [sku])
    assert request(api, cid, [(sku, 1)]).status_code == 202
    draft = last_quote(api, cid)
    api.login()
    [h] = api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"]
    assert (
        api.call(
            "manager",
            "POST",
            f"/api/v1/manager/handoffs/{h['handoffId']}/resolution",
            json={"type": "close", "reply": "Поставка возможна, цена 1000 руб."},
        ).status_code
        == 202
    )
    assert command(api, cid, draft, "confirm_quote").json()["code"] == "terms_pending"
    assert bill(api, cid, draft).json()["code"] == "terms_pending"


@pytest.mark.parametrize(
    "sku,question,reason",
    [
        ("504", "Какая цена?", "price_request"),
        ("48093", "Есть ли в наличии?", "availability"),
    ],
)
def test_no_duplicate_handoffs_between_chat_and_quote_revisions(api, tmp_path, monkeypatch, sku, question, reason):
    """Chat inquiry + quote creation + revision updates keep a single open handoff in both orderings."""
    monkeypatch.setattr(get_settings(), "rate_limit_session_msg_per_min", 50)
    api.login()

    # Ordering 1: chat handoff first -> then prepare quote -> then change quantity -> then approve.
    cid1 = _setup_branch2(api, tmp_path, [sku])
    assert api.say(cid1, question).status_code == 202
    api.idle()
    [chat_h] = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid1
    ]
    assert chat_h["reason"] == reason and "quoteLine" not in chat_h

    assert request(api, cid1, [(sku, 1)]).status_code == 202
    open1 = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid1
    ]
    assert len(open1) == 1
    assert open1[0]["handoffId"] == chat_h["handoffId"]
    assert open1[0]["quoteLine"]["revision"] == 1 and open1[0]["quoteLine"]["offerId"] == sku

    assert request(api, cid1, [(sku, 2)], revision=1).status_code == 202
    open1_rev2 = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid1
    ]
    assert len(open1_rev2) == 1
    assert open1_rev2[0]["handoffId"] == chat_h["handoffId"]
    assert open1_rev2[0]["quoteLine"]["revision"] == 2

    assert (
        api.call(
            "manager",
            "POST",
            f"/api/v1/manager/handoffs/{chat_h['handoffId']}/resolution",
            json={"type": "approve_line_price", **open1_rev2[0]["quoteLine"], "unitPriceKopecks": 50000},
        ).status_code
        == 202
    )
    assert [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid1
    ] == []

    # Ordering 2: prepare quote first -> then ask in chat -> then update quantity -> still 1 open handoff.
    cid2 = _setup_branch2(api, tmp_path, [sku])
    assert request(api, cid2, [(sku, 1)]).status_code == 202
    [quote_h] = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid2
    ]
    assert quote_h["reason"] == reason and quote_h["quoteLine"]["revision"] == 1

    assert api.say(cid2, question).status_code == 202
    api.idle()
    open2 = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid2
    ]
    assert len(open2) == 1 and open2[0]["handoffId"] == quote_h["handoffId"]

    assert request(api, cid2, [(sku, 3)], revision=1).status_code == 202
    open2_rev2 = [
        h for h in api.call("manager", "GET", "/api/v1/manager/handoffs").json()["items"] if h["conversationId"] == cid2
    ]
    assert len(open2_rev2) == 1
    assert open2_rev2[0]["handoffId"] == quote_h["handoffId"]
    assert open2_rev2[0]["quoteLine"]["revision"] == 2
