"""Regressions for the PR #8 review (T10): action_failed, product substitution, latency start point.

Everything here is synthetic: in-process clients, no stand, no holdout data.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


# ---------------------------------------------------------------------------
# [P1] Holdout source IDs must never leak to stand headers or CLI output
# ---------------------------------------------------------------------------


def test_p1_holdout_id_never_leaks_to_stand_headers_or_cli_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_num = "987654321"
    secret_id = f"holdout-{secret_num}"
    holdout_file = tmp_path / "holdout_eval.json"
    holdout_file.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": secret_id,
                        "suite": "holdout",
                        "sanitize_pii": True,
                        "modes": ["mvp", "full"],
                        "category": "Просто цена конкретного товара",
                        "turns": [
                            {
                                "action": {"type": "message", "text": "Подскажите цену SunRav BookOffice 61557?"},
                                "assertions": HOLDOUT_STD_ASSERTIONS,
                            }
                        ],
                        "assertions": HOLDOUT_STD_ASSERTIONS,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    recorded_requests: list[dict] = []
    action_keys_seen: list[str | None] = []
    state = {"action_attempts": 0, "events": []}

    class SpyHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            recorded_requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": ""})
            if self.path.startswith("/api/v1/conversations/c1/events"):
                after = int(self.path.split("after=")[-1]) if "after=" in self.path else 0
                evs = state["events"][after:]
                self._send(200, {"events": evs, "nextAfter": after + len(evs), "hasMore": False})
                return
            self._send(404, {"code": "not_found", "message": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            recorded_requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": raw})
            if self.path == "/api/v1/conversations":
                self._send(201, {"conversationId": "c1"})
                return
            if re.match(r"^/api/v1/conversations/c1/actions$", self.path):
                action_keys_seen.append(self.headers.get("Idempotency-Key"))
                state["action_attempts"] += 1
                if state["action_attempts"] == 1:
                    self._send(409, {"code": "agent_busy", "message": "Previous turn is still running"})
                    return
                body = json.loads(raw)
                state["events"].append(_ev(1, _input(body["text"]), "a1"))
                state["events"].append(_ev(2, _answer("SunRav BookOffice (61557) стоит 4 900 ₽."), "a1"))
                self._send(202, {"conversationId": "c1", "actionId": "a1"})
                return
            self._send(404, {"code": "not_found", "message": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), SpyHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out_prefix = tmp_path / "private_reports" / "holdout_out"
    try:
        rc = run_eval.main(
            [
                "--base-url",
                f"http://{host}:{port}",
                "--suite",
                "holdout",
                "--mode",
                "mvp",
                "--holdout-file",
                str(holdout_file),
                "--golden-open",
                str(GOLDEN_OPEN_PATH),
                "--out",
                str(out_prefix),
                "--poll-interval",
                "0.01",
                "--settle-seconds",
                "0.02",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    captured = capsys.readouterr()
    assert rc == 0

    # Source holdout ID must not appear in ANY HTTP request (headers, Idempotency-Key, path, body).
    wire_blob = json.dumps(recorded_requests, ensure_ascii=False)
    assert secret_id not in wire_blob
    assert secret_num not in wire_blob

    # Retry on 409 agent_busy reused the exact same technical Idempotency-Key.
    assert len(action_keys_seen) == 2
    assert action_keys_seen[0] is not None
    assert action_keys_seen[0] == action_keys_seen[1]

    # Source holdout ID must not appear in CLI stdout or stderr; holdout CLI prints only aggregates.
    assert secret_id not in captured.out
    assert secret_num not in captured.out
    assert secret_id not in captured.err
    assert secret_num not in captured.err
    assert "| Total cases | 1 |" in captured.out
    assert "## Failed Case Responses" not in captured.out
    assert "SunRav BookOffice" not in captured.out

    # Private report inside holdout output directory keeps the alias -> source ID mapping and conversation_id.
    private_report = json.loads(Path(f"{out_prefix}.json").read_text(encoding="utf-8"))
    alias = private_report["cases"][0]["alias"]
    assert alias.startswith("holdout-alias-")
    assert private_report["cases"][0]["id"] == secret_id
    assert private_report["cases"][0]["conversation_id"] == "c1"
    assert private_report["holdout_alias_map"] == {alias: secret_id}


# ---------------------------------------------------------------------------
# [P1] HyperQuantum (offer help then consent) + new open cases (typo, 2x operator)
# ---------------------------------------------------------------------------


def _handoff_ev(reason: str, question: str, products: list[dict] | None = None, hid: str = "h1") -> dict:
    return {
        "type": "handoff",
        "handoff": {
            "handoffId": hid,
            "conversationId": "c1",
            "reason": reason,
            "question": question,
            "products": products or [],
            "status": "open",
        },
    }


def _clarify(text: str) -> dict:
    return {"type": "message", "author": "agent", "purpose": "clarification", "text": text, "sources": []}


def test_p1_hyperquantum_requires_help_offer_then_consent_before_handoff() -> None:
    case = CASES["open-robust-unknown-product"]

    # Premature handoff on turn 1 (before customer consent) must FAIL.
    premature = _run(
        ScriptedClient(
            lambda body: [
                _input(body["text"]),
                _clarify("В источнике нет сведений. Подключить оператора?"),
                _handoff_ev("outside_catalog", body["text"]),
            ]
        ),
        case,
    )
    assert premature.passed is False
    assert any("Turn 1:" in e and "handoff" in e.lower() for e in premature.errors), premature.errors

    # Turn 1 offers operator help with 0 handoffs; turn 2 consent creates exactly 1 handoff -> PASS.
    turns = iter(
        [
            lambda body: [
                _input(body["text"]),
                _clarify("В источнике нет подтверждённых сведений. Подключить оператора, чтобы он уточнил ваш вопрос?"),
            ],
            lambda body: [
                _input(body["text"]),
                {
                    "type": "message",
                    "author": "agent",
                    "purpose": "notice",
                    "text": "Передал оператору.",
                    "sources": [],
                },
                _handoff_ev("insufficient_evidence", "HyperQuantum Nebula ERP 2026"),
            ],
        ]
    )
    valid = _run(ScriptedClient(lambda body: next(turns)(body)), case)
    assert valid.passed is True, valid.errors


def test_p1_typo_mdaemon_and_operator_twice_cases() -> None:
    # 1. Typo "Мдеймон": clarification without MDaemon family mention fails; proper 2-turn flow passes.
    typo_case = CASES["open-typo-mdaemon"]
    sku_19576 = GOLDEN["reference_skus"]["19576"]
    card_19576 = {
        "offerId": "19576",
        "name": sku_19576["name"],
        "source": {"url": sku_19576["url"], "label": "Карточка Softline"},
        "available": True,
        "priceState": "listed",
        "unitPriceKopecks": 7755600,
        "licenseTerm": "12 мес.",
    }
    bad_typo_turns = iter(
        [
            lambda body: [_input(body["text"]), _clarify("Уточните параметры покупки?")],
            lambda body: [
                _input(body["text"]),
                {"type": "products", "products": [card_19576]},
                _answer("Стоимость 77 556 ₽.", sku_19576["url"]),
            ],
        ]
    )
    bad_typo = _run(ScriptedClient(lambda body: next(bad_typo_turns)(body)), typo_case)
    assert bad_typo.passed is False
    assert any("mdaemon" in e.lower() for e in bad_typo.errors), bad_typo.errors

    good_typo_turns = iter(
        [
            lambda body: [
                _input(body["text"]),
                _clarify("По названию нашёл семейство MDaemon Messaging Server. Юрлицо или физлицо?"),
            ],
            lambda body: [
                _input(body["text"]),
                {"type": "products", "products": [card_19576]},
                _answer("MDaemon на 20 пользователей стоит 77 556 ₽.", sku_19576["url"]),
            ],
        ]
    )
    good_typo = _run(ScriptedClient(lambda body: next(good_typo_turns)(body)), typo_case)
    assert good_typo.passed is True, good_typo.errors

    # 2. Two "Позовите оператора" messages in empty chat:
    # Handoff on turn 1 must FAIL; clarification (0 handoffs) on turn 1 + 1 handoff on turn 2 must PASS.
    op_case = CASES["open-robust-operator-twice"]
    bad_op_turns = iter(
        [
            lambda body: [_input(body["text"]), _handoff_ev("customer_request", "Позовите оператора", hid="h1")],
            lambda body: [_input(body["text"]), _handoff_ev("customer_request", "Позовите оператора", hid="h1")],
        ]
    )
    bad_op = _run(ScriptedClient(lambda body: next(bad_op_turns)(body)), op_case)
    assert bad_op.passed is False
    assert any("Turn 1:" in e for e in bad_op.errors), bad_op.errors

    good_op_turns = iter(
        [
            lambda body: [_input(body["text"]), _clarify("Подскажите, с каким вопросом нужна помощь?")],
            lambda body: [
                _input(body["text"]),
                {
                    "type": "message",
                    "author": "agent",
                    "purpose": "notice",
                    "text": "Передал оператору.",
                    "sources": [],
                },
                _handoff_ev("customer_request", "Позовите оператора", hid="h1"),
            ],
        ]
    )
    good_op = _run(ScriptedClient(lambda body: next(good_op_turns)(body)), op_case)
    assert good_op.passed is True, good_op.errors


# ---------------------------------------------------------------------------
# [P2] Quote recalculation must keep same quoteId and strictly increase revision
# ---------------------------------------------------------------------------


def _quote_ev(qid: str, rev: int, oids: list[str], total: int) -> dict:
    lines = []
    for oid in oids:
        sku = GOLDEN["reference_skus"][oid]
        lines.append(
            {
                "offerId": oid,
                "name": sku["name"],
                "source": {"url": sku["url"], "label": "Карточка Softline"},
                "packages": 1,
                "unitPriceKopecks": sku["unit_price_kopecks"],
            }
        )
    return {
        "type": "quote",
        "quote": {
            "quoteId": qid,
            "revision": rev,
            "state": "ready",
            "lines": lines,
            "knownSubtotalKopecks": total,
            "totalKopecks": total,
            "terms": {"taxNote": "С НДС", "payment": "Предоплата", "delivery": "3 дня", "validUntil": "2026-12-10"},
        },
    }


def test_p2_quote_recalc_rejects_different_quote_id_or_same_revision() -> None:
    case = CASES["open-quote-recalc-full"]

    def run_pair(qid1: str, rev1: int, oids1: list[str], qid2: str, rev2: int, oids2: list[str]):
        turns = iter(
            [
                lambda body: [_input(body["text"]), _quote_ev(qid1, rev1, oids1, 11019900)],
                lambda body: [_input(body["text"]), _quote_ev(qid2, rev2, oids2, 14527400)],
            ]
        )
        return _run(ScriptedClient(lambda body: next(turns)(body)), case)

    # Valid recalculation: q1/rev1 -> q1/rev2 with expected items and totals -> PASS.
    ok = run_pair("q1", 1, ["19576", "359841"], "q1", 2, ["19578", "359843"])
    assert ok.passed is True, ok.errors

    # Different quoteId (q1/rev1 -> q2/rev1) must FAIL.
    diff_id = run_pair("q1", 1, ["19576", "359841"], "q2", 1, ["19578", "359843"])
    assert diff_id.passed is False
    assert any("quoteId" in e for e in diff_id.errors), diff_id.errors

    # Same quoteId but non-increasing revision (q1/rev1 -> q1/rev1) must FAIL.
    same_rev = run_pair("q1", 1, ["19576", "359841"], "q1", 1, ["19578", "359843"])
    assert same_rev.passed is False
    assert any("revision" in e for e in same_rev.errors), same_rev.errors


# ---------------------------------------------------------------------------
# [P1] Clarification-only or manager-only reply must NEVER count as closed_by_agent
# ---------------------------------------------------------------------------


def test_p1_clarification_or_manager_reply_never_credits_closed_by_agent() -> None:
    case = {
        "id": "synthetic-price-only",
        "suite": "holdout",
        "category": "Просто цена конкретного товара",
        "turns": [{"action": {"type": "message", "text": "Подскажите цену?"}, "assertions": HOLDOUT_STD_ASSERTIONS}],
        "assertions": HOLDOUT_STD_ASSERTIONS,
    }
    clar_res = _run(ScriptedClient(lambda body: [_input(body["text"]), _clarify("Назовите версию")]), case)
    assert clar_res.passed is True, clar_res.errors
    assert clar_res.escalated is False
    assert clar_res.closed_by_agent is False

    mgr_reply = {
        "type": "message",
        "author": "manager",
        "purpose": "answer",
        "text": "Ответил менеджер: цена 4 900 ₽.",
        "sources": [{"url": URL_61557, "label": "Карточка Softline"}],
    }
    mgr_res = _run(ScriptedClient(lambda body: [_input(body["text"]), mgr_reply]), case)
    assert mgr_res.passed is True, mgr_res.errors
    assert mgr_res.escalated is False
    assert mgr_res.closed_by_agent is False

    summary = run_eval.summarize_results([clar_res, mgr_res], suite="open", mode="mvp")
    assert summary["passed_cases"] == 2
    assert summary["closed_by_agent_count"] == 0
    assert summary["closed_by_agent_rate"] == 0.0
    assert all(c["closed_by_agent"] is False for c in summary["cases"])
    md = run_eval.format_markdown_report(summary)
    assert "| agent |" not in md
    assert "| unresolved |" in md


# ---------------------------------------------------------------------------
# [P1] open-tender-repeat must verify handoff.products and handoff.question
# ---------------------------------------------------------------------------


def test_p1_open_tender_repeat_checks_handoff_product_and_question() -> None:
    case = CASES["open-tender-repeat"]
    sku = GOLDEN["reference_skus"]["19576"]
    prod_19576 = {
        "offerId": "19576",
        "name": sku["name"],
        "source": {"url": sku["url"], "label": "Карточка Softline"},
        "available": True,
        "priceState": "listed",
        "unitPriceKopecks": 7755600,
        "licenseTerm": "12 мес.",
    }

    def run_tender(handoff_products: list[dict], handoff_question: str):
        turns = iter(
            [
                lambda body: [
                    _input(body["text"]),
                    {"type": "products", "products": [prod_19576]},
                    _answer("MDaemon на 20 пользователей стоит 77 556 ₽.", sku["url"]),
                ],
                lambda body: [
                    _input(body["text"]),
                    {
                        "type": "message",
                        "author": "agent",
                        "purpose": "notice",
                        "text": "Передал менеджеру.",
                        "sources": [],
                    },
                    _handoff_ev("tender", handoff_question, products=handoff_products, hid="h1"),
                ],
                lambda body: [_input(body["text"])],
            ]
        )
        return _run(ScriptedClient(lambda body: next(turns)(body)), case)

    # Empty products=[] or unrelated question must FAIL.
    bad = run_tender([], "Как выбрать калькулятор?")
    assert bad.passed is False
    assert any("19576" in e and "handoff.products" in e for e in bad.errors), bad.errors
    assert any("handoff question" in e for e in bad.errors), bad.errors

    # Valid SKU + price + URL + tender question must PASS.
    ok = run_tender([prod_19576], "Закупаем по тендеру. Пришлите пакет документов.")
    assert ok.passed is True, ok.errors


# ---------------------------------------------------------------------------
# [P1] Product substitution when article appears ONLY in URL
# ---------------------------------------------------------------------------


def test_p1_url_only_article_catches_product_substitution() -> None:
    case = CASES["open-price-61557"]
    spoofed = "Adobe Photoshop стоит 4 900 ₽. https://store.softline.ru/sunrav/-61557/"
    bad = _run(ScriptedClient(lambda body: [_input(body["text"]), _answer(spoofed)]), case)
    assert bad.passed is False
    assert any("61557" in e and "Adobe Photoshop" in e for e in bad.errors), bad.errors

    legit = "SunRav BookOffice стоит 4 900 ₽. https://store.softline.ru/sunrav/-61557/"
    ok = _run(ScriptedClient(lambda body: [_input(body["text"]), _answer(legit)]), case)
    assert ok.passed is True, ok.errors


# ---------------------------------------------------------------------------
# [P2] Late action_failed after a meaningful answer must be excluded from latency
# ---------------------------------------------------------------------------


def test_p2_late_action_failed_after_answer_excluded_from_latency() -> None:
    case = {
        "id": "synthetic-late-fail",
        "suite": "open",
        "category": "Просто цена конкретного товара",
        "turns": [{"action": {"type": "message", "text": "Сколько стоит?"}, "assertions": HOLDOUT_STD_ASSERTIONS}],
        "assertions": HOLDOUT_STD_ASSERTIONS,
    }
    failed = {"type": "action_failed", "error": {"code": "internal_error", "message": "Сбой после ответа"}}

    class LateFailClient:
        def __init__(self) -> None:
            self.polls = 0
            self.events: list[dict] = []

        def create_conversation(self, key: str) -> str:
            return "c-late"

        def submit_action(self, cid: str, body: dict, key: str):
            self.events.append(_ev(1, _input(body["text"]), "a1"))
            self.events.append(_ev(2, _answer("SunRav BookOffice стоит 4 900 ₽."), "a1"))
            return 202, {"conversationId": cid, "actionId": "a1"}, {}

        def read_all_from(self, cid: str, after: int = 0, **_: object):
            self.polls += 1
            # Answer arrives on poll 1; action_failed arrives later on poll 3 (during settle/drain).
            if self.polls == 3 and len(self.events) == 2:
                self.events.append(_ev(3, failed, "a1"))
            return self.events[after:], len(self.events)

    res = run_eval.run_case(LateFailClient(), case, CATALOG, timeout=3, poll_interval=0.01, settle_seconds=0.05)
    assert res.passed is False
    assert res.closed_by_agent is False
    assert res.latency_ms == []
    assert res.first_meaningful_latency_ms is None
    assert res.turns[0]["close"] == "action_failed"
    assert res.turns[0]["latency_ms"] is None

    summary = run_eval.summarize_results([res], suite="open", mode="mvp")
    assert summary["latency_ms"] == {"samples": 0, "mean": None, "p50": None, "p95": None}


# ---------------------------------------------------------------------------
# [P2] T19 export (conversation_id + llm_calls including []) & holdout console privacy
# ---------------------------------------------------------------------------


def test_p2_t19_export_and_holdout_console_aggregates_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_response = "СЕКРЕТНЫЙ_ФРАГМЕНТ_ПРОВАЛЕННОГО_ОТВЕТА"
    holdout_file = tmp_path / "holdout_eval.json"
    holdout_file.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "holdout-zero-llm-111",
                        "suite": "holdout",
                        "sanitize_pii": True,
                        "modes": ["mvp", "full"],
                        "category": "Просто цена конкретного товара",
                        "turns": [
                            {
                                "action": {"type": "message", "text": "Нужен SunRav 61557"},
                                "assertions": HOLDOUT_STD_ASSERTIONS,
                            }
                        ],
                        "assertions": HOLDOUT_STD_ASSERTIONS,
                    },
                    {
                        "id": "holdout-fail-222",
                        "suite": "holdout",
                        "sanitize_pii": True,
                        "modes": ["mvp", "full"],
                        "category": "Прочее (покупка)",
                        "turns": [
                            {
                                "action": {"type": "message", "text": "Проверить сбой"},
                                "assertions": HOLDOUT_STD_ASSERTIONS,
                            }
                        ],
                        "assertions": HOLDOUT_STD_ASSERTIONS,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state: dict = {"counter": 0, "convs": {}, "calls": []}
    token = "mgr-tok"

    class T19Handler(BaseHTTPRequestHandler):
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

        def do_GET(self) -> None:
            if self.path == "/api/v1/manager/telemetry":
                self._send(200, {"today_spend_rub": 0.1, "recent_calls": list(reversed(state["calls"]))})
                return
            m_tr = re.match(r"^/api/v1/manager/conversations/([^/]+)/trace$", self.path)
            if m_tr:
                cid = m_tr.group(1)
                if cid == "conv-1":
                    # Deterministic T07 turn: 0 LLM calls in trace!
                    self._send(
                        200,
                        [
                            {
                                "turn_id": "t1",
                                "total_cost_rub": 0.0,
                                "steps": [
                                    {
                                        "tool": "catalog_search",
                                        "args": {},
                                        "result_summary": "ok",
                                        "tokens_in": 0,
                                        "tokens_out": 0,
                                        "cost_rub": 0.0,
                                        "latency_ms": 5,
                                    }
                                ],
                            }
                        ],
                    )
                    return
                self._send(404, {"code": "not_found", "message": "no trace"})
                return
            m_ev = re.match(r"^/api/v1/conversations/([^/]+)/events", self.path)
            if m_ev:
                cid = m_ev.group(1)
                after = int(self.path.split("after=")[-1]) if "after=" in self.path else 0
                evs = state["convs"][cid][after:]
                self._send(200, {"events": evs, "nextAfter": after + len(evs), "hasMore": False})
                return
            self._send(404, {"code": "not_found", "message": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/api/v1/conversations":
                state["counter"] += 1
                cid = f"conv-{state['counter']}"
                state["convs"][cid] = []
                self._send(201, {"conversationId": cid})
                return
            m_act = re.match(r"^/api/v1/conversations/([^/]+)/actions$", self.path)
            if m_act:
                cid = m_act.group(1)
                if cid == "conv-1":
                    state["convs"][cid].append(_ev(1, _input(body["text"]), "a1"))
                    state["convs"][cid].append(_ev(2, _answer("SunRav BookOffice стоит 4 900 ₽."), "a1"))
                else:
                    state["calls"].append(
                        {
                            "id": 1,
                            "session_id": cid,
                            "step": "agent_loop",
                            "model": "gpt-4.1-mini",
                            "tokens_in": 500,
                            "tokens_out": 120,
                            "cost_rub": 0.1,
                            "latency_ms": 40,
                        }
                    )
                    state["convs"][cid].append(_ev(1, _input(body["text"]), "a1"))
                    # Missing sources -> fails holdout assertion so response text goes to Failed Case Responses.
                    state["convs"][cid].append(
                        _ev(
                            2,
                            {
                                "type": "message",
                                "author": "agent",
                                "purpose": "answer",
                                "text": f"{secret_response} 0 ₽",
                                "sources": [],
                            },
                            "a1",
                        )
                    )
                self._send(202, {"conversationId": cid, "actionId": "a1"})
                return
            self._send(404, {"code": "not_found", "message": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), T19Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("EVAL_SESSION_COOKIE", token)
    out_prefix = tmp_path / "private_holdout" / "report"
    try:
        rc = run_eval.main(
            [
                "--base-url",
                f"http://{host}:{port}",
                "--suite",
                "holdout",
                "--mode",
                "mvp",
                "--holdout-file",
                str(holdout_file),
                "--golden-open",
                str(GOLDEN_OPEN_PATH),
                "--out",
                str(out_prefix),
                "--poll-interval",
                "0.01",
                "--settle-seconds",
                "0.02",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    captured = capsys.readouterr()
    assert rc == 1  # second case failed

    # Console for holdout prints aggregates only: no failed response texts and no source holdout IDs.
    assert "| Total cases | 2 |" in captured.out
    assert secret_response not in captured.out
    assert "holdout-zero-llm-111" not in captured.out
    assert "holdout-fail-222" not in captured.out
    assert "## Failed Case Responses" not in captured.out

    # Private report in holdout dir contains full details, conversation_id, and llm_calls (including [] for 0 LLM).
    private_md = Path(f"{out_prefix}.md").read_text(encoding="utf-8")
    assert secret_response in private_md

    report = json.loads(Path(f"{out_prefix}.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in report["cases"]}
    c1 = by_id["holdout-zero-llm-111"]
    assert c1["conversation_id"] == "conv-1"
    assert c1["cost_status"] == "exact"
    assert c1["llm_calls"] == []
    assert c1["turns"][0]["steps"][0]["tool"] == "catalog_search"

    c2 = by_id["holdout-fail-222"]
    assert c2["conversation_id"] == "conv-2"
    assert c2["cost_status"] == "exact"
    assert c2["llm_calls"] == [
        {
            "step": "agent_loop",
            "model": "gpt-4.1-mini",
            "tokens_in": 500,
            "tokens_out": 120,
            "cost_rub": 0.1,
            "latency_ms": 40,
        }
    ]
