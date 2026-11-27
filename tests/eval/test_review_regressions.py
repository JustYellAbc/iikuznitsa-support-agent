"""Regression tests ported from the T10 reviewer scripts (/tmp/t10rev: runner_sim, race, quote, validators,
neg, mock, brute, pii_check). Everything here is synthetic; the only real-data test (PII share on the raw
archive) prints counts and chat IDs, never transcript text.

Findings that the review explicitly allowed to postpone (В2, В3, В4, В6, В9) are pinned as strict xfail:
the suite stays green, and the test flips to XPASS (= failure) once the gap is fixed, so it gets promoted.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
from contextlib import redirect_stdout
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import pii, run_eval, split_chats  # noqa: E402 - needs the sys.path tweak above

GOLDEN_OPEN_PATH = REPO_ROOT / "eval/golden_open.json"
ARCHIVE_PATH = REPO_ROOT / "data/raw/чаты поддержки.zip"
CATALOG_PATH = REPO_ROOT / "data/raw/Mindbox.yml.gz.xml"
GOLDEN = json.loads(GOLDEN_OPEN_PATH.read_text(encoding="utf-8"))
SKUS = GOLDEN["reference_skus"]
CASES = {c["id"]: c for c in GOLDEN["cases"]}
CATALOG = run_eval.load_catalog_index(GOLDEN_OPEN_PATH)


def _ev(seq: int, data: dict, action_id: str = "a1") -> dict:
    return {"seq": seq, "at": "2026-11-26T10:00:00Z", "actionId": action_id, "data": data}


def _agent(text: str, purpose: str = "answer", url: str | None = "https://store.softline.ru/sunrav/-61557/") -> list:
    sources = [{"url": url, "label": "Softline"}] if url else []
    return [_ev(1, {"type": "message", "author": "agent", "purpose": purpose, "text": text, "sources": sources})]


def _input(text: str) -> dict:
    return {"type": "message", "author": "customer", "purpose": "input", "text": text, "sources": []}


def _product(oid: str) -> dict:
    sku = SKUS[oid]
    return {
        "offerId": oid,
        "name": sku["name"],
        "source": {"url": sku["url"], "label": "Карточка Softline"},
        "available": sku["available"],
        "priceState": sku["price_state"],
        "unitPriceKopecks": sku["unit_price_kopecks"],
        "licenseTerm": sku["contract_license_term"],
    }


def _handoff(reason: str, products: list[dict], hid: str = "h1", question: str = "Вопрос оператору") -> dict:
    return {
        "type": "handoff",
        "handoff": {
            "handoffId": hid,
            "conversationId": "c1",
            "reason": reason,
            "question": question,
            "products": products,
            "status": "open",
        },
    }


def _quote(rev: int, ids: list[str], total: int) -> dict:
    lines = [
        {
            "offerId": oid,
            "name": SKUS[oid]["name"],
            "source": {"url": SKUS[oid]["url"], "label": "Карточка Softline"},
            "packages": 1,
            "unitPriceKopecks": SKUS[oid]["unit_price_kopecks"],
        }
        for oid in ids
    ]
    return {
        "type": "quote",
        "quote": {
            "quoteId": "q1",
            "revision": rev,
            "state": "ready",
            "lines": lines,
            "knownSubtotalKopecks": total,
            "totalKopecks": total,
            "terms": {"taxNote": "t", "payment": "p", "delivery": "d", "validUntil": "2026-12-10"},
        },
    }


class Timed:
    """Contract-valid in-process server (runner_sim.py): each action emits (delay_s, data) relative to submit."""

    def __init__(self, script) -> None:
        self.script = script
        self.events: list[dict] = []
        self.pending: list[tuple[float, str, dict]] = []
        self.turn = 0

    def create_conversation(self, key: str) -> str:
        return "c1"

    def submit_action(self, cid: str, body: dict, key: str):
        self.turn += 1
        aid = f"a{self.turn}"
        now = time.perf_counter()
        for delay, data in self.script(self.turn, body):
            self.pending.append((now + delay, aid, data))
        return 202, {"conversationId": "c1", "actionId": aid}, {}

    def read_all_from(self, cid: str, after: int = 0, **_: object):
        now = time.perf_counter()
        due = sorted((p for p in self.pending if p[0] <= now), key=lambda p: p[0])
        for item in due:
            self.pending.remove(item)
            self.events.append(_ev(len(self.events) + 1, item[2], item[1]))
        return self.events[after:], len(self.events)


class OnePerPoll:
    """race.py: the server appends one event per poll (answer, then card, then handoff) — a valid T04 order."""

    def __init__(self, journal: list[dict]) -> None:
        self.journal = journal
        self.events: list[dict] = []

    def create_conversation(self, key: str) -> str:
        return "c1"

    def submit_action(self, cid: str, body: dict, key: str):
        return 202, {"conversationId": "c1", "actionId": "a1"}, {}

    def read_all_from(self, cid: str, after: int = 0, **_: object):
        if len(self.events) < len(self.journal):
            self.events.append(_ev(len(self.events) + 1, self.journal[len(self.events)]))
        return self.events[after:], len(self.events)


def _script_504(handoff_delay: float):
    def script(turn: int, body: dict):
        product = _product("504")
        return [
            (0.0, _input(body["text"])),
            (0.3, {"type": "products", "products": [product]}),
            (
                0.5,
                {
                    "type": "message",
                    "author": "agent",
                    "purpose": "answer",
                    "text": "Цена по запросу, передаю менеджеру.",
                    "sources": [product["source"]],
                },
            ),
            (0.5 + handoff_delay, _handoff("price_request", [product])),
        ]

    return script


# ---------------------------------------------------------------------------
# Б1: turn completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handoff_delay", [0.2, 0.5, 1.0, 2.0, 3.0])
def test_b1_late_handoff_closes_turn_by_expectations_with_default_settle(handoff_delay: float) -> None:
    """runner_sim Q2: with settle=0.4 every delay >= 0.5 s failed; expectations now close the turn."""
    started = time.perf_counter()
    res = run_eval.run_case(
        Timed(_script_504(handoff_delay)), CASES["open-price-504"], CATALOG, timeout=10, poll_interval=0.05
    )
    assert res.passed, res.errors
    assert res.turns[0]["close"] == "expectations"
    assert res.turns[0]["latency_ms"] < 1000  # first useful event (the card) at ~0.3 s
    assert time.perf_counter() - started < handoff_delay + 0.5 + run_eval.DEFAULT_SETTLE_SECONDS + 1.5


def test_b1_one_event_per_poll_is_not_cut_short() -> None:
    """race.py: answer first, then the card, then the handoff, one event per poll."""
    product = _product("504")
    journal = [
        _input("q"),
        {
            "type": "message",
            "author": "agent",
            "purpose": "answer",
            "text": "Цена по запросу.",
            "sources": [product["source"]],
        },
        {"type": "products", "products": [product]},
        _handoff("price_request", []),
    ]
    res = run_eval.run_case(
        OnePerPoll(journal), CASES["open-price-504"], CATALOG, timeout=3, poll_interval=0.01, settle_seconds=0.3
    )
    assert res.passed, res.errors
    assert res.events_count == 4


def test_b1_quote_only_turns_close_without_timeout() -> None:
    """quote.py / runner_sim B1(b): message → input + quote, no agent text (T04 c-docs)."""

    def script(turn: int, body: dict):
        ids, total = (["19576", "359841"], 11019900) if turn == 1 else (["19578", "359843"], 14527400)
        return [(0.0, _input(body["text"])), (0.2, _quote(turn, ids, total))]

    started = time.perf_counter()
    res = run_eval.run_case(
        Timed(script), CASES["open-quote-recalc-full"], CATALOG, timeout=3, poll_interval=0.05, settle_seconds=0.5
    )
    assert res.passed, res.errors
    assert not res.timed_out
    assert [t["close"] for t in res.turns] == ["expectations", "expectations"]
    assert time.perf_counter() - started < 3


@pytest.mark.parametrize("repeat_emits_handoff", [False, True])
def test_b1_tender_repeat_without_new_agent_text(repeat_emits_handoff: bool) -> None:
    """runner_sim: the 3rd turn gets no new agent message (server dedups) or re-sends the same open handoff."""
    product = _product("19576")

    def script(turn: int, body: dict):
        out = [(0.0, _input(body["text"]))]
        if turn == 1:
            out += [
                (0.1, {"type": "products", "products": [product]}),
                (
                    0.2,
                    {
                        "type": "message",
                        "author": "agent",
                        "purpose": "answer",
                        "text": "19576 — 77 556 ₽.",
                        "sources": [product["source"]],
                    },
                ),
            ]
        elif turn == 2:
            out += [
                (
                    0.1,
                    {
                        "type": "message",
                        "author": "agent",
                        "purpose": "notice",
                        "text": "Передал менеджеру.",
                        "sources": [],
                    },
                ),
                (0.2, _handoff("tender", [product], question="Закупаем по тендеру. Пришлите пакет документов.")),
            ]
        elif repeat_emits_handoff:
            out += [(0.1, _handoff("tender", [product], question="Закупаем по тендеру. Пришлите пакет документов."))]
        return out

    res = run_eval.run_case(
        Timed(script), CASES["open-tender-repeat"], CATALOG, timeout=3, poll_interval=0.05, settle_seconds=0.3
    )
    assert res.passed, res.errors
    assert not res.timed_out
    assert res.turns[2]["close"] == ("quiet" if repeat_emits_handoff else "quiet_silent")


def test_b1_silent_turn_without_allow_silent_still_times_out() -> None:
    """A slow agent must not be cut by the quiet fallback before it says anything."""

    def script(turn: int, body: dict):
        return [(0.0, _input(body["text"]))]

    res = run_eval.run_case(
        Timed(script), CASES["open-price-61557"], CATALOG, timeout=0.6, poll_interval=0.05, settle_seconds=0.1
    )
    assert res.timed_out and not res.passed and not res.closed_by_agent
    summary = run_eval.summarize_results([res], suite="x", mode="mvp")
    assert summary["timed_out_cases"] == 1
    assert summary["latency_ms"]["p95"] is None


def test_b1_late_events_of_previous_turn_are_checked_with_that_turn() -> None:
    """Events of turn 1 that arrive while turn 2 runs are attributed to turn 1 after the final drain."""
    product = _product("61557")

    def script(turn: int, body: dict):
        if turn == 1:
            return [
                (0.0, _input(body["text"])),
                (0.05, {"type": "products", "products": [product]}),
                (
                    0.1,
                    {
                        "type": "message",
                        "author": "agent",
                        "purpose": "answer",
                        "text": "4 900 ₽",
                        "sources": [product["source"]],
                    },
                ),
                (
                    0.6,
                    {"type": "message", "author": "agent", "purpose": "notice", "text": "Или 5 555 ₽.", "sources": []},
                ),
            ]
        return [
            (0.0, _input(body["text"])),
            (
                0.1,
                {
                    "type": "message",
                    "author": "agent",
                    "purpose": "answer",
                    "text": "ok",
                    "sources": [product["source"]],
                },
            ),
        ]

    case = {
        "id": "late",
        "turns": [
            {
                "action": {"type": "message", "text": "1"},
                "assertions": {"expected_offer_ids": ["61557"], "allowed_prices_rub": ["4900"]},
            },
            {"action": {"type": "message", "text": "2"}, "assertions": {}},
        ],
        "assertions": {},
    }
    res = run_eval.run_case(Timed(script), case, CATALOG, timeout=3, poll_interval=0.02, settle_seconds=0.8)
    assert any(err.startswith("Turn 1: Hallucinated or disallowed price in agent text: 5555") for err in res.errors), (
        res.errors
    )


def test_q6_cost_rub_in_event_violates_schema_and_is_never_read() -> None:
    """runner_sim Q6: data.cost_rub breaks Event (additionalProperties: false); the runner ignores it."""
    bad = _agent("Цена 4 900 ₽.")
    bad[0]["data"]["cost_rub"] = 0.12
    assert any("OpenAPI Event schema violation" in e for e in run_eval.validate_contract_events(bad, "c1"))

    product = _product("61557")

    def script(turn: int, body: dict):
        msg = {
            "type": "message",
            "author": "agent",
            "purpose": "answer",
            "text": "4 900 ₽",
            "sources": [product["source"]],
            "cost_rub": 1.0,
        }
        return [(0.0, _input(body["text"])), (0.0, {"type": "products", "products": [product]}), (0.05, msg)]

    case = {
        "id": "cost",
        "turns": [{"action": {"type": "message", "text": "61557"}, "assertions": {}}],
        "assertions": {},
    }
    res = run_eval.run_case(Timed(script), case, CATALOG, timeout=2, poll_interval=0.02, settle_seconds=0.2)
    assert res.cost_rub is None and res.cost_status == "unavailable"
    assert not res.passed


# ---------------------------------------------------------------------------
# T04 0.3.0: agent_busy / rate_limited, manager session and cost
# ---------------------------------------------------------------------------


class _StandHandler(BaseHTTPRequestHandler):
    """Tiny T05-like stand: first action → 409 agent_busy, second → 429 rate_limited, then 202."""

    state: ClassVar[dict] = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        st = self.state
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/v1/manager/"):
            st["manager_cookies"].append(self.headers.get("Cookie"))
            if "support_session=given-cookie" not in (self.headers.get("Cookie") or ""):
                self._send(401, {"code": "unauthenticated", "message": "login"})
            elif path.endswith("/trace"):
                self._send(200, [{"turn_id": "t1", "total_cost_rub": 0.3}, {"turn_id": "t2", "total_cost_rub": 0.2}])
            else:
                self._send(200, {"recent_calls": []})
            return
        after = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("after", ["0"])[0])
        events = st["events"]
        self._send(200, {"events": events[after:], "nextAfter": len(events), "hasMore": False})

    def do_POST(self) -> None:
        st = self.state
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/v1/conversations":
            self._send(201, {"conversationId": "c1"})
            return
        st["keys"].append(self.headers.get("Idempotency-Key"))
        st["attempts"] += 1
        if st["attempts"] == 1:
            self._send(409, {"code": "agent_busy", "message": "Агент ещё отвечает."})
            return
        if st["attempts"] == 2:
            self._send(429, {"code": "rate_limited", "message": "Слишком часто."}, {"Retry-After": "1"})
            return
        product = _product("61557")
        st["events"].extend(
            [
                _ev(1, _input(body["text"])),
                _ev(2, {"type": "products", "products": [product]}),
                _ev(
                    3,
                    {
                        "type": "message",
                        "author": "agent",
                        "purpose": "answer",
                        "text": "4 900 ₽",
                        "sources": [product["source"]],
                    },
                ),
            ]
        )
        self._send(202, {"conversationId": "c1", "actionId": "a1"})


def test_t04_030_busy_and_rate_limit_are_retried_with_same_key_and_cost_comes_from_trace() -> None:
    _StandHandler.state = {"events": [], "keys": [], "attempts": 0, "manager_cookies": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StandHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        client = run_eval.T04HttpClient(base, timeout=5.0)
        cost = run_eval.ManagerCostClient(base, session_cookie="given-cookie")
        cost.login()  # a given cookie skips POST /auth/manager/login
        case = {
            "id": "busy",
            "turns": [
                {"action": {"type": "message", "text": "61557"}, "assertions": {"expected_offer_ids": ["61557"]}}
            ],
            "assertions": {},
        }
        res = run_eval.run_case(
            client, case, CATALOG, timeout=5, poll_interval=0.02, settle_seconds=0.1, cost_client=cost
        )
    finally:
        server.shutdown()
        server.server_close()
    st = _StandHandler.state
    assert res.passed, res.errors
    assert st["attempts"] == 3 and len(set(st["keys"])) == 1  # rejected commands do not consume the key
    assert client.rejections == ["agent_busy", "rate_limited"]
    assert (res.cost_status, res.cost_source, res.cost_rub) == ("exact", "trace", 0.5)
    assert [t.get("trace_cost_rub") for t in res.turns] == [0.3]
    assert all(c == "support_session=given-cookie" for c in st["manager_cookies"])


def test_manager_client_requires_credentials_and_telemetry_window_must_cover_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        run_eval.ManagerCostClient("http://127.0.0.1:9")
    client = run_eval.ManagerCostClient("http://127.0.0.1:9", session_cookie="x")
    calls = [{"id": i, "session_id": "other", "cost_rub": 0.1} for i in range(20)]
    monkeypatch.setattr(
        client, "_get", lambda path: (404, {}) if path.endswith("/trace") else (200, {"recent_calls": calls})
    )
    # 20 calls and no overlap with the pre-case snapshot: the window may have dropped this case's calls.
    info = client.conversation_cost("c1", baseline={("id", 999)})
    assert info.status == "unavailable" and "does not cover" in str(info.reason)
    # Overlap with the snapshot proves the window covers the whole case.
    info = client.conversation_cost("c1", baseline={("id", 0)})
    assert (info.status, info.source, info.total_rub) == ("exact", "telemetry", 0.0)


def test_schema_validation_is_mandatory_unless_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    validator = run_eval.T04SchemaValidator(REPO_ROOT / "eval/t04_openapi.yaml")
    assert validator.spec_version == "0.3.0" == run_eval.EXPECTED_T04_VERSION
    assert validator.validate("Error", {"code": "agent_busy", "message": "x"}) == []
    assert validator.validate("Error", {"code": "rate_limited", "message": "x"}) == []

    monkeypatch.setattr(run_eval, "yaml", None)
    try:
        with pytest.raises(run_eval.SchemaValidationUnavailableError):
            run_eval.T04SchemaValidator()
        with pytest.raises(SystemExit, match="PyYAML and jsonschema are required"):
            run_eval.main(["--mock-check"])
        assert not run_eval.T04SchemaValidator(enabled=False).active
        out = io.StringIO()
        with redirect_stdout(out):
            assert run_eval.main(["--mock-check", "--no-schema-validation"]) == 0
        assert "schema validation disabled" in out.getvalue()
    finally:
        monkeypatch.undo()
        run_eval.set_schema_validator(run_eval.T04SchemaValidator())


def test_requirements_declare_schema_dependencies() -> None:
    reqs = (REPO_ROOT / "eval/requirements.txt").read_text(encoding="utf-8").lower()
    assert "pyyaml" in reqs and "jsonschema" in reqs


# ---------------------------------------------------------------------------
# Amounts (review §5 and В1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 670,94 ₽", "1670.94"),
        ("1670.94 ₽", "1670.94"),
        ("1 670.94 ₽", "1670.94"),
        ("1.670,94 ₽", "1670.94"),
        ("1,670.94 ₽", "1670.94"),
        ("1.5 тыс. ₽", "1500"),
        ("41,2 тыс. ₽", "41200"),
        ("41,227 тыс. ₽", "41227"),
        ("4.900 ₽", "4900"),
        ("4900 р.", "4900"),
        ("4 900 рублей", "4900"),
        ("4,9 тыс. руб", "4900"),
        ("4,9 млн ₽", "4900000"),
        ("1 рубль", "1"),
        ("Вариант 2. 4 900 ₽", "4900"),
    ],
)
def test_amounts_are_parsed_exactly(text: str, expected: str) -> None:
    assert run_eval.extract_prices_rub(text) == [Decimal(expected)]


@pytest.mark.parametrize("text", ["1,670,94 ₽", "1.670.94 ₽", "12,3456 ₽", "1 670.940 ₽", "1 67 0 ₽", "41,227,5 ₽"])
def test_ambiguous_amounts_are_parse_errors_not_truncations(text: str) -> None:
    prices, errors = run_eval.parse_prices_rub(text)
    assert prices == [] and errors
    with pytest.raises(run_eval.PriceParseError):
        run_eval.extract_prices_rub(text)
    assert any(
        "Price parse error" in e
        for e in run_eval.validate_case_expectations(_agent(text), {"allowed_prices_rub": ["1670.94"]})
    )


def test_price_assertions_from_validators_script() -> None:
    ok = [
        "Цена 4 900 ₽.",
        "Цена 4900 руб.",
        "Цена 4,9 тыс. ₽",
        "Цена 4 900,00 ₽",
        "Цена 4900 р.",
        "Цена 4.900 ₽",
        "Итого 4 900 руб., НДС не облагается",
    ]
    for text in ok:
        assert (
            run_eval.validate_case_expectations(
                _agent(text), {"allowed_prices_rub": ["4900"], "required_prices_rub": ["4900"]}, CATALOG
            )
            == []
        ), text
    assert run_eval.validate_case_expectations(_agent("Цена 5 тыс. ₽"), {"allowed_prices_rub": ["4900"]}, CATALOG)


@pytest.mark.xfail(
    strict=True, reason="В1 (остаток): числа без валюты, ₽ перед числом, «р» без точки, нижняя граница диапазона"
)
@pytest.mark.parametrize("text", ["Цена 4 900,00", "Цена ₽4900", "Цена 4900 р", "Цена от 1 000 до 4 900 ₽"])
def test_known_gap_v1_price_forms(text: str) -> None:
    errs = run_eval.validate_case_expectations(
        _agent(text), {"allowed_prices_rub": ["4900"], "required_prices_rub": ["4900"]}, CATALOG
    )
    if "от 1 000" in text:
        assert errs  # 1 000 must be checked as a price too
    else:
        assert errs == []


# ---------------------------------------------------------------------------
# Text validators (validators.py, neg.py)
# ---------------------------------------------------------------------------


def test_offer_id_boundary_and_strict_clarification() -> None:
    assert run_eval.validate_case_expectations(
        _agent("Похожий товар стоит 15045 руб."), {"expected_offer_ids": ["504"]}
    )
    assert run_eval.validate_case_expectations(_agent("Цена 77 556 ₽. Чем ещё помочь?"), {"expect_clarification": True})


def test_self_check_detects_a_disabled_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    """validators.py: under python -O a broken validator must still be detected (RuntimeError, not assert)."""
    monkeypatch.setattr(run_eval, "validate_anti_zero_price", lambda *a, **k: [])
    with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError, match="Self-check failed"):
        run_eval.run_mock_self_check(GOLDEN_OPEN_PATH)


@pytest.mark.xfail(
    strict=True, reason="В2: срок у 61557 — «365 дней», «один год», «вечная», «не указан, обычно 12 мес»"
)
@pytest.mark.parametrize(
    "text",
    [
        "Действует 365 дней.",
        "Срок — один год.",
        "Срок 24 месяца.",
        "Лицензия вечная.",
        "Срок не указан, обычно 12 мес.",
        "Лицензия бессрочная, верно?",
    ],
)
def test_known_gap_v2_term_hallucinations_missed(text: str) -> None:
    assert run_eval.validate_anti_term_hallucination(_agent(text))


@pytest.mark.xfail(strict=True, reason="В2: ложное срабатывание на отрицание «бессрочной назвать нельзя»")
def test_known_gap_v2_term_negation_false_positive() -> None:
    assert (
        run_eval.validate_anti_term_hallucination(_agent("Срок в карточке не указан. Бессрочной её назвать нельзя."))
        == []
    )


@pytest.mark.xfail(strict=True, reason="В3: «0,00» без валюты, «Цена: 0», «ноль рублей» не ловятся")
@pytest.mark.parametrize("text", ["Цена 0,00", "Цена: 0", "Цена ноль рублей"])
def test_known_gap_v3_zero_forms(text: str) -> None:
    assert run_eval.validate_case_expectations(_agent(text), {"forbid_zero_price": True}, CATALOG)


@pytest.mark.xfail(strict=True, reason="В3: «бесплатно» ложно срабатывает на бесплатную подготовку КП")
def test_known_gap_v3_free_word_false_positive() -> None:
    handoff = _ev(2, _handoff("price_request", []))
    assert (
        run_eval.validate_anti_zero_price(
            _agent("Цена по запросу, менеджер бесплатно подготовит КП.") + [handoff], "504"
        )
        == []
    )


@pytest.mark.xfail(strict=True, reason="В4: отрицания считаются нарушениями")
@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        ("INSTANT_DELIVERY_RE", "Сразу отгрузить не сможем."),
        ("FAKE_STANDARD_CLAIM_RE", "MDaemon Standard в каталоге нет."),
        ("DISCOUNT_PROMISE_RE", "Оформить со скидкой 50% не можем."),
    ],
)
def test_known_gap_v4_negation_false_positives(pattern: str, text: str) -> None:
    assert not getattr(run_eval, pattern).search(text)


@pytest.mark.xfail(strict=True, reason="В4: пропуски обещаний и номеров реестра")
@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        ("DISCOUNT_PROMISE_RE", "Хорошо, сделаю скидку 50%."),
        ("FAKE_REGISTRY_NUMBER_RE", "Продукт входит в реестр (№ 12345)."),
        ("FAKE_REGISTRY_NUMBER_RE", "Запись № 4321 в реестре Минцифры."),
    ],
)
def test_known_gap_v4_missed_promises(pattern: str, text: str) -> None:
    assert getattr(run_eval, pattern).search(text)


@pytest.mark.xfail(strict=True, reason="В4: оговорку о юрлице засчитывает любое упоминание менеджера")
def test_known_gap_v4_weak_legal_entity_caveat() -> None:
    text = "Цена 77 556 ₽, оформляйте. Если что — менеджер на связи."
    assert run_eval.validate_case_expectations(_agent(text), {"expect_legal_entity_caveat": True})


@pytest.mark.xfail(strict=True, reason="В9: проверка цен holdout по всему каталогу принимает случайные круглые суммы")
def test_known_gap_v9_holdout_catalog_check_is_weak() -> None:
    if not CATALOG_PATH.is_file():
        pytest.skip("catalog XML not present")
    full = run_eval.load_catalog_index(GOLDEN_OPEN_PATH, CATALOG_PATH, load_full_xml=True)
    assert run_eval.validate_case_expectations(_agent("Итого 41 227 ₽"), {"check_catalog_prices": True}, full) == []
    assert run_eval.validate_case_expectations(_agent("Цена 5 000 ₽"), {"check_catalog_prices": True}, full)


# ---------------------------------------------------------------------------
# В6: the --mock-check server is not a contract implementation (mock.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_server():
    server, base = run_eval._make_mock_t04_server()
    yield run_eval.T04HttpClient(base, timeout=5.0), base
    server.shutdown()
    server.server_close()


@pytest.mark.xfail(strict=True, reason="В6: mock игнорирует Idempotency-Key")
def test_known_gap_v6_mock_idempotency(mock_server) -> None:
    client, _ = mock_server
    assert client.create_conversation("same-key") == client.create_conversation("same-key")


@pytest.mark.xfail(strict=True, reason="В6: повторный request_operator создаёт новую передачу")
def test_known_gap_v6_mock_duplicate_handoff(mock_server) -> None:
    client, _ = mock_server
    cid = client.create_conversation("k1")
    client.submit_action(cid, {"type": "request_operator"}, key="op1")
    client.submit_action(cid, {"type": "request_operator"}, key="op2")
    events, _ = client.read_all_from(cid)
    assert len({e["data"]["handoff"]["handoffId"] for e in events if e["data"]["type"] == "handoff"}) == 1


@pytest.mark.xfail(strict=True, reason="В6: after=-1 не отклоняется с 422")
def test_known_gap_v6_mock_negative_cursor(mock_server) -> None:
    client, base = mock_server
    cid = client.create_conversation("k1")
    status, _, _ = client._request("GET", f"{base}/api/v1/conversations/{cid}/events?after=-1")
    assert status == 422


# ---------------------------------------------------------------------------
# Б2: seed and holdout isolation (brute.py)
# ---------------------------------------------------------------------------


def test_b2_seed_must_come_from_env_and_be_128_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_SPLIT_SEED", raising=False)
    with pytest.raises(ValueError, match="not set"):
        split_chats.resolve_seed()
    for weak in ("20261126", "0" * 16, secrets.token_hex(15), "zz" * 20):
        monkeypatch.setenv("EVAL_SPLIT_SEED", weak)
        with pytest.raises(ValueError, match="128 bits"):
            split_chats.resolve_seed()
    with pytest.raises(SystemExit):
        monkeypatch.setenv("EVAL_SPLIT_SEED", "20261126")
        split_chats.main([])
    assert not hasattr(split_chats, "DEFAULT_SEED")


def test_b2_summary_publishes_no_seed_fingerprint() -> None:
    """brute.py recovered the old seed from seed_sha256_prefix in 9 827 tries; nothing derived from it is left."""
    summary_text = (REPO_ROOT / "eval/split_summary.json").read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    assert not {"seed", "seed_sha256_prefix", "seed_source"} & set(summary)
    assert not re.search(r"\b[0-9a-f]{12,}\b", summary_text.replace(summary["archive_sha256"], ""))
    readme = (REPO_ROOT / "eval/README.md").read_text(encoding="utf-8")
    assert "20261126" not in readme and "EVAL_SPLIT_SEED=2" not in readme


def test_b2_split_order_depends_on_secret_seed(tmp_path: Path) -> None:
    if not ARCHIVE_PATH.is_file():
        pytest.skip("Raw support archive not present")
    _, records = split_chats.parse_archive(ARCHIVE_PATH)
    clusters = split_chats.cluster_records(records)
    holdouts = []
    for _ in range(2):
        _, holdout, _ = split_chats.split_records(
            records, clusters, archive_filename="a.zip", archive_sha256="x", seed=secrets.token_hex(16)
        )
        holdouts.append(set(holdout["ids"]))
    assert holdouts[0] != holdouts[1]


def test_b2_holdout_dir_outside_repo_and_tmp_without_private_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for bad in (REPO_ROOT / "data/private", REPO_ROOT / "x", Path("/tmp/iikuznitsa-eval-private"), tmp_path / "h"):
        with pytest.raises(ValueError):
            split_chats.check_holdout_dir(bad)
    monkeypatch.delenv("EVAL_HOLDOUT_FILE", raising=False)
    monkeypatch.setenv("EVAL_HOLDOUT_DIR", str(tmp_path / "ext"))
    assert run_eval.default_holdout_file() == tmp_path / "ext/holdout_eval.json"
    assert "data/private" not in Path(run_eval.__file__).read_text(encoding="utf-8")
    monkeypatch.setenv("EVAL_SPLIT_SEED", secrets.token_hex(16))
    with pytest.raises(SystemExit, match="temp directory"):
        split_chats.main(["--holdout-dir", str(tmp_path / "h")])


# ---------------------------------------------------------------------------
# eval/pii.py on lower-case text (pii_check.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "leaks"),
    [
        ("дарья, добрый день! меня зовут анна, хотим купить", ["дарья", "анна"]),
        ("инн 7701234567, test@example.ru андрей спасибо", ["андрей", "7701234567", "test@"]),
        ("ответьте андрею или дмитрию, натальей ивановной займусь", ["андрею", "дмитрию", "натальей", "ивановной"]),
        ("с уважением, петрова мария сергеевна", ["петрова", "мария", "сергеевна"]),
        ("отправил сидорову а.а. и а.б. кузнецову", ["сидорову", "кузнецову"]),
        ("контактное лицо: лилия, ип смирнов", ["лилия", "смирнов"]),
        ('ооо «ромашка-строй» и ао "гамма-страхование" и ооо нпк вектор просит счёт', ["ромашка", "гамма", "вектор"]),
        ("есть лицензия z3dy5-nsth4-4t8nm-r7h2h и ключ продукта ab12c-34def", ["z3dy5", "ab12c"]),
        ("почтовый сервер 192.168.10.42 недоступен", ["192.168.10.42"]),
    ],
)
def test_pii_masks_lowercase_names_orgs_keys_ips(raw: str, leaks: list[str]) -> None:
    scrubbed = pii.scrub_pii(raw)
    for leak in leaks:
        assert leak not in scrubbed, (leak, scrubbed)


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("ао «лаборатория касперского», ооо «1с» и пао «софтлайн»", ["касперского", "1с", "софтлайн"]),
        ("АО «Лаборатория Касперского» и ООО «1С»", ["Касперского", "1С"]),
        ("astra linux 1.7.4.1 и версия 2.3.0.1", ["1.7.4.1", "2.3.0.1"]),
        ("подписка на журнал роман-газета", ["роман-газета"]),
        ("поставка до 8 марта, алиса и аврора, т.е. завтра", ["марта", "алиса", "аврора", "т.е."]),
        (
            "ип или юрлицо, kaspersky на 50 пк, 44-фз, 2024-2025-2026",
            ["ип или", "kaspersky", "44-фз", "2024-2025-2026"],
        ),
    ],
)
def test_pii_keeps_vendors_versions_and_product_terms(raw: str, kept: list[str]) -> None:
    scrubbed = pii.scrub_pii(raw)
    for token in kept:
        assert token in scrubbed, (token, scrubbed)


def test_pii_real_archive_share_of_masked_names_and_orgs() -> None:
    """Real-data check (review §4). Uses the reviewer's independent detectors; prints only counts and chat IDs."""
    if not ARCHIVE_PATH.is_file():
        pytest.skip("Raw support archive not present")
    _, records = split_chats.parse_archive(ARCHIVE_PATH)
    names = (
        "александр алексей анастасия андрей анна антон артем артём вадим валентин валерий василий виктор виктория "
        "виталий владимир владислав вячеслав галина георгий григорий дарья денис дмитрий евгений евгения егор "
        "екатерина елена иван игорь илья ирина кирилл константин ксения лариса лилия людмила максим маргарита марина "
        "мария михаил надежда наталья наталия никита николай оксана олег ольга павел петр пётр полина роман руслан "
        "светлана сергей станислав татьяна тимур федор фёдор юлия юрий яна алина альбина вера нина зоя валентина "
        "эльвира гульнара ринат рустам айгуль"
    ).split()
    name_re = re.compile(r"(?<![\w-])(?:" + "|".join(names) + r")(?:а|у|ом|е|ой|и|ю|я|ей)?(?![\w-])")
    org_re = re.compile(
        r"(?<![\w-])(?:ооо|зао|пао|оао|ао|ип|фгбу|гбу|мбу|мбоу|гбоу|фгуп|муп|ано)\s+(?P<name>[«\"'][^»\"'\n]{1,80}[»\"']?|[\w-]+)"
    )
    stop_re = re.compile(rf"^{pii._ORG_STOP}$")

    raw_names = raw_orgs = 0
    residual_names: list[int] = []
    residual_orgs: list[int] = []
    labels = {"имя": 0, "организация": 0}
    for rec in records:
        scrubbed = pii.scrub_pii(rec.text)
        raw_names += bool(name_re.search(rec.text))
        raw_orgs += bool(org_re.search(rec.text))
        if name_re.search(scrubbed):
            residual_names.append(rec.chat_id)
        if any(
            not pii.is_vendor_name(m.group("name")) and not stop_re.match(m.group("name"))
            for m in org_re.finditer(scrubbed)
        ):
            residual_orgs.append(rec.chat_id)
        for label in labels:
            labels[label] += scrubbed.count(f"[{label}]")

    assert raw_names >= 50 and raw_orgs >= 20  # the archive really contains names and organisations
    name_share = 1 - len(residual_names) / raw_names
    org_share = 1 - len(residual_orgs) / raw_orgs
    assert name_share >= 0.95, f"names masked {name_share:.0%}; residual chat ids: {residual_names}"
    assert org_share >= 0.95, f"organisations masked {org_share:.0%}; residual chat ids: {residual_orgs}"
    assert labels["имя"] >= raw_names and labels["организация"] >= 30, labels


def test_pilot_publication_and_incomplete_quote_do_not_resolve():
    pilot = _agent("Готовлю ответ, оператор проверит его и пришлёт сюда.", "notice")
    answer = _agent("Ответ согласован")
    assert not run_eval._is_meaningful_event(pilot[0])
    assert not run_eval._turn_has_response(pilot)
    assert run_eval._is_meaningful_event(answer[0])
    assert not run_eval._has_confirmed_agent_resolution(pilot + answer, "a1")
    pending = _ev(3, {"type": "quote", "quote": {"state": "needs_review", "totalKopecks": None}})
    assert not run_eval._has_confirmed_agent_resolution(answer + [pending], "a1")


def test_sunrav_requires_consistent_answer_and_explicit_term():
    rules = CASES["open-price-61557"]["assertions"]
    card = _ev(1, {"type": "products", "products": [_product("61557")]})

    def errors(text, with_card):
        return run_eval.validate_case_expectations(([card] if with_card else []) + _agent(text), rules, CATALOG)

    assert errors("Adobe Photoshop стоит 4900 ₽. Срок неизвестен.", True)
    assert errors("SunRav BookOffice стоит 4900 ₽.", True)
    assert not errors("SunRav BookOffice стоит 4900 ₽. Срок неизвестен.", False)


def test_repeat_handoff_validates_latest_card():
    rules = CASES["open-tender-repeat"]["assertions"]
    good = _handoff("tender", [_product("19576")], question="Тендер. Пришлите документы.")
    broken = _handoff("tender", [], question="Как выбрать калькулятор?")
    errors = run_eval.validate_case_expectations([_ev(1, good), _ev(2, broken)], rules, CATALOG)
    assert any("handoff.products" in e for e in errors)
    assert any("handoff question" in e for e in errors)


def test_public_mixed_report_hides_holdout_errors():
    results = [
        run_eval.CaseResult(
            case_id="open-example", suite="open", category="open", passed=True, escalated=False, closed_by_agent=False
        ),
        run_eval.CaseResult(
            case_id="private-id",
            suite="holdout",
            category="private",
            passed=False,
            escalated=False,
            closed_by_agent=False,
            errors=["private-content"],
        ),
    ]
    report = run_eval.summarize_results(results, suite="all", mode="mvp")
    public = run_eval.format_markdown_report(report, mask_holdout_ids=True, hide_holdout_details=True)
    assert "open-example" in public
    assert "private-content" not in public and "private-id" not in public
    assert report["failed_cases"] == 1


@pytest.mark.parametrize("publish", [True, False])
def test_pilot_latency_starts_at_submit_and_ends_at_publication(publish):
    def script(_turn, body):
        events = [
            (0, _input(body["text"])),
            (0.01, _agent("Готовлю ответ, оператор проверит его и пришлёт сюда.", "notice")[0]["data"]),
        ]
        if publish:
            events.append((0.10, _agent("Срок указан в карточке")[0]["data"]))
        return events

    case = {"id": "pilot-example", "suite": "open", "turns": [{"action": {"type": "message", "text": "Какой срок?"}}]}
    res = run_eval.run_case(Timed(script), case, CATALOG, timeout=0.5, poll_interval=0.005, settle_seconds=0.01)
    assert not res.closed_by_agent
    if publish:
        assert res.passed, res.errors
        assert res.first_meaningful_latency_ms >= 90
    else:
        assert res.first_meaningful_latency_ms is None and res.timed_out
