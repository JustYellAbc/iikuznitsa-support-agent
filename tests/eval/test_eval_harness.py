"""Tests for T10 chat splitter, golden open cases vs XML, and T04 black-box eval runner."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import run_eval, split_chats  # noqa: E402

ARCHIVE_PATH = REPO_ROOT / "data/raw/чаты поддержки.zip"
CATALOG_PATH = REPO_ROOT / "data/raw/Mindbox.yml.gz.xml"
GOLDEN_OPEN_PATH = REPO_ROOT / "eval/golden_open.json"
SPLIT_SUMMARY_PATH = REPO_ROOT / "eval/split_summary.json"
PRIVATE_HOLDOUT_PATH = REPO_ROOT / "data/private/holdout_eval.json"
PRIVATE_DEV_PATH = REPO_ROOT / "data/private/dev_ids.json"


# ---------------------------------------------------------------------------
# 1. split_chats.py tests
# ---------------------------------------------------------------------------


def test_real_archive_split_has_zero_overlap_and_pins_100646(tmp_path: Path) -> None:
    if not ARCHIVE_PATH.is_file():
        pytest.skip("Raw support archive not present")

    dev1, holdout1, summary1 = split_chats.run_split(ARCHIVE_PATH, tmp_path / "run1", tmp_path / "summary1.json")
    dev2, holdout2, summary2 = split_chats.run_split(ARCHIVE_PATH, tmp_path / "run2", tmp_path / "summary2.json")

    assert dev1 == dev2
    assert holdout1 == holdout2
    assert summary1 == summary2

    dev_ids = set(dev1["ids"])
    holdout_ids = set(holdout1["ids"])
    assert len(dev_ids) + len(holdout_ids) == 495
    assert dev_ids.isdisjoint(holdout_ids)
    assert summary1["overlap_count"] == 0
    assert summary1["cluster_overlap_count"] == 0
    assert 100646 in dev_ids
    assert 100646 not in holdout_ids
    assert summary1["anchor_100646_in_open_dev"] is True
    assert summary1["anchor_100646_cluster_in_open_dev"] is True
    assert len(summary1["categories"]) == 9
    assert summary1["open_dev_count"] == 396
    assert summary1["private_holdout_count"] == 99

    # Verify cluster integrity: no cluster spans both dev and holdout
    _, records = split_chats.parse_archive(ARCHIVE_PATH)
    clusters = split_chats.cluster_records(records)
    for idxs in clusters:
        c_ids = {records[i].chat_id for i in idxs}
        assert c_ids <= dev_ids or c_ids <= holdout_ids


def test_synthetic_archive_keeps_near_duplicate_clusters_together(tmp_path: Path) -> None:
    zip_path = tmp_path / "synthetic_chats.zip"
    lines_part1 = [
        (
            "#100001 [Просто цена конкретного товара] [https://store.softline.ru/a] "
            "Сколько стоит лицензия Альфа на 10 ПК?"
        ),
        (
            "#100002 [Просто цена конкретного товара] [https://store.softline.ru/b] "
            "Здравствуйте! Нужна цена на лицензию Бета для 25 серверов."
        ),
        (
            "#100003 [Просто цена конкретного товара] [https://store.softline.ru/b] "
            "Добрый день, подскажите, нужна цена на лицензию Бета для 25 серверов, пожалуйста."
        ),
        (
            "#100004 [Просто цена конкретного товара] [https://store.softline.ru/c] "
            "Стоимость продукта Гамма на 50 мест для коммерческой организации."
        ),
        (
            "#100005 [Просто цена конкретного товара] [https://store.softline.ru/d] "
            "Нужен прайс на решение Дельта для 5 пользователей."
        ),
        (
            "#100006 [Госзакупки / процедуры] [https://store.softline.ru/e] "
            "Планируем тендер по 44-ФЗ, нужен пакет документов на ПО."
        ),
        (
            "#100007 [Госзакупки / процедуры] [https://store.softline.ru/f] "
            "Закупаем по 223-ФЗ через электронную площадку, подскажите порядок."
        ),
        (
            "#100008 [Конфигурация / спецификация] [https://store.softline.ru/g] "
            "Подберите антивирус на 100 рабочих станций под Linux."
        ),
    ]
    lines_part3 = [
        (
            "#100646 [Конфигурация / спецификация] [https://store.softline.ru/m] "
            "Нужен MDaemon Messaging Server на 20 пользователей для коммерческой компании."
        ),
        (
            "#100647 [Конфигурация / спецификация] [https://store.softline.ru/m] "
            "Добрый день! Нужен MDaemon Messaging Server на 20 пользователей для коммерческой компании, спасибо."
        ),
    ]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("buy_part_1_masked.txt", "\n".join(lines_part1) + "\n")
        zf.writestr("buy_part_3_masked.txt", "\n".join(lines_part3) + "\n")

    dev, holdout, summary = split_chats.run_split(
        zip_path, tmp_path / "priv", tmp_path / "summary.json", holdout_ratio=0.30, seed=42
    )
    dev_ids = set(dev["ids"])
    holdout_ids = set(holdout["ids"])
    assert summary["overlap_count"] == 0
    assert summary["cluster_overlap_count"] == 0
    # 100646 and its near-duplicate 100647 must both be forced into open_dev
    assert {100646, 100647} <= dev_ids
    # 100002 and 100003 form a cluster and must stay in the same split
    assert ({100002, 100003} <= dev_ids) or ({100002, 100003} <= holdout_ids)


def test_split_summary_is_safe_and_private_files_are_gitignored() -> None:
    summary_text = SPLIT_SUMMARY_PATH.read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    assert "ids" not in summary
    assert "cases" not in summary
    assert summary["overlap_count"] == 0
    assert summary["cluster_overlap_count"] == 0
    assert summary["anchor_100646_in_open_dev"] is True
    assert "[email]" not in summary_text
    assert "[номер]" not in summary_text

    if PRIVATE_HOLDOUT_PATH.exists() and PRIVATE_DEV_PATH.exists():
        proc = subprocess.run(
            ["git", "check-ignore", str(PRIVATE_HOLDOUT_PATH), str(PRIVATE_DEV_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "holdout_eval.json" in proc.stdout
        assert "dev_ids.json" in proc.stdout


# ---------------------------------------------------------------------------
# 2. golden_open.json vs Mindbox.yml.gz.xml cross-verification
# ---------------------------------------------------------------------------


def test_golden_open_reference_skus_match_xml() -> None:
    if not CATALOG_PATH.is_file():
        pytest.skip("Catalog XML not present")

    golden = json.loads(GOLDEN_OPEN_PATH.read_text(encoding="utf-8"))
    with CATALOG_PATH.open("rb") as handle:
        actual_sha = hashlib.file_digest(handle, "sha256").hexdigest()
    assert actual_sha == golden["catalog_snapshot_sha256"]

    ref_skus: dict[str, dict] = golden["reference_skus"]
    expected_ids = {
        "504",
        "48093",
        "61557",
        "19574",
        "19575",
        "19576",
        "19578",
        "19587",
        "359841",
        "359843",
        "19645",
        "3850",
        "532803",
    }
    assert set(ref_skus) == expected_ids

    xml_offers: dict[str, dict] = {}
    for _, elem in ET.iterparse(CATALOG_PATH):
        if elem.tag != "offer":
            continue
        oid = elem.get("id")
        if oid in expected_ids:
            params: dict[str, list[str]] = {}
            for p in elem.findall("param"):
                params.setdefault(p.get("name", ""), []).append((p.text or "").strip())
            xml_offers[oid] = {
                "available": elem.get("available") == "true",
                "group_id": elem.get("group_id"),
                "name": (elem.findtext("name") or "").strip(),
                "price": Decimal((elem.findtext("price") or "0").strip()),
                "url": (elem.findtext("url") or "").strip(),
                "sales_notes": (elem.findtext("sales_notes") or "").strip(),
                "params": params,
            }
        elem.clear()

    assert set(xml_offers) == expected_ids
    for oid, ref in ref_skus.items():
        actual = xml_offers[oid]
        assert ref["available"] == actual["available"], oid
        assert ref["group_id"] == actual["group_id"], oid
        assert Decimal(ref["price_rub"]) == actual["price"], oid
        assert ref["url"] == actual["url"], oid
        assert [ref["xml_license_term"]] == actual["params"]["Срок действия"], oid
        assert [ref["license_type"]] == actual["params"]["Тип лицензии"], oid
        assert [ref["user_type"]] == actual["params"]["Пользователь"], oid
        if ref["unit_price_kopecks"] is None:
            assert actual["price"] == Decimal(0)
        else:
            assert Decimal(ref["unit_price_kopecks"]) / Decimal(100) == actual["price"]

    # T03 arithmetic invariants
    assert Decimal(ref_skus["19645"]["price_rub"]) + Decimal(ref_skus["532803"]["price_rub"]) == Decimal("41227")
    assert 50 * Decimal(ref_skus["3850"]["price_rub"]) + Decimal(ref_skus["532803"]["price_rub"]) == Decimal("90047")
    assert Decimal(ref_skus["19576"]["price_rub"]) + Decimal(ref_skus["359841"]["price_rub"]) == Decimal("110199")
    assert Decimal(ref_skus["19578"]["price_rub"]) + Decimal(ref_skus["359843"]["price_rub"]) == Decimal("145274")


def test_golden_open_mode_filtering() -> None:
    mvp_cases = run_eval.load_cases(
        suite="open", mode="mvp", golden_open_path=GOLDEN_OPEN_PATH, holdout_path=PRIVATE_HOLDOUT_PATH
    )
    full_cases = run_eval.load_cases(
        suite="open", mode="full", golden_open_path=GOLDEN_OPEN_PATH, holdout_path=PRIVATE_HOLDOUT_PATH
    )
    mvp_ids = {c["id"] for c in mvp_cases}
    full_ids = {c["id"] for c in full_cases}
    assert "open-quote-recalc-full" not in mvp_ids
    assert "open-quote-recalc-full" in full_ids
    assert {"open-price-61557", "open-price-504", "open-price-48093", "open-branch-b-mail-files-50"} <= mvp_ids


# ---------------------------------------------------------------------------
# 3. run_eval.py validators & runner tests
# ---------------------------------------------------------------------------


def _event(seq: int, data: dict, action_id: str = "a1") -> dict:
    return {"seq": seq, "at": "2026-11-26T10:00:00Z", "actionId": action_id, "data": data}


def test_extract_prices_rub_ignores_non_currency_numbers() -> None:
    sample = (
        "На 50 сотрудников (12 мес., SKU 19576, 44-ФЗ, скидка 50%) "
        "варианты: 4 900 ₽, 1 523 654 руб., 1 670,94 ₽, 0 руб."
    )
    assert run_eval.extract_prices_rub(sample) == [
        Decimal("4900"),
        Decimal("1523654"),
        Decimal("1670.94"),
        Decimal("0"),
    ]


def _product(
    oid: str,
    name: str,
    url: str,
    *,
    available: bool,
    price_state: str,
    kopecks: int | None,
    term: str | None,
) -> dict:
    return {
        "offerId": oid,
        "name": name,
        "source": {"url": url, "label": "Карточка Softline"},
        "available": available,
        "priceState": price_state,
        "unitPriceKopecks": kopecks,
        "licenseTerm": term,
    }


def test_price_hallucination_validator() -> None:
    catalog = run_eval.load_catalog_index(GOLDEN_OPEN_PATH)
    url = "https://store.softline.ru/sunrav/-61557/"
    src = {"url": url, "label": "Карточка Softline"}
    good_prod = _product("61557", "BookOffice", url, available=True, price_state="listed", kopecks=490000, term=None)
    good = [
        _event(1, {"type": "products", "products": [good_prod]}),
        _event(
            2,
            {"type": "message", "author": "agent", "purpose": "answer", "text": "Цена 4 900 ₽.", "sources": [src]},
        ),
    ]
    assert run_eval.validate_no_hallucinated_prices(good, ["4900"], catalog) == []

    bad_text = [
        _event(
            1,
            {"type": "message", "author": "agent", "purpose": "answer", "text": "Цена 5 200 ₽.", "sources": [src]},
        ),
    ]
    assert run_eval.validate_no_hallucinated_prices(bad_text, ["4900"], catalog)

    bad_prod = _product("61557", "BookOffice", url, available=True, price_state="listed", kopecks=520000, term=None)
    bad_card = [_event(1, {"type": "products", "products": [bad_prod]})]
    assert run_eval.validate_no_hallucinated_prices(bad_card, ["4900", "5200"], catalog)


def test_anti_term_hallucination_for_61557() -> None:
    url = "https://store.softline.ru/sunrav/-61557/"
    src = {"url": url, "label": "Карточка Softline"}
    good_prod = _product("61557", "BookOffice", url, available=True, price_state="listed", kopecks=490000, term=None)
    good = [
        _event(1, {"type": "products", "products": [good_prod]}),
        _event(
            2,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": (
                    "Стоимость 4 900 ₽. Срок действия в карточке не указан "
                    "(12 мес. или бессрочно — требуется уточнение у специалиста)."
                ),
                "sources": [src],
            },
        ),
    ]
    assert run_eval.validate_anti_term_hallucination(good, "61557") == []

    bad_text = [
        _event(
            1,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Стоимость 4 900 ₽. Лицензия предоставляется на 12 мес.",
                "sources": [src],
            },
        ),
    ]
    assert run_eval.validate_anti_term_hallucination(bad_text, "61557")

    bad_prod = _product(
        "61557", "BookOffice", url, available=True, price_state="listed", kopecks=490000, term="12 мес."
    )
    assert run_eval.validate_anti_term_hallucination([_event(1, {"type": "products", "products": [bad_prod]})], "61557")


def test_anti_zero_price_for_504() -> None:
    url = "https://store.softline.ru/adobe/65272676ba01a12-504/"
    src = {"url": url, "label": "Карточка Softline"}
    prod = _product("504", "InCopy", url, available=False, price_state="on_request", kopecks=None, term="12 мес.")
    handoff = {
        "handoffId": "h1",
        "conversationId": "c1",
        "reason": "price_request",
        "question": "Уточнить цену 504",
        "products": [],
        "status": "open",
    }
    good = [
        _event(1, {"type": "products", "products": [prod]}),
        _event(
            2,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Цена по запросу. Запрос передан менеджеру.",
                "sources": [src],
            },
        ),
        _event(3, {"type": "handoff", "handoff": handoff}),
    ]
    assert run_eval.validate_anti_zero_price(good, "504") == []

    bad_zero = [
        _event(
            1,
            {"type": "message", "author": "agent", "purpose": "answer", "text": "Цена 0 руб.", "sources": [src]},
        ),
        _event(2, {"type": "handoff", "handoff": handoff}),
    ]
    assert run_eval.validate_anti_zero_price(bad_zero, "504")

    bad_no_handoff = [
        _event(
            1,
            {"type": "message", "author": "agent", "purpose": "answer", "text": "Цена по запросу.", "sources": [src]},
        ),
    ]
    assert run_eval.validate_anti_zero_price(bad_no_handoff, "504")


def test_unavailable_with_price_48093_and_sources_and_cursors() -> None:
    url = "https://store.softline.ru/iceni-technology/-48093/"
    src = {"url": url, "label": "Карточка Softline"}
    prod = _product(
        "48093", "Iceni", url, available=False, price_state="availability_check", kopecks=152365400, term=None
    )
    handoff = {
        "handoffId": "h1",
        "conversationId": "c1",
        "reason": "availability",
        "question": "Проверить 48093",
        "products": [],
        "status": "open",
    }
    good = [
        _event(1, {"type": "products", "products": [prod]}),
        _event(
            2,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Цена снимка 1 523 654 ₽, но позиция сейчас недоступна — передал проверку оператору.",
                "sources": [src],
            },
        ),
        _event(3, {"type": "handoff", "handoff": handoff}),
    ]
    assert run_eval.validate_unavailable_with_price(good, "48093") == []
    assert run_eval.validate_sources_present(good, [url]) == []
    assert run_eval.validate_contract_events(good, "c1") == []
    assert run_eval.validate_independent_cursors(good, good, 3) == []

    doc = {
        "documentId": "d1",
        "quoteId": "q1",
        "revision": 1,
        "kind": "demo_invoice",
        "url": "/api/v1/documents/d1",
    }
    bad_doc = good + [_event(4, {"type": "document", "document": doc})]
    assert run_eval.validate_unavailable_with_price(bad_doc, "48093")

    bad_no_source = [
        _event(
            1,
            {"type": "message", "author": "agent", "purpose": "answer", "text": "Цена 4 900 ₽.", "sources": []},
        ),
    ]
    assert run_eval.validate_sources_present(bad_no_source)
    assert run_eval.validate_independent_cursors(good, good[:2], 2)


def test_metrics_latency_and_economics() -> None:
    results = [
        run_eval.CaseResult("c1", "open", "cat", True, False, True, latency_ms=[100.0, 200.0], cost_rub=0.40),
        run_eval.CaseResult("c2", "open", "cat", True, True, False, latency_ms=[400.0], cost_rub=0.80),
    ]
    summary = run_eval.summarize_results(results, suite="open", mode="mvp")
    assert summary["latency_ms"]["p50"] == 200.0
    assert summary["latency_ms"]["p95"] == 380.0
    assert summary["closed_by_agent_count"] == 1
    assert summary["escalated_count"] == 1
    assert summary["economics"]["avg_cost_per_case_rub"] == 0.6
    assert summary["economics"]["projected_500_cases_per_day_rub"] == 300.0


def test_run_eval_cli_full_open_suite_over_http(tmp_path: Path) -> None:
    """End-to-end HTTP test covering all 12 golden_open.json cases in full mode."""
    golden = json.loads(GOLDEN_OPEN_PATH.read_text(encoding="utf-8"))
    skus = golden["reference_skus"]
    lock = threading.Lock()
    state: dict[str, object] = {"counter": 0, "convs": {}, "spend": 0.0}

    def prod_obj(oid: str) -> dict:
        s = skus[oid]
        return {
            "offerId": oid,
            "name": s["name"],
            "source": {"url": s["url"], "label": "Карточка Softline"},
            "available": s["available"],
            "priceState": s["price_state"],
            "unitPriceKopecks": s["unit_price_kopecks"],
            "licenseTerm": s["contract_license_term"],
        }

    def make_handoff(cid: str, reason: str, question: str, products: list[dict]) -> dict:
        return {
            "type": "handoff",
            "handoff": {
                "handoffId": f"h-{cid}",
                "conversationId": cid,
                "reason": reason,
                "question": question,
                "products": products,
                "status": "open",
            },
        }

    def make_quote(rev: int, lines: list[dict], total: int) -> dict:
        return {
            "type": "quote",
            "quote": {
                "quoteId": "q1",
                "revision": rev,
                "state": "ready",
                "lines": lines,
                "knownSubtotalKopecks": total,
                "totalKopecks": total,
                "terms": {
                    "taxNote": "С НДС",
                    "payment": "Предоплата",
                    "delivery": "3 дня",
                    "validUntil": "2026-12-10",
                },
            },
        }

    def quote_line(oid: str, kopecks: int) -> dict:
        p = prod_obj(oid)
        return {
            "offerId": oid,
            "name": p["name"],
            "source": p["source"],
            "packages": 1,
            "unitPriceKopecks": kopecks,
        }

    class FullMockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                with lock:
                    spend = state["spend"]
                self._send(200, {"status": "ok", "today_spend_rub": spend})
                return
            m = re.match(r"^/api/v1/conversations/([^/]+)/events$", parsed.path)
            if not m:
                self._send(404, {"code": "not_found", "message": "not found"})
                return
            cid = m.group(1)
            after = int(urllib.parse.parse_qs(parsed.query).get("after", ["0"])[0])
            with lock:
                events = state["convs"][cid]["events"]  # type: ignore[index]
                page = events[after : after + 2]
                next_after = after + len(page)
                has_more = next_after < len(events)
            self._send(200, {"events": page, "nextAfter": next_after, "hasMore": has_more})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/v1/conversations":
                with lock:
                    state["counter"] = int(state["counter"]) + 1
                    cid = f"c-{state['counter']}"
                    state["convs"][cid] = {"events": [], "turn": 0}  # type: ignore[index]
                self._send(201, {"conversationId": cid})
                return
            m = re.match(r"^/api/v1/conversations/([^/]+)/actions$", parsed.path)
            if not m:
                self._send(404, {"code": "not_found", "message": "not found"})
                return
            cid = m.group(1)
            with lock:
                conv = state["convs"][cid]  # type: ignore[index]
                conv["turn"] += 1
                aid = f"a-{conv['turn']}"
                events: list[dict] = conv["events"]
                state["spend"] = float(state["spend"]) + 0.25

                def push(data: dict) -> None:
                    events.append({"seq": len(events) + 1, "at": "2026-11-26T10:00:00Z", "actionId": aid, "data": data})

                def msg(purpose: str, text: str, sources: list[dict]) -> None:
                    push({"type": "message", "author": "agent", "purpose": purpose, "text": text, "sources": sources})

                if body.get("type") == "request_operator":
                    push(make_handoff(cid, "customer_request", "Нужен оператор", []))
                else:
                    text = body["text"]
                    push({"type": "message", "author": "customer", "purpose": "input", "text": text, "sources": []})
                    if "61557" in text:
                        p = prod_obj("61557")
                        push({"type": "products", "products": [p]})
                        msg(
                            "answer",
                            "SunRav BookOffice: 4 900 ₽. Срок не указан, уточнит специалист.",
                            [p["source"]],
                        )
                    elif "504" in text:
                        p = prod_obj("504")
                        push({"type": "products", "products": [p]})
                        msg("answer", "Adobe InCopy CC — цена по запросу, передал менеджеру.", [p["source"]])
                        push(make_handoff(cid, "price_request", "Запросить цену 504", [p]))
                    elif "48093" in text:
                        p = prod_obj("48093")
                        push({"type": "products", "products": [p]})
                        msg(
                            "answer",
                            "Iceni: 1 523 654 ₽, недоступна — проверка у оператора.",
                            [p["source"]],
                        )
                        push(make_handoff(cid, "availability", "Проверить доступность 48093", [p]))
                    elif "MDaemon Standard" in text:
                        msg(
                            "clarification",
                            "Редакции Standard нет в каталоге. Уточните число пользователей?",
                            [],
                        )
                    elif "5 и 10 пользователей" in text:
                        p5, p10 = prod_obj("19574"), prod_obj("19575")
                        push({"type": "products", "products": [p5, p10]})
                        msg(
                            "answer",
                            "5 мест — 47 808 ₽, 10 мест — 61 698 ₽.",
                            [p5["source"], p10["source"]],
                        )
                    elif "MDaemon на 20 пользователей" in text:
                        msg("clarification", "Это первая покупка или продление подписки?", [])
                    elif "новая покупка" in text:
                        p20 = prod_obj("19576")
                        push({"type": "products", "products": [p20]})
                        msg("answer", "Новая покупка MDaemon на 20 пользователей — 77 556 ₽.", [p20["source"]])
                    elif "защитить корпоративную почту" in text:
                        msg("clarification", "Уточните почтовый сервер, ОС файлов, адреса и бюджет?", [])
                    elif "Postfix в Linux" in text:
                        p1, p2, p3 = prod_obj("19645"), prod_obj("3850"), prod_obj("532803")
                        push({"type": "products", "products": [p1, p2, p3]})
                        msg(
                            "answer",
                            "Вариант 1: 34 727 ₽ + 6 500 ₽ = 41 227 ₽. "
                            "Вариант 2: 50 × 1 670,94 ₽ + 6 500 ₽ = 90 047 ₽.",
                            [p1["source"], p2["source"], p3["source"]],
                        )
                    elif "российский аналог" in text:
                        p2 = prod_obj("3850")
                        reg_src = {
                            "url": "https://www.kaspersky.ru/small-to-medium-business-security/registry",
                            "label": "Реестр Kaspersky",
                        }
                        msg(
                            "answer",
                            "Российский аналог — Kaspersky; в снимке 3850 номер не указан.",
                            [p2["source"], reg_src],
                        )
                    elif "для 20 пользователей" in text:
                        p20, pc20 = prod_obj("19576"), prod_obj("359841")
                        lines = [quote_line("19576", 7755600), quote_line("359841", 3264300)]
                        push(make_quote(1, lines, 11019900))
                        msg(
                            "answer",
                            "Расчёт на 20 мест: 77 556 ₽ + 32 643 ₽ = 110 199 ₽.",
                            [p20["source"], pc20["source"]],
                        )
                    elif "на 40 пользователей" in text:
                        p40, pc40 = prod_obj("19578"), prod_obj("359843")
                        lines = [quote_line("19578", 9434100), quote_line("359843", 5093300)]
                        push(make_quote(2, lines, 14527400))
                        msg(
                            "answer",
                            "На 40 мест: 94 341 ₽ + 50 933 ₽ = 145 274 ₽ (разница 35 075 ₽).",
                            [p40["source"], pc40["source"]],
                        )
                    elif "HyperQuantum" in text:
                        msg("notice", "Продукт не найден в каталоге, передаю запрос менеджеру.", [])
                        push(make_handoff(cid, "outside_catalog", "HyperQuantum Nebula ERP 2026", []))
                    elif "живого менеджера" in text:
                        msg("notice", "Переключаю диалог на оператора.", [])
                        push(make_handoff(cid, "customer_request", "Запрос менеджера", []))
                    elif "скидку 50%" in text:
                        msg("notice", "Индивидуальные скидки согласовывает менеджер, передал запрос.", [])
                        push(make_handoff(cid, "special_terms", "Запрос скидки 50%", [prod_obj("19576")]))
                    elif "44-ФЗ" in text:
                        msg("notice", "Тендерную документацию по 44-ФЗ готовит менеджер, обращение передано.", [])
                        push(make_handoff(cid, "tender", "Тендер 44-ФЗ", []))

            self._send(202, {"conversationId": cid, "actionId": aid})

    server = ThreadingHTTPServer(("127.0.0.1", 0), FullMockHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out_prefix = tmp_path / "eval_out"
        rc = run_eval.main(
            [
                "--base-url",
                f"http://{host}:{port}",
                "--suite",
                "open",
                "--mode",
                "full",
                "--poll-interval",
                "0.01",
                "--out",
                str(out_prefix),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert rc == 0
    report = json.loads((tmp_path / "eval_out.json").read_text(encoding="utf-8"))
    assert report["total_cases"] == 12
    assert report["passed_cases"] == 12
    assert report["failed_cases"] == 0
    assert report["economics"]["avg_cost_per_case_rub"] is not None
    assert (tmp_path / "eval_out.md").is_file()


def test_run_eval_mock_check_cli() -> None:
    assert run_eval.main(["--mock-check", "--golden-open", str(GOLDEN_OPEN_PATH)]) == 0
