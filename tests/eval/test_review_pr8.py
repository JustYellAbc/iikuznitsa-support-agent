"""Regressions for the PR #8 review (T10): action_failed, product substitution, latency start point.

Everything here is synthetic: in-process clients, no stand, no holdout data.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import run_eval  # noqa: E402 - needs the sys.path tweak above

GOLDEN_OPEN_PATH = REPO_ROOT / "eval/golden_open.json"
GOLDEN = json.loads(GOLDEN_OPEN_PATH.read_text(encoding="utf-8"))
CASES = {c["id"]: c for c in GOLDEN["cases"]}
CATALOG = run_eval.load_catalog_index(GOLDEN_OPEN_PATH)
URL_61557 = "https://store.softline.ru/sunrav/-61557/"
HOLDOUT_STD_ASSERTIONS = {"forbid_zero_price": True, "require_sources_on_answer": True, "check_catalog_prices": True}


def _ev(seq: int, data: dict, action_id: str) -> dict:
    return {"seq": seq, "at": "2026-11-26T10:00:00Z", "actionId": action_id, "data": data}


def _input(text: str) -> dict:
    return {"type": "message", "author": "customer", "purpose": "input", "text": text, "sources": []}


def _answer(text: str, url: str = URL_61557) -> dict:
    return {
        "type": "message",
        "author": "agent",
        "purpose": "answer",
        "text": text,
        "sources": [{"url": url, "label": "Карточка Softline"}],
    }


class ScriptedClient:
    """Contract-valid local client: every action immediately appends the scripted events to the journal."""

    def __init__(self, reply, *, submit_delay_s: float = 0.0) -> None:
        self.reply = reply
        self.submit_delay_s = submit_delay_s
        self.events: list[dict] = []
        self.turn = 0

    def create_conversation(self, key: str) -> str:
        return "c1"

    def submit_action(self, cid: str, body: dict, key: str):
        self.turn += 1
        aid = f"a{self.turn}"
        for data in self.reply(body):
            self.events.append(_ev(len(self.events) + 1, data, aid))
        if self.submit_delay_s:
            time.sleep(self.submit_delay_s)  # slow POST: the reply is already in the journal before the 202
        return 202, {"conversationId": "c1", "actionId": aid}, {}

    def read_all_from(self, cid: str, after: int = 0, **_: object):
        return self.events[after:], len(self.events)


def _run(client: ScriptedClient, case: dict):
    return run_eval.run_case(client, case, CATALOG, timeout=3, poll_interval=0.01, settle_seconds=0.1)


# ---------------------------------------------------------------------------
# [P1] action_failed fails the turn and the scenario
# ---------------------------------------------------------------------------


def test_p1_action_failed_fails_scenario_with_holdout_assertions() -> None:
    case = {
        "id": "synthetic-action-failed",
        "suite": "open",
        "category": "price",
        "turns": [{"action": {"type": "message", "text": "Подскажите цену"}, "assertions": HOLDOUT_STD_ASSERTIONS}],
        "assertions": HOLDOUT_STD_ASSERTIONS,
    }
    failed = {"type": "action_failed", "error": {"code": "invalid_input", "message": "Обработка не выполнена"}}
    res = _run(ScriptedClient(lambda body: [failed]), case)

    assert res.passed is False
    assert res.closed_by_agent is False
    assert res.errors
    assert any("action_failed invalid_input: Обработка не выполнена" in e for e in res.errors), res.errors
    assert res.turns[0]["close"] == "action_failed"
    summary = run_eval.summarize_results([res], suite="x", mode="mvp")
    assert summary["passed_cases"] == 0 and summary["closed_by_agent_count"] == 0
    assert summary["cases"][0]["closed_by_agent"] is False
    # action_failed is not a useful reaction: no latency sample for this turn or case (JustYellAbc, PR #8).
    assert res.first_meaningful_latency_ms is None
    assert res.latency_ms == []
    assert res.turns[0]["latency_ms"] is None
    assert summary["latency_ms"] == {"samples": 0, "mean": None, "p50": None, "p95": None}
    assert summary["cases"][0]["latency_ms"] == []


# ---------------------------------------------------------------------------
# [P2] correct article, price and link attached to a different product
# ---------------------------------------------------------------------------


def test_p2_substituted_product_name_fails_open_price_61557() -> None:
    text = "Adobe Photoshop (артикул 61557) стоит 4900 ₽."
    res = _run(ScriptedClient(lambda body: [_input(body["text"]), _answer(text)]), CASES["open-price-61557"])

    assert res.passed is False
    assert res.closed_by_agent is False
    assert any("61557" in e and "Adobe Photoshop" in e for e in res.errors), res.errors


@pytest.mark.parametrize(
    "text",
    [
        "SunRav BookOffice (артикул 61557) стоит 4900 ₽.",
        "BookOffice (артикул 61557) стоит 4900 ₽.",
    ],
)
def test_p2_accepted_spellings_pass_open_price_61557(text: str) -> None:
    res = _run(ScriptedClient(lambda body: [_input(body["text"]), _answer(text)]), CASES["open-price-61557"])
    assert res.passed is True, res.errors


def test_p2_article_without_product_name_and_without_card_fails() -> None:
    errs = run_eval.validate_product_names([_ev(1, _answer("Артикул 61557 стоит 4900 ₽."), "a1")], ["61557"], CATALOG)
    assert any("without its product name" in e for e in errs), errs


def test_p2_product_card_with_foreign_name_fails() -> None:
    sku = GOLDEN["reference_skus"]["61557"]
    card = {
        "offerId": "61557",
        "name": "Adobe Photoshop",
        "source": {"url": sku["url"], "label": "Карточка Softline"},
        "available": True,
        "priceState": "listed",
        "unitPriceKopecks": 490000,
        "licenseTerm": None,
    }
    events = [_ev(1, {"type": "products", "products": [card]}, "a1"), _ev(2, _answer("Цена 4 900 ₽."), "a1")]
    errs = run_eval.validate_case_expectations(events, {"expected_offer_ids": ["61557"]}, CATALOG)
    assert any("Product 61557 name 'Adobe Photoshop'" in e for e in errs), errs


def test_p2_catalog_card_name_and_neutral_text_pass() -> None:
    sku = GOLDEN["reference_skus"]["61557"]
    card = {
        "offerId": "61557",
        "name": sku["name"],
        "source": {"url": sku["url"], "label": "Карточка Softline"},
        "available": True,
        "priceState": "listed",
        "unitPriceKopecks": 490000,
        "licenseTerm": None,
    }
    events = [
        _ev(1, {"type": "products", "products": [card]}, "a1"),
        _ev(2, _answer(f"61557 — 4 900 ₽, карточка: {URL_61557}"), "a1"),
    ]
    assert run_eval.validate_product_names(events, ["61557"], CATALOG) == []


# ---------------------------------------------------------------------------
# [P2] first_meaningful_latency_ms starts before submit_action
# ---------------------------------------------------------------------------


def test_p2_latency_includes_slow_submit_action() -> None:
    client = ScriptedClient(
        lambda body: [_input(body["text"]), _answer("SunRav BookOffice (артикул 61557) стоит 4 900 ₽.")],
        submit_delay_s=0.25,
    )
    res = _run(client, CASES["open-price-61557"])

    assert res.first_meaningful_latency_ms is not None
    assert res.first_meaningful_latency_ms >= 250.0
    assert res.turns[0]["latency_ms"] >= 250.0
    assert res.turns[0]["ack_ms"] >= 250.0
    assert res.turns[0]["post_ack_latency_ms"] < res.turns[0]["latency_ms"]
