"""Тесты воспроизводимого расчёта стоимости запроса (T19)."""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "calc_unit_economics.py"
PRICING_EXAMPLE_PATH = REPO_ROOT / "config" / "pricing.yaml.example"


def _load_module():
    spec = importlib.util.spec_from_file_location("calc_unit_economics", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ue = _load_module()

NINE_CATEGORIES = (
    "Просто цена конкретного товара",
    "Прочее (покупка)",
    "Счет / КП как документ (с реквизитами)",
    "Конфигурация / спецификация",
    "Доступность вендора (можно ли купить)",
    "Физлицо / самозанятый / срок",
    "Скидки / льготы / спецусловия",
    "Госзакупки / процедуры",
    "Follow-up / не дозвонился",
)


def _write_synthetic_split_summary(path: Path, counts: dict[str, int] | None = None) -> Path:
    if counts is None:
        counts = {
            NINE_CATEGORIES[0]: 200,
            NINE_CATEGORIES[1]: 100,
            NINE_CATEGORIES[2]: 50,
            NINE_CATEGORIES[3]: 50,
            NINE_CATEGORIES[4]: 40,
            NINE_CATEGORIES[5]: 25,
            NINE_CATEGORIES[6]: 15,
            NINE_CATEGORIES[7]: 12,
            NINE_CATEGORIES[8]: 8,
        }
    total = sum(counts.values())
    payload = {
        "schema_version": "1.2",
        "total_records": total,
        "categories": {cat: {"total": cnt, "open_dev": cnt, "private_holdout": 0} for cat, cnt in counts.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_synthetic_pricing(path: Path, overrides: dict | None = None) -> Path:
    data: dict = {
        "models": {
            "synth-smart": {
                "input_per_1m": 2.0,
                "output_per_1m": 8.0,
                "currency": "USD",
                "source_url": "https://example.invalid/pricing/smart",
                "date": "2026-11-26",
            },
            "synth-fast-rub": {
                "input_per_1m": 100.0,
                "output_per_1m": 400.0,
                "currency": "RUB",
                "source_url": "https://example.invalid/pricing/fast",
                "date": "2026-11-26",
            },
        },
        "infrastructure": {
            "daily_cost": 150.0,
            "currency": "RUB",
            "source_url": "https://example.invalid/pricing/vps",
            "date": "2026-11-26",
            "description": "Synthetic VPS",
        },
        "operator": {
            "hourly_rate_rub": 1200.0,
            "minutes_per_request": 5.0,
            "source_url": "https://example.invalid/hr/operator",
            "date": "2026-11-26",
        },
    }
    if overrides:
        data.update(overrides)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _create_synthetic_template(
    path: Path,
    *,
    cbr_rate: float = 100.0,
    header_row: int = 5,
    data_row_count: int = 5,
    grouped_headers: bool = False,
    merged_comment: bool = False,
) -> tuple[Path, dict[str, str]]:
    """Создаёт синтетический .xlsx-шаблон с курсом ЦБ в I3, формулами и комментарием."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Расчёт стоимости"

    ws["A1"] = "Расчёт стоимости одного запроса ИИ-агента"
    ws["H3"] = "Курс ЦБ (USD/RUB)"
    ws["I3"] = cbr_rate

    if grouped_headers:
        top = header_row - 1
        ws.cell(row=top, column=4, value="Средние токены")
        ws.merge_cells(start_row=top, start_column=4, end_row=top, end_column=5)
        ws.cell(row=top, column=6, value="Цена за 1 млн")
        ws.merge_cells(start_row=top, start_column=6, end_row=top, end_column=7)
        headers = [
            "Шаг агента",
            "Модель",
            "Вызовов на запрос",
            "Вход",
            "Выход",
            "Вход",
            "Выход",
            "Валюта",
            "Стоимость, руб.",
        ]
    else:
        headers = [
            "Шаг агента",
            "Модель",
            "Вызовов на запрос",
            "Средние токены вход",
            "Средние токены выход",
            "Цена за 1 млн вход",
            "Цена за 1 млн выход",
            "Валюта",
            "Стоимость, руб.",
        ]

    for col_idx, text in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col_idx, value=text)

    formulas: dict[str, str] = {}
    first_data = header_row + 1
    last_data = first_data + data_row_count - 1
    for r in range(first_data, last_data + 1):
        formula = f'=IF(C{r}="","",C{r}*((D{r}/1000000)*F{r}+(E{r}/1000000)*G{r})*IF(H{r}="USD",$I$3,1))'
        ws.cell(row=r, column=9, value=formula)
        formulas[f"I{r}"] = formula

    total_row = last_data + 1
    ws.cell(row=total_row, column=1, value="Итого за 1 запрос, руб.")
    total_formula = f"=SUM(I{first_data}:I{last_data})"
    ws.cell(row=total_row, column=9, value=total_formula)
    formulas[f"I{total_row}"] = total_formula

    comment_row = total_row + 2
    ws.cell(row=comment_row, column=1, value="Комментарий")
    if merged_comment:
        ws.merge_cells(start_row=comment_row, start_column=2, end_row=comment_row + 2, end_column=9)
    else:
        # Оставляем 3 пустые строки под меткой комментария.
        ws.cell(row=comment_row + 1, column=1, value=None)
        ws.cell(row=comment_row + 2, column=1, value=None)
        ws.cell(row=comment_row + 3, column=1, value=None)

    wb.save(path)
    wb.close()
    return path, formulas


# --- 1. Формулы расчёта и агрегация шагов ---------------------------------------


def test_cost_formula_and_step_aggregation(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))

    # 2 диалога в одной категории:
    # - Dialog 1: agent_loop (synth-smart, 1000 in, 500 out)
    #   cost = (1000/1e6 * 2 + 500/1e6 * 8) * 100 = (0.002 + 0.004) * 100 = 0.60 RUB
    # - Dialog 2: agent_loop (synth-smart, 2000 in, 1000 out) + escalation_summary (synth-fast-rub, 500 in, 250 out)
    #   agent_loop cost = (0.004 + 0.008) * 100 = 1.20 RUB
    #   escalation cost = (500/1e6 * 100 + 250/1e6 * 400) * 1 = 0.05 + 0.10 = 0.15 RUB
    #   total dialog 2 = 1.35 RUB
    # Average per request = (0.60 + 1.35) / 2 = 0.975 RUB
    # Step agent_loop: calls=1.0, avg_in=1500, avg_out=750 -> cost = 0.90 RUB
    # Step escalation_summary: calls=0.5, avg_in=500, avg_out=250 -> cost = 0.075 RUB
    cat = NINE_CATEGORIES[0]
    dialogs = [
        ue.DialogMeasurement(
            dialog_id="d1",
            category=cat,
            calls=(ue.LLMCallRecord("agent_loop", "synth-smart", 1000, 500),),
        ),
        ue.DialogMeasurement(
            dialog_id="d2",
            category=cat,
            calls=(
                ue.LLMCallRecord("agent_loop", "synth-smart", 2000, 1000),
                ue.LLMCallRecord("escalation_summary", "synth-fast-rub", 500, 250),
            ),
        ),
    ]

    res = ue.compute_unit_economics(
        dialogs,
        summary,
        pricing,
        usd_rub_rate=Decimal("100"),
    )

    assert res.has_measurements is True
    assert res.measured_dialogs == 2
    assert res.period_days == 180
    assert res.requests_per_period == 500
    assert res.weighted_variable_cost_rub == Decimal("0.975")
    assert res.unweighted_variable_cost_rub == Decimal("0.975")

    by_step = {s.step: s for s in res.steps}
    assert by_step["agent_loop"].calls_per_request == Decimal("1")
    assert by_step["agent_loop"].avg_tokens_in == Decimal("1500")
    assert by_step["agent_loop"].avg_tokens_out == Decimal("750")
    assert by_step["agent_loop"].cost_per_request_rub == Decimal("0.9")

    assert by_step["escalation_summary"].calls_per_request == Decimal("0.5")
    assert by_step["escalation_summary"].avg_tokens_in == Decimal("500")
    assert by_step["escalation_summary"].avg_tokens_out == Decimal("250")
    assert by_step["escalation_summary"].cost_per_request_rub == Decimal("0.075")

    # Тождество: сумма стоимостей шагов == переменная стоимость 1 обращения.
    assert sum((s.cost_per_request_rub for s in res.steps), Decimal("0")) == res.active_variable_cost_rub

    # Базовый период (полгода = 180 дней, ровно 500 обращений за весь период):
    # Переменные: 500 * 0.975 = 487.50 RUB; инфраструктура: 150 * 180 = 27000 RUB;
    # Итого за полгода: 27487.50 RUB -> 54.975 RUB/обращение.
    assert res.period_variable_cost_rub == Decimal("487.5")
    assert res.daily_infra_cost_rub == Decimal("150")
    assert res.period_infra_cost_rub == Decimal("27000")
    assert res.period_total_cost_rub == Decimal("27487.5")
    assert res.full_cost_per_request_rub == Decimal("54.975")

    # Ручной маршрут (оператор) на тех же 500 обращениях за полгода:
    # 1200 RUB/ч * 5 мин / 60 = 100 RUB/обр; 500 * 100 = 50000 RUB за период.
    assert res.operator_cost_per_request_rub == Decimal("100")
    assert res.operator_period_cost_rub == Decimal("50000")
    assert res.savings_per_request_rub == Decimal("45.025")
    assert res.savings_period_rub == Decimal("22512.5")
    assert res.savings_pct == Decimal("45.025")

    # Отдельный нагрузочный сценарий (500 обращений/сутки со слайда 2):
    load = res.load_scenario
    assert load.requests_per_day == 500
    assert load.period_days == 180
    assert load.requests_per_period == 90000
    assert load.daily_variable_cost_rub == Decimal("487.5")
    assert load.daily_infra_cost_rub == Decimal("150")
    assert load.daily_total_cost_rub == Decimal("637.5")
    assert load.period_variable_cost_rub == Decimal("87750")
    assert load.period_infra_cost_rub == Decimal("27000")
    assert load.period_total_cost_rub == Decimal("114750")
    assert load.full_cost_per_request_rub == Decimal("1.275")
    assert load.operator_daily_cost_rub == Decimal("50000")
    assert load.operator_period_cost_rub == Decimal("9000000")
    assert load.savings_per_request_rub == Decimal("98.725")
    assert load.savings_daily_rub == Decimal("49362.5")
    assert load.savings_period_rub == Decimal("8885250")
    assert load.savings_pct == Decimal("98.725")


def test_infra_is_independent_of_request_volume_and_load_scenario_is_separate(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))
    dialogs = [
        ue.DialogMeasurement(
            dialog_id="d1",
            category=NINE_CATEGORIES[0],
            calls=(ue.LLMCallRecord("agent_loop", "synth-smart", 1000, 500),),  # 0.60 RUB
        )
    ]

    # При неизменных 180 днях инфраструктура (150 * 180 = 27000) не зависит от объёма обращений.
    base_500 = ue.compute_unit_economics(
        dialogs, summary, pricing, usd_rub_rate=Decimal("100"), requests_per_period=500, period_days=180
    )
    small_100 = ue.compute_unit_economics(
        dialogs, summary, pricing, usd_rub_rate=Decimal("100"), requests_per_period=100, period_days=180
    )
    assert base_500.period_infra_cost_rub == Decimal("27000")
    assert small_100.period_infra_cost_rub == Decimal("27000")
    assert base_500.period_variable_cost_rub == Decimal("300")
    assert small_100.period_variable_cost_rub == Decimal("60")
    assert base_500.operator_period_cost_rub == Decimal("50000")
    assert small_100.operator_period_cost_rub == Decimal("10000")

    # При изменении длительности периода (90 дней) инфраструктура = ставка * дни,
    # а переменные и оператор на тех же 500 обращениях не меняются.
    quarter = ue.compute_unit_economics(
        dialogs, summary, pricing, usd_rub_rate=Decimal("100"), requests_per_period=500, period_days=90
    )
    assert quarter.period_infra_cost_rub == Decimal("13500")
    assert quarter.period_variable_cost_rub == Decimal("300")
    assert quarter.operator_period_cost_rub == Decimal("50000")
    assert quarter.period_total_cost_rub == Decimal("13800")

    # В Markdown-отчёте базовый расчёт и нагрузочный сценарий 500/сутки разделены и явно подписаны.
    md = ue.render_markdown_report(base_500, pricing)
    assert "500 обращений за 180 дн." in md
    assert "27300.00" in md  # 300 + 27000
    assert "50000.00" in md  # оператор на 500 обращениях
    assert "Нагрузочный сценарий (отдельный расчёт: 500 обращений/сутки, не базовый прогноз)" in md
    assert "не является базовым прогнозом" in md
    assert "81000.00" in md  # 90000 * 0.60 + 27000 = 54000 + 27000 = 81000.00


# --- 2. Взвешивание по долям категорий + eval/telemetry join --------------------


def test_archive_category_weighting_and_telemetry_join(tmp_path: Path):
    # Cat 0: 80% (800 записей), Cat 1: 20% (200 записей)
    counts = {NINE_CATEGORIES[0]: 800, NINE_CATEGORIES[1]: 200}
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json", counts=counts))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))

    # В eval-отчёте: 1 сценарий в Cat 0 (дешёвый: 1000 in, 0 out -> 0.20 RUB)
    # и 3 сценария в Cat 1 (дорогие: по 5000 in, 0 out -> 1.00 RUB каждый).
    # Невзвешенное среднее: (0.20 + 3 * 1.00) / 4 = 0.80 RUB
    # Взвешенное по архиву: 0.8 * 0.20 + 0.2 * 1.00 = 0.16 + 0.20 = 0.36 RUB!
    eval_report = tmp_path / "eval.json"
    eval_report.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "c0", "category": NINE_CATEGORIES[0], "conversation_id": "conv_0"},
                    {"id": "c1", "category": NINE_CATEGORIES[1], "conversation_id": "conv_1"},
                    {"id": "c2", "category": NINE_CATEGORIES[1], "conversation_id": "conv_2"},
                    {"id": "c3", "category": NINE_CATEGORIES[1], "conversation_id": "conv_3"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text(
        json.dumps(
            {
                "today_spend_rub": 3.2,
                "daily_budget_rub": 500.0,
                "budget_left_rub": 496.8,
                "usd_rub_rate": 100.0,
                "steps": [],
                "recent_calls": [
                    {
                        "id": 1,
                        "session_id": "conv_0",
                        "step": "agent_loop",
                        "model": "synth-smart",
                        "tokens_in": 1000,
                        "tokens_out": 0,
                    },
                    {
                        "id": 2,
                        "session_id": "conv_1",
                        "step": "agent_loop",
                        "model": "synth-smart",
                        "tokens_in": 5000,
                        "tokens_out": 0,
                    },
                    {
                        "id": 3,
                        "session_id": "conv_2",
                        "step": "agent_loop",
                        "model": "synth-smart",
                        "tokens_in": 5000,
                        "tokens_out": 0,
                    },
                    {
                        "id": 4,
                        "session_id": "conv_3",
                        "step": "agent_loop",
                        "model": "synth-smart",
                        "tokens_in": 5000,
                        "tokens_out": 0,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dialogs = ue.load_measurements(
        eval_report_paths=[eval_report],
        telemetry_paths=[telemetry],
        split_summary=summary,
    )
    res = ue.compute_unit_economics(dialogs, summary, pricing, usd_rub_rate=Decimal("100"))

    assert res.weighting_mode == "archive_weighted"
    assert res.unweighted_variable_cost_rub == Decimal("0.8")
    assert res.weighted_variable_cost_rub == Decimal("0.36")
    assert res.covered_archive_share == Decimal("1")
    assert res.missing_categories == ()

    # Проверяем, что взвешенные средние токены по шагу дают ту же взвешенную стоимость.
    assert len(res.steps) == 1
    assert res.steps[0].calls_per_request == Decimal("1")
    assert res.steps[0].avg_tokens_in == Decimal("1800")
    assert res.steps[0].cost_per_request_rub == Decimal("0.36")


# --- 3. Частичные и пустые замеры -----------------------------------------------


def test_partial_measurements_mark_missing_categories_explicitly(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))

    # Замеряем только 2 из 9 категорий (200/500 = 0.4 и 100/500 = 0.2 -> покрытие 60%).
    dialogs = [
        ue.DialogMeasurement(
            dialog_id="d1",
            category=NINE_CATEGORIES[0],
            calls=(ue.LLMCallRecord("agent_loop", "synth-smart", 1000, 0),),  # 0.20 RUB
        ),
        ue.DialogMeasurement(
            dialog_id="d2",
            category=NINE_CATEGORIES[1],
            calls=(ue.LLMCallRecord("agent_loop", "synth-smart", 4000, 0),),  # 0.80 RUB
        ),
    ]
    res = ue.compute_unit_economics(dialogs, summary, pricing, usd_rub_rate=Decimal("100"))

    assert res.covered_archive_share == Decimal("0.6")
    assert res.uncovered_archive_share == Decimal("0.4")
    assert len(res.missing_categories) == 7

    # Относительные веса: 0.4/0.6 = 2/3, 0.2/0.6 = 1/3 -> (2/3)*0.20 + (1/3)*0.80 = 0.40 RUB.
    assert ue._q(res.weighted_variable_cost_rub, "0.0001") == Decimal("0.4000")

    cat_map = {c.name: c for c in res.categories}
    for missing_cat in NINE_CATEGORIES[2:]:
        assert cat_map[missing_cat].status == "no_data"
        assert cat_map[missing_cat].measured_dialogs == 0
        assert cat_map[missing_cat].avg_variable_cost_rub is None

    md = ue.render_markdown_report(res, pricing)
    assert "**нет замеров**" in md
    assert "60.00%" in md


def test_empty_measurements(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))

    eval_report = tmp_path / "empty_eval.json"
    eval_report.write_text(json.dumps({"cases": []}), encoding="utf-8")

    dialogs = ue.load_measurements(eval_report_paths=[eval_report], telemetry_paths=[], split_summary=summary)
    res = ue.compute_unit_economics(dialogs, summary, pricing, usd_rub_rate=Decimal("100"))

    assert res.has_measurements is False
    assert res.measured_dialogs == 0
    assert res.steps == ()
    assert res.weighted_variable_cost_rub is None
    assert res.period_variable_cost_rub is None
    assert res.period_total_cost_rub is None
    assert res.daily_infra_cost_rub == Decimal("150")
    assert res.period_infra_cost_rub == Decimal("27000")
    assert res.operator_cost_per_request_rub == Decimal("100")
    assert res.operator_period_cost_rub == Decimal("50000")
    assert res.load_scenario.period_total_cost_rub is None
    assert res.load_scenario.period_infra_cost_rub == Decimal("27000")
    assert len(res.missing_categories) == 9
    assert all(c.status == "no_data" and c.avg_variable_cost_rub is None for c in res.categories)

    md = ue.render_markdown_report(res, pricing)
    assert "Нет замеров LLM-вызовов" in md


# --- 4. Падение без конфига и при неполных/некорректных данных ------------------


def test_missing_pricing_config_fails(tmp_path: Path):
    with pytest.raises(ue.ConfigError, match="не найден"):
        ue.load_pricing_config(tmp_path / "does_not_exist.yaml")


def test_pricing_example_has_no_defaults_and_fails_validation():
    assert PRICING_EXAMPLE_PATH.is_file()
    with pytest.raises(ue.ConfigError):
        ue.load_pricing_config(PRICING_EXAMPLE_PATH)


@pytest.mark.parametrize(
    "mutator,expected_msg",
    [
        (lambda d: d.pop("operator"), "operator"),
        (lambda d: d["models"]["synth-smart"].pop("source_url"), "source_url"),
        (lambda d: d["models"]["synth-smart"].pop("date"), "date"),
        (lambda d: d["models"]["synth-smart"].__setitem__("input_per_1m", None), "input_per_1m"),
        (lambda d: d["infrastructure"].__setitem__("daily_cost", ""), "daily_cost"),
        (lambda d: d["operator"].__setitem__("minutes_per_request", 0), "minutes_per_request"),
    ],
)
def test_incomplete_pricing_config_fails_with_clear_message(tmp_path: Path, mutator, expected_msg: str):
    cfg_path = _write_synthetic_pricing(tmp_path / "pricing.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    mutator(raw)
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ue.ConfigError, match=expected_msg):
        ue.load_pricing_config(cfg_path)


def test_unknown_model_and_unknown_category_fail(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))

    with pytest.raises(ue.ConfigError, match="unknown-model"):
        ue.compute_unit_economics(
            [ue.DialogMeasurement("d1", NINE_CATEGORIES[0], (ue.LLMCallRecord("s", "unknown-model", 10, 10),))],
            summary,
            pricing,
            usd_rub_rate=Decimal("100"),
        )

    bad_eval = tmp_path / "bad_eval.json"
    bad_eval.write_text(
        json.dumps({"cases": [{"id": "c1", "category": "Несуществующая категория", "llm_calls": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ue.InputDataError, match="неизвестную категорию"):
        ue.load_measurements(eval_report_paths=[bad_eval], telemetry_paths=[], split_summary=summary)


# --- 5. Шаблон .xlsx: формулы и курс ЦБ целы после записи -----------------------


def test_xlsx_template_preserves_formulas_and_cbr_rate_end_to_end(tmp_path: Path):
    split_path = _write_synthetic_split_summary(tmp_path / "split.json")
    pricing_path = _write_synthetic_pricing(tmp_path / "pricing.yaml")
    template_path, original_formulas = _create_synthetic_template(
        tmp_path / "template.xlsx",
        cbr_rate=97.5,
        header_row=5,
        data_row_count=5,
    )
    out_xlsx = tmp_path / "filled.xlsx"
    out_md = tmp_path / "cost_report.md"

    eval_path = tmp_path / "eval_report.json"
    eval_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "case-1",
                        "category": NINE_CATEGORIES[0],
                        "turns": [
                            {
                                "steps": [
                                    {
                                        "tool": "llm:agent_loop",
                                        "args": {"model": "synth-smart"},
                                        "tokens_in": 1200,
                                        "tokens_out": 300,
                                    }
                                ]
                            }
                        ],
                    },
                    {
                        "id": "case-2",
                        "category": NINE_CATEGORIES[0],
                        "turns": [
                            {
                                "steps": [
                                    {
                                        "tool": "llm:agent_loop",
                                        "args": {"model": "synth-smart"},
                                        "tokens_in": 800,
                                        "tokens_out": 100,
                                    },
                                    {
                                        "tool": "llm:escalation_summary",
                                        "args": {"model": "synth-fast-rub"},
                                        "tokens_in": 400,
                                        "tokens_out": 200,
                                    },
                                ]
                            }
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc = ue.main(
        [
            "--eval-report",
            str(eval_path),
            "--split-summary",
            str(split_path),
            "--pricing",
            str(pricing_path),
            "--template",
            str(template_path),
            "--xlsx-out",
            str(out_xlsx),
            "--report-out",
            str(out_md),
        ]
    )
    assert rc == 0
    assert out_xlsx.is_file()
    assert out_md.is_file()

    # Открываем записанный .xlsx и проверяем, что курс ЦБ и ВСЕ формулы остались целы.
    wb = openpyxl.load_workbook(out_xlsx, data_only=False)
    try:
        ws = wb["Расчёт стоимости"]
        assert ws["I3"].value == 97.5

        for coord, expected_formula in original_formulas.items():
            actual = ws[coord].value
            assert isinstance(actual, str) and actual.startswith("="), f"Ячейка {coord} потеряла формулу: {actual!r}"
            assert actual == expected_formula

        # Проверяем заполненные строки шагов (строки 6 и 7).
        assert ws["A6"].value == "agent_loop"
        assert ws["B6"].value == "synth-smart"
        assert ws["C6"].value == 1.0
        assert ws["D6"].value == 1000.0
        assert ws["E6"].value == 200.0
        assert ws["F6"].value == 2.0
        assert ws["G6"].value == 8.0
        assert ws["H6"].value == "USD"

        assert ws["A7"].value == "escalation_summary"
        assert ws["B7"].value == "synth-fast-rub"
        assert ws["C7"].value == 0.5
        assert ws["D7"].value == 400.0
        assert ws["E7"].value == 200.0
        assert ws["H7"].value == "RUB"

        # Неиспользуемая строка 8 очищена по входным полям, формула в I8 цела.
        assert ws["A8"].value is None
        assert ws["I8"].value == original_formulas["I8"]

        # Комментарий (строки 14-16 под меткой «Комментарий» в A13) содержит <= 3 строк и ссылки.
        comment_lines = [ws.cell(row=r, column=1).value for r in (14, 15, 16)]
        assert all(isinstance(line, str) and line.strip() for line in comment_lines)
        assert "архив" in comment_lines[0].lower()
        assert "https://example.invalid/pricing/smart" in comment_lines[1]
        assert "500 обр./180 дн." in comment_lines[2]
        assert "нагрузочный сценарий (не база): 500 обр./сут." in comment_lines[2]
    finally:
        wb.close()

    md = out_md.read_text(encoding="utf-8")
    assert "500 обращений за 180 дн." in md
    assert "не базовый прогноз" in md


def test_xlsx_template_grouped_headers_and_shifted_rows(tmp_path: Path):
    summary = ue.load_split_summary(_write_synthetic_split_summary(tmp_path / "split.json"))
    pricing = ue.load_pricing_config(_write_synthetic_pricing(tmp_path / "pricing.yaml"))
    template_path, original_formulas = _create_synthetic_template(
        tmp_path / "grouped_template.xlsx",
        cbr_rate=95.0,
        header_row=8,
        data_row_count=3,
        grouped_headers=True,
        merged_comment=True,
    )
    out_xlsx = tmp_path / "grouped_out.xlsx"

    dialogs = [
        ue.DialogMeasurement(
            "d1",
            NINE_CATEGORIES[0],
            (ue.LLMCallRecord("agent_loop", "synth-smart", 500, 100),),
        )
    ]
    res = ue.compute_unit_economics(dialogs, summary, pricing, usd_rub_rate=Decimal("95"))
    ue.fill_xlsx_template(template_path, out_xlsx, res, pricing)

    wb = openpyxl.load_workbook(out_xlsx, data_only=False)
    try:
        ws = wb.active
        assert ws["I3"].value == 95.0
        assert ws["A9"].value == "agent_loop"
        assert ws["D9"].value == 500.0
        for coord, expected_formula in original_formulas.items():
            assert ws[coord].value == expected_formula
        # В объединённом комментарии <= 3 строк.
        comment_val = ws["B14"].value
        assert isinstance(comment_val, str)
        assert len(comment_val.splitlines()) <= 3
    finally:
        wb.close()


def test_xlsx_template_layout_mismatch_fails_clearly(tmp_path: Path):
    # 1. Отсутствует столбец «Валюта» — скрипт обязан упасть с понятной ошибкой.
    bad_tpl = tmp_path / "bad_template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["H3"] = "Курс ЦБ"
    ws["I3"] = 95.0
    ws["A5"] = "Шаг агента"
    ws["B5"] = "Модель"
    ws["C5"] = "Вызовов на запрос"
    ws["D5"] = "Средние токены вход"
    ws["E5"] = "Средние токены выход"
    ws["F5"] = "Цена за 1 млн вход"
    ws["G5"] = "Цена за 1 млн выход"
    # Намеренно пропускаем «Валюта»
    ws["A10"] = "Комментарий"
    wb.save(bad_tpl)
    wb.close()

    with pytest.raises(ue.TemplateLayoutError, match="валют"):
        ue.read_template_cbr_rate(bad_tpl)

    # 2. Отсутствует курс ЦБ.
    no_rate_tpl = tmp_path / "no_rate.xlsx"
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2["A1"] = "Пустой лист"
    wb2.save(no_rate_tpl)
    wb2.close()

    with pytest.raises(ue.TemplateLayoutError, match="курса ЦБ"):
        ue.read_template_cbr_rate(no_rate_tpl)
