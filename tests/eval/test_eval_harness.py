"""Tests for T10 chat splitter, golden open cases vs XML, and T04 black-box eval runner."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import stat
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

from eval import pii, run_eval, split_chats  # noqa: E402 - needs the sys.path tweak above

ARCHIVE_PATH = REPO_ROOT / "data/raw/чаты поддержки.zip"
CATALOG_PATH = REPO_ROOT / "data/raw/Mindbox.yml.gz.xml"
GOLDEN_OPEN_PATH = REPO_ROOT / "eval/golden_open.json"
SPLIT_SUMMARY_PATH = REPO_ROOT / "eval/split_summary.json"
PRIVATE_DEV_PATH = REPO_ROOT / "data/private/dev_ids.json"


# ---------------------------------------------------------------------------
# 1. split_chats.py tests
# ---------------------------------------------------------------------------


def test_real_archive_split_has_zero_overlap_and_pins_100646(tmp_path: Path) -> None:
    if not ARCHIVE_PATH.is_file():
        pytest.skip("Raw support archive not present")

    seed = secrets.token_hex(16)
    dev1, holdout1, summary1 = split_chats.run_split(
        ARCHIVE_PATH, tmp_path / "run1", tmp_path / "summary1.json", seed=seed
    )
    dev2, holdout2, summary2 = split_chats.run_split(
        ARCHIVE_PATH, tmp_path / "run2", tmp_path / "summary2.json", seed=seed
    )
    for name in ("holdout_eval.json", "dev_ids.json"):
        assert stat.S_IMODE((tmp_path / "run1" / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run1").stat().st_mode) == 0o700
    assert "seed" not in holdout1 and "seed" not in dev1

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
    # All data_audit.md paraphrased examples and Echelon repeats must be pinned in open_dev.
    must_be_dev = {102976, 102802, 102847, 102888, 102972, 101730, 101860, 101796}
    assert must_be_dev <= dev_ids
    assert must_be_dev.isdisjoint(holdout_ids)
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
        zip_path, tmp_path / "priv", tmp_path / "summary.json", holdout_ratio=0.30, seed=secrets.token_hex(16)
    )
    dev_ids = set(dev["ids"])
    holdout_ids = set(holdout["ids"])
    assert summary["overlap_count"] == 0
    assert summary["cluster_overlap_count"] == 0
    # 100646 and its near-duplicate 100647 must both be forced into open_dev
    assert {100646, 100647} <= dev_ids
    # 100002 and 100003 form a cluster and must stay in the same split
    assert ({100002, 100003} <= dev_ids) or ({100002, 100003} <= holdout_ids)


def test_split_summary_is_safe_and_isolation_guards_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    summary_text = SPLIT_SUMMARY_PATH.read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    assert "ids" not in summary
    assert "cases" not in summary
    assert "seed" not in summary
    assert "seed_sha256_prefix" not in summary
    assert "seed_source" not in summary
    assert summary["overlap_count"] == 0
    assert summary["cluster_overlap_count"] == 0
    assert summary["anchor_100646_in_open_dev"] is True
    assert "[email]" not in summary_text
    assert "[номер]" not in summary_text

    # Holdout default path lives outside the repo and outside /tmp; the seed comes only from EVAL_SPLIT_SEED.
    monkeypatch.delenv("EVAL_HOLDOUT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    default_dir = split_chats.default_holdout_dir()
    assert default_dir == tmp_path / "home/.local/share/iikuznitsa-eval-private"
    assert not default_dir.resolve().is_relative_to(REPO_ROOT.resolve())
    token = secrets.token_hex(16)
    monkeypatch.setenv("EVAL_SPLIT_SEED", token)
    assert split_chats.resolve_seed() == token

    # Dotfiles & rules protecting holdout and ignoring .DS_Store.
    claude_cfg = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    deny = claude_cfg["permissions"]["deny"]
    # data/private holds the T07/T14 catalog index, so it must stay readable; only the external holdout is denied.
    assert not any("data/private" in rule for rule in deny)
    assert "Read(~/.local/share/iikuznitsa-eval-private/**)" in deny
    assert "Bash(*iikuznitsa-eval-private*)" in deny
    assert "data/private/**" not in (REPO_ROOT / ".geminiignore").read_text(encoding="utf-8")
    assert "data/private/**" not in (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert ".DS_Store" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    proc = subprocess.run(
        ["git", "check-ignore", "data/private/dev_ids.json", ".DS_Store", "data/.DS_Store"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "dev_ids.json" in proc.stdout
    assert ".DS_Store" in proc.stdout


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
    assert Decimal(ref_skus["19645"]["price_rub"]) + Decimal(ref_skus["532803"]["price_rub"]) == Decimal(41227)
    assert 50 * Decimal(ref_skus["3850"]["price_rub"]) + Decimal(ref_skus["532803"]["price_rub"]) == Decimal(90047)
    assert Decimal(ref_skus["19576"]["price_rub"]) + Decimal(ref_skus["359841"]["price_rub"]) == Decimal(110199)
    assert Decimal(ref_skus["19578"]["price_rub"]) + Decimal(ref_skus["359843"]["price_rub"]) == Decimal(145274)


def test_golden_open_mode_filtering() -> None:
    holdout_fallback = split_chats.default_holdout_dir() / "holdout_eval.json"
    mvp_cases = run_eval.load_cases(
        suite="open", mode="mvp", golden_open_path=GOLDEN_OPEN_PATH, holdout_path=holdout_fallback
    )
    full_cases = run_eval.load_cases(
        suite="open", mode="full", golden_open_path=GOLDEN_OPEN_PATH, holdout_path=holdout_fallback
    )
    mvp_ids = {c["id"] for c in mvp_cases}
    full_ids = {c["id"] for c in full_cases}
    assert "open-quote-recalc-full" not in mvp_ids
    assert "open-quote-recalc-full" in full_ids
    assert {
        "open-price-61557",
        "open-price-504",
        "open-price-48093",
        "open-branch-b-mail-files-50",
        "open-tender-repeat",
        "open-individual-buyer",
    } <= mvp_ids


# ---------------------------------------------------------------------------
# 3. run_eval.py validators & runner tests
# ---------------------------------------------------------------------------


def _event(seq: int, data: dict, action_id: str = "a1") -> dict:
    return {"seq": seq, "at": "2026-11-26T10:00:00Z", "actionId": action_id, "data": data}


def test_extract_prices_rub_supports_extensions_and_ignores_non_currency() -> None:
    sample = (
        "На 50 сотрудников (12 мес., SKU 19576, 44-ФЗ, скидка 50%) "
        "варианты: 4 900 р., 1.523.654 ₽, 4.900 руб., 1 670,94 ₽, 41,2 тыс. ₽, 0,00 р."
    )
    assert run_eval.extract_prices_rub(sample) == [
        Decimal(4900),
        Decimal(1523654),
        Decimal(4900),
        Decimal("1670.94"),
        Decimal(41200),
        Decimal(0),
    ]


def test_pii_scrubbing_masks_residual_pii_without_touching_product_terms() -> None:
    raw = (
        "Меня зовут Иван Петрович, ООО «Ромашка», ИНН 7701234567, адрес ул. Ленина д. 10, "
        "IP 192.168.10.42, ключ ABCD-1234-EFGH-5678, почта ivan@example.ru, тел +7 (999) 123-45-67. "
        "Нужен MDaemon на 20 пользователей и Kaspersky на 50 адресов."
    )
    scrubbed = pii.scrub_pii(raw)
    for leaked in ("Иван", "Петрович", "Ромашка", "7701234567", "192.168.10.42", "ABCD-1234", "ivan@example.ru", "999"):
        assert leaked not in scrubbed
    assert "MDaemon" in scrubbed
    assert "Kaspersky" in scrubbed
    assert "20 пользователей" in scrubbed


def test_one_liner_fixes_boundary_id_and_strict_clarification() -> None:
    src = {"url": "https://store.softline.ru/sunrav/-61557/", "label": "Softline"}
    # Substring "504" inside "15045" must NOT satisfy expected_offer_ids=["504"].
    events_sub = [
        _event(
            1,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Ваш номер заказа 15045. Помочь оформить?",
                "sources": [src],
            },
        )
    ]
    assert run_eval.validate_case_expectations(events_sub, {"expected_offer_ids": ["504"]})
    # A trailing "?" in purpose="answer" must NOT satisfy expect_clarification=True.
    assert run_eval.validate_case_expectations(events_sub, {"expect_clarification": True})


def test_openapi_schema_validator_catches_contract_violations() -> None:
    validator = run_eval.get_schema_validator()
    assert validator.active is True
    bad_extra_field = [
        _event(
            1,
            {
                "type": "message",
                "author": "agent",
                "purpose": "answer",
                "text": "Цена 4 900 ₽.",
                "sources": [],
                "unexpected_extra": 123,
            },
        )
    ]
    errs = run_eval.validate_contract_events(bad_extra_field, "c1")
    assert any("OpenAPI Event schema violation" in err for err in errs)


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


def test_metrics_latency_economics_and_failed_case_accounting() -> None:
    results = [
        run_eval.CaseResult(
            "c1",
            "open",
            "Просто цена конкретного товара",
            passed=True,
            escalated=False,
            closed_by_agent=True,
            latency_ms=[100.0, 200.0],
            cost_rub=0.40,
            cost_status="exact",
            cost_source="trace",
        ),
        run_eval.CaseResult(
            "c2",
            "open",
            "Госзакупки / процедуры",
            passed=True,
            escalated=True,
            closed_by_agent=False,
            latency_ms=[400.0],
            cost_rub=0.80,
            cost_status="exact",
            cost_source="trace",
        ),
        run_eval.CaseResult(
            "c3",
            "open",
            "Прочее (покупка)",
            passed=False,
            escalated=False,
            closed_by_agent=True,  # summarize_results must still NOT count failed case as closed_by_agent
            timed_out=True,
            latency_ms=[1000.0],
            cost_rub=0.60,
            cost_status="exact",
            cost_source="telemetry",
            errors=["Turn 1 timed out"],
            agent_responses=["Неверный ответ агента"],
        ),
    ]
    weights = run_eval.load_category_weights(SPLIT_SUMMARY_PATH)
    summary = run_eval.summarize_results(results, suite="open", mode="mvp", category_weights=weights)
    assert summary["latency_ms"]["p50"] == 300.0
    assert summary["latency_ms"]["p95"] == 910.0  # timeout latency is included in p95
    assert summary["timed_out_cases"] == 1
    assert summary["closed_by_agent_count"] == 1  # c3 failed, so NOT credited to agent
    assert summary["escalated_count"] == 1
    assert summary["economics"]["avg_cost_per_case_rub"] == 0.6
    assert summary["economics"]["projected_500_cases_half_year_rub"] == 300.0
    assert summary["economics"]["weighted_projected_500_cases_half_year_rub"] is not None
    assert summary["economics"]["load_scenario_500_cases_per_day_rub"] == 300.0
    assert summary["economics"]["cost_status"] == "exact"

    # Without exact stand costs the report says "unavailable" instead of estimating.
    for item in results:
        item.cost_status, item.cost_rub = "unavailable", None
    no_cost = run_eval.summarize_results(results, suite="open", mode="mvp", category_weights=weights)
    assert no_cost["economics"]["cost_status"] == "unavailable"
    assert no_cost["economics"]["avg_cost_per_case_rub"] is None
    assert "unavailable" in run_eval.format_markdown_report(no_cost)

    md = run_eval.format_markdown_report(summary)
    assert "500 cases per half-year" in md
    assert "separate load scenario 500/day" in md
    assert "## Failed Case Responses" in md
    assert "Неверный ответ агента" in md


def test_b1_quiescence_waits_for_trailing_handoff_and_pii_scrubs_holdout() -> None:
    """Verify B1 fix (trailing handoff after message) + PII scrubbing + timeout + cost dedup."""
    received_texts: list[str] = []
    lock = threading.Lock()
    state: dict[str, object] = {"convs": {}}

    class AsyncMockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(self, code: int, payload: dict, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            m = re.match(r"^/api/v1/conversations/([^/]+)/events$", parsed.path)
            if not m:
                self._send(404, {"code": "not_found", "message": "not found"})
                return
            cid = m.group(1)
            after = int(urllib.parse.parse_qs(parsed.query).get("after", ["0"])[0])
            with lock:
                events = state["convs"][cid]["events"]  # type: ignore[index]
                page = events[after : after + 50]
                next_after = after + len(page)
                has_more = next_after < len(events)
            self._send(200, {"events": page, "nextAfter": next_after, "hasMore": has_more})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/v1/conversations":
                cid = f"async-{len(state['convs']) + 1}"  # type: ignore[arg-type]
                with lock:
                    state["convs"][cid] = {"events": []}  # type: ignore[index]
                self._send(201, {"conversationId": cid})
                return
            m = re.match(r"^/api/v1/conversations/([^/]+)/actions$", parsed.path)
            if not m:
                self._send(404, {"code": "not_found", "message": "not found"})
                return
            cid = m.group(1)
            text = str(body.get("text", ""))
            received_texts.append(text)
            if "SLOW_TIMEOUT" in text:
                self._send(202, {"conversationId": cid, "actionId": "a1"})
                return

            with lock:
                events: list[dict] = state["convs"][cid]["events"]  # type: ignore[index]
                events.append(
                    {
                        "seq": 1,
                        "at": "2026-11-26T10:00:00Z",
                        "actionId": "a1",
                        "data": {
                            "type": "message",
                            "author": "customer",
                            "purpose": "input",
                            "text": text,
                            "sources": [],
                        },
                    }
                )
                events.append(
                    {
                        "seq": 2,
                        "at": "2026-11-26T10:00:01Z",
                        "actionId": "a1",
                        "data": {
                            "type": "message",
                            "author": "agent",
                            "purpose": "answer",
                            "text": "Цена по запросу, подключаю менеджера.",
                            "sources": [
                                {"url": "https://store.softline.ru/adobe/65272676ba01a12-504/", "label": "Softline"}
                            ],
                        },
                    }
                )

            def push_delayed_handoff() -> None:
                import time as _t

                _t.sleep(0.05)
                with lock:
                    events.append(
                        {
                            "seq": 3,
                            "at": "2026-11-26T10:00:02Z",
                            "actionId": "a1",
                            "data": {
                                "type": "handoff",
                                "handoff": {
                                    "handoffId": "h-delayed",
                                    "conversationId": cid,
                                    "reason": "price_request",
                                    "question": "Запросить цену 504",
                                    "products": [],
                                    "status": "open",
                                },
                            },
                        }
                    )

            threading.Thread(target=push_delayed_handoff, daemon=True).start()
            self._send(
                202,
                {"conversationId": cid, "actionId": "a1"},
                extra_headers={"X-LLM-Cost-Rub": "0.15"},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), AsyncMockHandler)
    host, port = server.server_address
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = run_eval.T04HttpClient(f"http://{host}:{port}", timeout=2.0)
        catalog = run_eval.load_catalog_index(GOLDEN_OPEN_PATH)

        # 1. Trailing handoff must be captured via quiescence wait, AND PII must be scrubbed on holdout.
        holdout_case = {
            "id": "holdout-pii-check",
            "suite": "holdout",
            "category": "Доступность вендора (можно ли купить)",
            "turns": [
                {
                    "action": {
                        "type": "message",
                        "text": "Я Иван Петрович из ООО «Тест», IP 10.20.30.40, нужна цена Adobe 504",
                    },
                    "assertions": {"expect_handoff": True},
                }
            ],
            "assertions": {"expect_handoff": True},
        }
        res = run_eval.run_case(client, holdout_case, catalog, timeout=2.0, poll_interval=0.02, settle_seconds=0.12)
        assert res.passed is True, res.errors
        assert res.escalated is True
        assert res.events_count == 3
        # X-LLM-Cost-Rub is not part of T04: without manager credentials the cost is unavailable.
        assert res.cost_rub is None
        assert res.cost_status == "unavailable"
        assert "Иван" not in received_texts[0]
        assert "10.20.30.40" not in received_texts[0]

        # 2. Timeout must be recorded in latency_ms and NOT counted as closed_by_agent.
        timeout_case = {
            "id": "timeout-check",
            "suite": "open",
            "category": "Прочее (покупка)",
            "turns": [{"action": {"type": "message", "text": "SLOW_TIMEOUT"}, "assertions": {}}],
            "assertions": {},
        }
        t_res = run_eval.run_case(client, timeout_case, catalog, timeout=0.1, poll_interval=0.02, settle_seconds=0.05)
        assert t_res.passed is False
        assert t_res.timed_out is True
        assert t_res.closed_by_agent is False
        assert len(t_res.latency_ms) == 1 and t_res.latency_ms[0] >= 90.0
    finally:
        server.shutdown()
        server.server_close()


def test_run_eval_cli_full_open_suite_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end HTTP test covering all 14 golden_open.json cases in full mode, with exact stand cost.

    The mock mimics T05: manager login sets support_session, trace is visible only for conversations with a
    handoff (otherwise 404), telemetry returns the last 20 LLM calls.
    """
    golden = json.loads(GOLDEN_OPEN_PATH.read_text(encoding="utf-8"))
    skus = golden["reference_skus"]
    lock = threading.Lock()
    state: dict[str, object] = {"counter": 0, "convs": {}, "calls": [], "logins": 0}
    manager_token = "mgr-" + secrets.token_hex(8)

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

        def _send(self, code: int, payload: object, cookie: str | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(raw)

        def _is_manager(self) -> bool:
            return f"support_session={manager_token}" in (self.headers.get("Cookie") or "")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/v1/manager/"):
                if not self._is_manager():
                    self._send(401, {"code": "unauthenticated", "message": "login"})
                    return
                with lock:
                    calls = list(state["calls"])  # type: ignore[call-overload]
                    convs = state["convs"]  # type: ignore[assignment]
                if parsed.path == "/api/v1/manager/telemetry":
                    self._send(200, {"today_spend_rub": 0.0, "recent_calls": calls[-20:][::-1]})
                    return
                m_trace = re.match(r"^/api/v1/manager/conversations/([^/]+)/trace$", parsed.path)
                if m_trace and convs.get(m_trace.group(1), {}).get("handoff"):  # type: ignore[union-attr]
                    own = [c for c in calls if c["session_id"] == m_trace.group(1)]
                    turns = [
                        {
                            "turn_id": f"t{c['id']}",
                            "total_cost_rub": c["cost_rub"],
                            "steps": [
                                {
                                    "tool": f"llm:{c['step']}",
                                    "args": {"model": c["model"]},
                                    "result_summary": "ok",
                                    "tokens_in": c["tokens_in"],
                                    "tokens_out": c["tokens_out"],
                                    "cost_rub": c["cost_rub"],
                                    "latency_ms": 10,
                                }
                            ],
                        }
                        for c in own
                    ]
                    self._send(200, turns)
                    return
                self._send(404, {"code": "not_found", "message": "no access"})
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

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/v1/auth/manager/login":
                if body == {"username": "manager", "password": "pw-test"}:
                    state["logins"] = int(state["logins"]) + 1  # type: ignore[call-overload]
                    self._send(200, {"role": "manager"}, cookie=f"support_session={manager_token}; Path=/api/v1")
                else:
                    self._send(401, {"code": "unauthenticated", "message": "bad credentials"})
                return
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
                calls: list[dict] = state["calls"]  # type: ignore[assignment]
                calls.append(
                    {
                        "id": len(calls) + 1,
                        "session_id": cid,
                        "step": "agent",
                        "model": "gpt-4.1-mini",
                        "tokens_in": 400,
                        "tokens_out": 100,
                        "cost_rub": 0.25,
                    }
                )

                def push(data: dict) -> None:
                    if data["type"] == "handoff":
                        conv["handoff"] = True
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
                    elif "Я физлицо" in text:
                        msg(
                            "answer",
                            "Позиция MDaemon 19576 (77 556 ₽) предназначена только для юрлиц. "
                            "Для физлица требуется уточнение у оператора.",
                            [prod_obj("19576")["source"]],
                        )
                    elif "покупаем впервые. Нужен MDaemon" in text:
                        p20 = prod_obj("19576")
                        push({"type": "products", "products": [p20]})
                        msg("answer", "MDaemon на 20 пользователей для юрлица стоит 77 556 ₽.", [p20["source"]])
                    elif "Закупаем по тендеру" in text:
                        msg("notice", "Создал обращение менеджеру по тендеру.", [])
                        push(make_handoff(cid, "tender", "Пакет документов для тендера", [prod_obj("19576")]))
                    elif "Повторюсь, пришлите пакет" in text:
                        msg("notice", "Ваше открытое обращение по тендеру уже в работе у менеджера.", [])
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
                        conv["offered_help"] = True
                        msg(
                            "clarification",
                            "В источнике нет подтверждённых сведений. "
                            "Подключить оператора, чтобы он уточнил ваш вопрос?",
                            [],
                        )
                    elif conv.get("offered_help") and "подключайте" in text.lower():
                        msg("notice", "Передал вопрос оператору. Он увидит переписку и ответит в этом чате.", [])
                        push(make_handoff(cid, "insufficient_evidence", "HyperQuantum Nebula ERP 2026", []))
                    elif "Мдеймон" in text:
                        msg(
                            "clarification",
                            "По названию нашёл в каталоге семейство «MDaemon Messaging Server». "
                            "На кого оформляется лицензия и покупаете впервые или продлеваете?",
                            [],
                        )
                    elif "Коммерческая компания, покупаем впервые" in text:
                        p20 = prod_obj("19576")
                        push({"type": "products", "products": [p20]})
                        msg("answer", "Новая покупка MDaemon на 20 пользователей — 77 556 ₽.", [p20["source"]])
                    elif text.strip().lower() == "позовите оператора":
                        if conv["turn"] == 1:
                            msg("clarification", "Подскажите, с каким вопросом нужна помощь?", [])
                        else:
                            msg("notice", "Передал вопрос оператору.", [])
                            push(make_handoff(cid, "customer_request", "Позовите оператора", []))
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
    monkeypatch.setenv("EVAL_MANAGER_PASSWORD", "pw-test")
    monkeypatch.delenv("EVAL_SESSION_COOKIE", raising=False)
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
                "--settle-seconds",
                "0.02",
                "--out",
                str(out_prefix),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert rc == 0
    report = json.loads((tmp_path / "eval_out.json").read_text(encoding="utf-8"))
    assert report["total_cases"] == 16
    assert report["passed_cases"] == 16
    assert report["failed_cases"] == 0
    assert state["logins"] == 1
    econ = report["economics"]
    assert econ["cost_status"] == "exact", econ
    assert econ["cost_sources"] == ["telemetry", "trace"]
    by_id = {c["id"]: c for c in report["cases"]}
    # One mocked LLM call (0.25 RUB) per action: exact cost, no double counting.
    assert by_id["open-tender-repeat"]["cost_rub"] == 0.75
    assert by_id["open-tender-repeat"]["cost_source"] == "trace"
    assert by_id["open-tender-repeat"]["conversation_id"]
    assert len(by_id["open-tender-repeat"]["llm_calls"]) == 3
    assert by_id["open-price-61557"]["cost_rub"] == 0.25
    assert by_id["open-price-61557"]["cost_source"] == "telemetry"
    assert by_id["open-price-61557"]["conversation_id"]
    assert len(by_id["open-price-61557"]["llm_calls"]) == 1
    assert econ["avg_cost_per_case_rub"] == round(sum(c["cost_rub"] for c in report["cases"]) / 16, 4)
    assert (tmp_path / "eval_out.md").is_file()


def test_run_eval_mock_check_cli() -> None:
    assert run_eval.main(["--mock-check", "--golden-open", str(GOLDEN_OPEN_PATH)]) == 0
