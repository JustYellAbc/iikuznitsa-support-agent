"""T19 with qwen3.8-flash: llm_call() -> llm_calls/trace -> eval cost reader -> calculator -> official sheet."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import openpyxl
import pytest

from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say
from app.config import get_settings
from app.core import llm
from app.db.database import close_db, init_db
from tests.test_unit_economics import (
    NINE_CATEGORIES,
    _create_official_organizer_template,
    _eval_official_sheet_total,
    _write_synthetic_pricing,
    _write_synthetic_split_summary,
    ue,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import run_eval  # noqa: E402

QWEN = "qwen3.8-flash"
CATEGORY = NINE_CATEGORIES[0]
QWEN_PRICE = {
    "input_per_1m": 0,
    "output_per_1m": 0,
    "currency": "RUB",
    "source_url": "https://example.invalid/opencode-go",
    "date": "2026-11-27",
    "note": "подписка, 0 ₽ за токены",
}


def tool(name, **arguments):
    return {
        "tool_calls": [
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    }


@pytest.fixture
def qwen(monkeypatch):
    """Stand profile of PR #33 with a zero token rate and a scripted provider; an int item is an HTTP error."""
    script: list = []
    monkeypatch.setenv("AGENT_ENGINE", "tools")
    monkeypatch.setenv("MOCK_LLM", "false")
    monkeypatch.setenv("LLM_BASE_URL", "https://test.invalid/v1")
    for profile in ("SMART", "FAST"):
        monkeypatch.setenv(f"MODEL_{profile}", QWEN)
        monkeypatch.setenv(f"MODEL_{profile}_PRICE_IN_USD", "0")
        monkeypatch.setenv(f"MODEL_{profile}_PRICE_OUT_USD", "0")
    get_settings.cache_clear()

    def respond(request):
        item = script.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, text="busy")
        return httpx.Response(
            200,
            json={
                "model": QWEN,
                "choices": [{"message": {"role": "assistant", **item}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2000, "completion_tokens": 100},
            },
        )

    monkeypatch.setattr(llm, "_client", httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return script


def cost_reader(api) -> run_eval.ManagerCostClient:
    """The eval runner's manager cost reader, pointed at the in-process stand."""
    api.login()
    reader = run_eval.ManagerCostClient("http://stand.invalid", session_cookie="unused")

    def get(path):
        r = api.call("manager", "GET", "/api/v1" + path)
        return r.status_code, r.json()

    reader._get = get
    return reader


def measure(api, reader, text) -> tuple[str, run_eval.CostInfo]:
    baseline = reader.snapshot()
    cid = api.new_conversation()
    say(api, cid, text)
    return cid, reader.conversation_cost(cid, baseline)


def case_result(case_id, cid, cost, *, escalated) -> run_eval.CaseResult:
    r = run_eval.CaseResult(
        case_id=case_id,
        suite="open",
        category=CATEGORY,
        passed=True,
        escalated=escalated,
        closed_by_agent=not escalated,
        conversation_id=cid,
    )
    r.cost_status, r.cost_source, r.cost_reason = cost.status, cost.source, cost.reason
    r.cost_rub, r.llm_calls = cost.total_rub, cost.llm_calls
    return r


@pytest.mark.usefixtures("index")
def test_qwen_zero_rate_is_exact_cost_and_fills_the_official_sheet(api, qwen, tmp_path):
    reader = cost_reader(api)

    # Handoff after one model call that needed an HTTP retry: the manager reads the trace.
    qwen.extend([503, tool("get_offer", offer_id="ask")])
    handoff_cid, handoff = measure(api, reader, "Сколько стоит артикул ask?")
    # Answer without handoff: trace is closed to the manager, the 20-call telemetry window covers the case.
    qwen.extend([tool("get_offer", offer_id="new20"), tool("respond", kind="product", refs=["r1"], field="price")])
    answer_cid, answer = measure(api, reader, "Подскажите цену по артикулу new20.")
    # Deterministic route: no LLM call, confirmed zero.
    quote_cid, quote = measure(api, reader, "Пришлите КП")

    call = {"step": "support_tools", "model": QWEN, "tokens_in": 2000, "tokens_out": 100, "cost_rub": 0.0}
    assert (handoff.status, handoff.source, handoff.total_rub) == ("exact", "trace", 0.0)
    assert [{k: c[k] for k in (*call, "attempts")} for c in handoff.llm_calls] == [{**call, "attempts": 2}]
    assert (answer.status, answer.source, answer.total_rub) == ("exact", "telemetry", 0.0)
    assert [{k: c[k] for k in call} for c in answer.llm_calls] == [call, call]
    assert (quote.status, quote.source, quote.total_rub, quote.llm_calls) == ("exact", "telemetry", 0, [])

    telemetry = api.call("manager", "GET", "/api/v1/manager/telemetry").json()
    [stats] = telemetry["steps"]
    assert (stats["step"], stats["calls"], stats["total_cost_rub"]) == ("support_tools", 3, 0.0)
    assert {(c["model"], c["tokens_in"], c["tokens_out"], c["cost_rub"]) for c in telemetry["recent_calls"]} == {
        (QWEN, 2000, 100, 0.0)
    }
    trace = api.call("manager", "GET", f"/api/v1/manager/conversations/{handoff_cid}/trace").json()
    [llm_step] = [s for turn in trace for s in turn["steps"] if s["tool"].startswith("llm:")]
    assert llm_step["args"]["model"] == QWEN and llm_step["args"]["attempts"] == 2

    summary = run_eval.summarize_results(
        [
            case_result("handoff", handoff_cid, handoff, escalated=True),
            case_result("answer", answer_cid, answer, escalated=False),
            case_result("quote", quote_cid, quote, escalated=False),
        ],
        suite="open",
        mode="mvp",
    )
    assert summary["economics"]["cost_status"] == "exact"
    report = tmp_path / "t34_eval_report.json"
    report.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    template, formulas = _create_official_organizer_template(tmp_path / "official.xlsx", cbr_rate=90.0)
    pricing = _write_synthetic_pricing(tmp_path / "pricing.yaml", overrides={"models": {QWEN: QWEN_PRICE}})
    out_xlsx, out_md = tmp_path / "t19.xlsx", tmp_path / "t19.md"
    split = _write_synthetic_split_summary(tmp_path / "split.json")
    argv = ["--eval-report", str(report), "--split-summary", str(split), "--pricing", str(pricing)]
    argv += ["--template", str(template), "--xlsx-out", str(out_xlsx), "--report-out", str(out_md)]
    assert ue.main(argv) == 0

    wb = openpyxl.load_workbook(out_xlsx)
    ws = wb["Расчёт"]
    row = [ws.cell(row=9, column=c).value for c in range(1, 9)]
    assert row == ["support_tools", QWEN, 1.0, 2000.0, 100.0, 0.0, 0.0, "₽"]
    assert ws["A10"].value is None and ws["I3"].value == 90.0
    assert all(ws[coord].value == formula for coord, formula in formulas.items())
    assert _eval_official_sheet_total(ws) == 0
    assumptions, prices, infra = (ws.cell(row=r, column=1).value for r in (17, 18, 19))
    assert "Без LLM (детерминированные маршруты, 0 ₽): 33.3%, с LLM: 66.7% (1 и 2 диалогов)" in assumptions
    assert f"{QWEN}: 0/0 RUB за 1 млн, подписка, 0 ₽ за токены" in prices
    assert "HTTP-повторов к провайдеру 1 (по трассе для 1 из 3 вызовов)" in prices
    assert "500 обр./180 дн." in infra
    wb.close()

    md = out_md.read_text(encoding="utf-8")
    assert "| Средняя переменная стоимость 1 обращения (взвешенная), ₽ | 0.0000 |" in md
    assert "| Инфраструктура за период (суточная ставка × 180 дн., не зависит от числа обращений), ₽ | 27000.00 |" in md
    assert "| Замерено обращений без LLM (детерминированные маршруты, 0 ₽) | 1 |" in md
    assert "| LLM-вызовов в сутки (500 обр./сут.) | 500.00 |" in md


@pytest.mark.usefixtures("index")
def test_turn_longer_than_the_telemetry_window_stays_unavailable(api, qwen, tmp_path):
    """Since PR #49 one buyer turn may take 30 model calls; without a handoff only 20 are visible."""
    reader = cost_reader(api)
    qwen.extend([tool("get_offer", offer_id="new20") for _ in range(20)])
    qwen.append(tool("respond", kind="product", refs=["r1"], field="price"))
    long_cid, long_turn = measure(api, reader, "Подскажите цену по артикулу new20.")
    assert long_turn.status == "unavailable" and "does not cover the case" in long_turn.reason
    quote_cid, quote = measure(api, reader, "Пришлите КП")
    assert quote.status == "exact" and quote.llm_calls == []

    results = [
        case_result("long", long_cid, long_turn, escalated=False),
        case_result("quote", quote_cid, quote, escalated=False),
    ]
    summary = run_eval.summarize_results(results, suite="open", mode="mvp")
    assert summary["economics"]["cost_status"] == "partial"
    report = tmp_path / "report.json"
    report.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    split = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(
        _write_synthetic_pricing(tmp_path / "pricing.yaml", overrides={"models": {QWEN: QWEN_PRICE}})
    )
    res = ue.compute_unit_economics(
        ue.load_measurements(eval_report_paths=[report], telemetry_paths=[], split_summary=split),
        split,
        pricing,
        usd_rub_rate=None,
    )
    assert (res.measured_dialogs, res.total_dialogs) == (1, 2)
    assert res.net_savings_period_rub is None
    assert any("Телеметрия вызовов есть у 1 из 2 обращений" in w for w in res.telemetry_warnings)


@pytest.fixture
async def db(tmp_path):
    await init_db(tmp_path / "test.db")
    yield
    llm.set_http_client(None)
    await close_db()


@pytest.mark.anyio
async def test_mock_llm_calls_are_rejected_instead_of_priced(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_SMART", QWEN)
    get_settings.cache_clear()
    resp = await llm.llm_call([{"role": "user", "content": "Цена?"}], step="support_tools")
    assert resp.to_step_trace().args == {"model": f"mock/{QWEN}", "attempts": 1}

    report = tmp_path / "mock_report.json"
    calls = [{"step": "support_tools", "model": resp.model, "tokens_in": resp.tokens_in, "tokens_out": 1}]
    report.write_text(
        json.dumps({"cases": [{"id": "m", "category": CATEGORY, "cost_status": "exact", "llm_calls": calls}]}),
        encoding="utf-8",
    )
    split = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    with pytest.raises(ue.InputDataError, match="MOCK_LLM=true"):
        ue.load_measurements(eval_report_paths=[report], telemetry_paths=[], split_summary=split)


def test_zero_rate_is_priced_but_an_unknown_model_still_fails(tmp_path):
    split = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(
        _write_synthetic_pricing(tmp_path / "pricing.yaml", overrides={"models": {QWEN: QWEN_PRICE}})
    )
    assert pricing.models[QWEN].input_per_1m == 0 and pricing.models[QWEN].note == "подписка, 0 ₽ за токены"

    steps = [{"tool": "llm:support_tools", "args": {"model": QWEN, "attempts": 3}, "tokens_in": 900, "tokens_out": 50}]
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cases": [{"id": "c", "category": CATEGORY, "cost_status": "exact", "turns": [{"steps": steps}]}]}),
        encoding="utf-8",
    )
    dialogs = ue.load_measurements(eval_report_paths=[report], telemetry_paths=[], split_summary=split)
    res = ue.compute_unit_economics(dialogs, split, pricing, usd_rub_rate=None)
    assert res.active_variable_cost_rub == 0 and res.period_variable_cost_rub == 0
    [step] = res.steps
    assert (step.model, step.calls_per_request, step.avg_tokens_in, step.price_in_per_1m) == (QWEN, 1, 900, 0)
    assert (res.provider_retries, res.calls_with_attempts) == (2, 1)

    other = ue.DialogMeasurement("d", CATEGORY, (ue.LLMCallRecord("support_tools", "qwen3.7-plus", 900, 50),))
    with pytest.raises(ue.ConfigError, match="'qwen3.7-plus'.*отсутствует в pricing.yaml"):
        ue.compute_unit_economics([other], split, pricing, usd_rub_rate=None)


def test_deterministic_and_llm_dialogs_keep_their_weighted_shares(tmp_path):
    split = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))
    paid = ue.LLMCallRecord("support_tools", "synth-fast-rub", 1000, 0)  # 0.10 ₽
    # Archive weights 200:100 -> 2/3 and 1/3 of the measured categories.
    dialogs = [
        ue.DialogMeasurement("zero", NINE_CATEGORIES[0], ()),
        ue.DialogMeasurement("two-calls", NINE_CATEGORIES[0], (paid, paid)),
        ue.DialogMeasurement("one-call", NINE_CATEGORIES[1], (paid,)),
    ]
    res = ue.compute_unit_economics(dialogs, split, pricing, usd_rub_rate=None)

    def q(value):
        return ue._q(value, "0.0001")

    # 2/3 * (0 + 0.2) / 2 + 1/3 * 0.1 = 0.1 ₽; share with LLM 2/3 * 1/2 + 1/3 * 1 = 2/3.
    assert q(res.active_variable_cost_rub) == Decimal("0.1000")
    assert (res.zero_llm_dialogs, res.llm_dialogs, res.total_llm_calls) == (1, 2, 3)
    assert q(res.llm_dialog_share) == Decimal("0.6667")
    assert q(res.avg_cost_per_llm_dialog_rub) == Decimal("0.1500")
    assert q(res.llm_dialog_share * res.avg_cost_per_llm_dialog_rub) == q(res.active_variable_cost_rub)
    assert q(res.llm_calls_per_request) == Decimal("1.0000")
    assert q(res.load_scenario.daily_llm_calls) == Decimal("500.0000")
    assert q(res.load_scenario.daily_variable_cost_rub) == Decimal("50.0000")
    assert res.calls_with_attempts == 0
    md = ue.render_markdown_report(res, pricing)
    assert "| Доля обращений без LLM (взвешенная, как средняя стоимость), % | 33.33% |" in md
    assert "| Средняя переменная стоимость обращения с LLM, ₽ | 0.1500 |" in md
    assert "| LLM-вызовов на 1 обращение с LLM | 1.5000 |" in md
    assert "н/д: в выгрузке нет трассы" in md

    unweighted = ue.compute_unit_economics(
        [ue.DialogMeasurement("a", None, ()), ue.DialogMeasurement("b", None, (paid,))],
        split,
        pricing,
        usd_rub_rate=None,
    )
    assert unweighted.llm_dialog_share == Decimal("0.5") and unweighted.active_variable_cost_rub == Decimal("0.05")


def test_customer_contour_is_a_separate_local_scenario(tmp_path):
    split = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    customer = {
        "daily_cost": 900,
        "currency": "RUB",
        "source_url": "https://example.invalid/t36",
        "date": "2026-11-27",
        "description": "GPU-сервер заказчика",
    }
    pricing = ue.load_pricing_config(
        _write_synthetic_pricing(tmp_path / "pricing.yaml", overrides={"customer_infrastructure": customer})
    )
    dialogs = [
        ue.DialogMeasurement(
            "a", CATEGORY, (ue.LLMCallRecord("support_tools", "synth-fast-rub", 1000, 0),), False, True
        ),
        ue.DialogMeasurement("b", CATEGORY, (), True, False),
    ]
    res = ue.compute_unit_economics(dialogs, split, pricing, usd_rub_rate=None)
    contour = res.customer_contour
    assert contour.period_total_cost_rub == 900 * 180 and contour.full_cost_per_request_rub == Decimal("324")
    assert contour.load_daily_total_cost_rub == 900 and contour.load_full_cost_per_request_rub == Decimal("1.8")
    # Operator 1200 ₽/h * 5 min = 100 ₽ per request; half of the requests still reach the manager.
    assert contour.net_savings_period_rub == 500 * 100 - (900 * 180 + 500 * 100 // 2)
    assert contour.load_net_savings_daily_rub == 500 * 100 - (900 + 500 * 100 // 2)
    # The stand scenario keeps the Selectel rate and the measured token cost.
    assert res.period_infra_cost_rub == 150 * 180 and res.period_total_cost_rub == Decimal("27025")
    md = ue.render_markdown_report(res, pricing)
    assert "## Контур заказчика (локальный запуск после T36)" in md and "| Сервер заказчика, ₽/сут. | 900.00 |" in md
    assert "| Контур заказчика (T36) (GPU-сервер заказчика) | 900 / сут. | RUB |" in md
    assert (
        "Контур заказчика (T36, локальный запуск, токены 0 ₽): 900.00 ₽/сут" in ue.build_comment_lines(res, pricing)[2]
    )

    plain = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "plain.yaml"))
    res = ue.compute_unit_economics(dialogs, split, plain, usd_rub_rate=None)
    assert res.customer_contour is None
    assert "_Не задан." in ue.render_markdown_report(res, plain)
    del customer["date"]
    broken = _write_synthetic_pricing(tmp_path / "broken.yaml", overrides={"customer_infrastructure": customer})
    with pytest.raises(ue.ConfigError, match="customer_infrastructure.date"):
        ue.load_pricing_config(broken)
