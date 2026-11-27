#!/usr/bin/env python3
"""Воспроизводимый расчёт unit-экономики и стоимости запроса агента поддержки (T19).

Входные данные:
  - JSON-отчёт eval/run_eval.py (--eval-report) и/или выгрузка /api/v1/manager/telemetry (--telemetry);
  - eval/split_summary.json (--split-summary) с долями 9 категорий из архива чатов;
  - config/pricing.yaml (--pricing) — прайсы моделей, инфраструктура и оператор (без значений по умолчанию);
  - шаблон организаторов .xlsx (--template + --xlsx-out) — курс ЦБ и формулы не изменяются.

Выходные данные:
  - заполненный .xlsx по шаблону организаторов (с сохранением всех формул и курса ЦБ);
  - отчёт Markdown (по умолчанию docs/cost_report.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl
import yaml
from openpyxl.worksheet.worksheet import Worksheet

ONE_MILLION = Decimal("1000000")
SIXTY = Decimal("60")
HUNDRED = Decimal("100")
ZERO = Decimal("0")

SUPPORTED_CURRENCIES = frozenset({"RUB", "USD"})
# T05 /api/v1/manager/telemetry возвращает не более 20 последних вызовов даже с all_time=true.
TELEMETRY_RECENT_CALLS_LIMIT = 20

DEFAULT_PERIOD_DAYS = 180
DEFAULT_REQUESTS_PER_PERIOD = 500
DEFAULT_LOAD_REQUESTS_PER_DAY = 500
DEFAULT_REPORT_OUT = Path("docs/cost_report.md")
DEFAULT_PRICING_PATH = Path("config/pricing.yaml")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://\S+$")
SUM_RANGE_RE = re.compile(r"(?:SUM|СУММ)\(\s*\$?[A-Z]+\$?(\d+)\s*:\s*\$?[A-Z]+\$?(\d+)\s*\)", re.IGNORECASE)
CELL_REF_RE = re.compile(r"\$([A-Z]{1,3})\$(\d+)")

# --- Исключения -----------------------------------------------------------------


class UnitEconomicsError(Exception):
    """Базовая ошибка расчёта unit-экономики."""


class ConfigError(UnitEconomicsError):
    """Отсутствует файл конфигурации цен или обязательное поле в нём."""


class InputDataError(UnitEconomicsError):
    """Некорректные входные данные eval / telemetry / split_summary."""


class TemplateLayoutError(UnitEconomicsError):
    """Структура .xlsx-шаблона не совпала с ожидаемой."""


# --- Структуры данных -----------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_per_1m: Decimal
    output_per_1m: Decimal
    currency: str
    source_url: str
    date: str


@dataclass(frozen=True)
class InfrastructurePrice:
    daily_cost: Decimal
    currency: str
    source_url: str
    date: str
    description: str = ""


@dataclass(frozen=True)
class OperatorPrice:
    hourly_rate_rub: Decimal
    minutes_per_request: Decimal
    source_url: str
    date: str


@dataclass(frozen=True)
class PricingConfig:
    models: dict[str, ModelPrice]
    infrastructure: InfrastructurePrice
    operator: OperatorPrice


@dataclass(frozen=True)
class CategoryShare:
    name: str
    archive_count: int
    share: Decimal


@dataclass(frozen=True)
class SplitSummary:
    total_records: int
    categories: dict[str, CategoryShare]


@dataclass(frozen=True)
class LLMCallRecord:
    step: str
    model: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class DialogMeasurement:
    dialog_id: str
    category: str | None
    calls: tuple[LLMCallRecord, ...] | None  # None means missing telemetry; () means a confirmed zero.
    escalated: bool | None = False
    closed_by_agent: bool | None = None

    @property
    def confirmed_escalation(self) -> bool | None:
        if self.escalated is None and self.closed_by_agent is True:
            return False
        return self.escalated

    @property
    def needs_operator(self) -> bool | None:
        if self.escalated:
            return True
        return None if self.closed_by_agent is None else not self.closed_by_agent


@dataclass(frozen=True)
class StepAggregate:
    step: str
    model: str
    calls_per_request: Decimal
    avg_tokens_in: Decimal
    avg_tokens_out: Decimal
    price_in_per_1m: Decimal
    price_out_per_1m: Decimal
    currency: str
    cost_per_request_rub: Decimal
    raw_call_count: int


@dataclass(frozen=True)
class CategoryBreakdown:
    name: str
    archive_count: int
    archive_share: Decimal
    measured_dialogs: int
    status: str  # "measured" | "no_data"
    avg_variable_cost_rub: Decimal | None


@dataclass(frozen=True)
class LoadScenarioEconomics:
    """Отдельный нагрузочный сценарий (например, 500 обращений/сутки со слайда 2; не базовый прогноз)."""

    requests_per_day: int
    period_days: int
    requests_per_period: int
    daily_tokens_in: Decimal | None
    daily_tokens_out: Decimal | None
    daily_variable_cost_rub: Decimal | None
    daily_infra_cost_rub: Decimal
    daily_total_cost_rub: Decimal | None
    period_variable_cost_rub: Decimal | None
    period_infra_cost_rub: Decimal
    period_total_cost_rub: Decimal | None
    full_cost_per_request_rub: Decimal | None
    operator_daily_cost_rub: Decimal
    operator_period_cost_rub: Decimal
    daily_residual_operator_cost_rub: Decimal | None
    period_residual_operator_cost_rub: Decimal | None
    savings_per_request_rub: Decimal | None
    savings_daily_rub: Decimal | None
    savings_period_rub: Decimal | None
    savings_pct: Decimal | None
    net_savings_daily_rub: Decimal | None
    net_savings_period_rub: Decimal | None
    net_savings_pct: Decimal | None


@dataclass(frozen=True)
class UnitEconomicsResult:
    has_measurements: bool
    weighting_mode: str  # "archive_weighted" | "unweighted" | "none"
    measured_dialogs: int
    total_dialogs: int
    known_outcomes: int
    escalated_dialogs: int
    escalation_rate: Decimal | None
    covered_archive_share: Decimal
    uncovered_archive_share: Decimal
    missing_categories: tuple[str, ...]
    usd_rub_rate: Decimal | None
    period_days: int
    requests_per_period: int
    steps: tuple[StepAggregate, ...]
    categories: tuple[CategoryBreakdown, ...]
    avg_tokens_in_per_request: Decimal | None
    avg_tokens_out_per_request: Decimal | None
    daily_tokens_in: Decimal | None
    daily_tokens_out: Decimal | None
    weighted_variable_cost_rub: Decimal | None
    unweighted_variable_cost_rub: Decimal | None
    active_variable_cost_rub: Decimal | None
    daily_infra_cost_rub: Decimal
    period_variable_cost_rub: Decimal | None
    period_infra_cost_rub: Decimal
    period_total_cost_rub: Decimal | None
    full_cost_per_request_rub: Decimal | None
    operator_cost_per_request_rub: Decimal
    operator_period_cost_rub: Decimal
    period_residual_operator_cost_rub: Decimal | None
    period_pilot_total_cost_rub: Decimal | None
    savings_per_request_rub: Decimal | None
    savings_period_rub: Decimal | None
    savings_pct: Decimal | None
    net_savings_period_rub: Decimal | None
    net_savings_pct: Decimal | None
    load_scenario: LoadScenarioEconomics
    telemetry_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateLayout:
    sheet_name: str
    cbr_rate_cell: tuple[int, int]  # (row, col)
    cbr_rate_value: Decimal | None
    header_row: int
    columns: dict[str, int]  # field_key -> 1-based column index
    data_rows: tuple[int, ...]
    comment_cells: tuple[tuple[int, int], ...]  # up to 3 (row, col) cells, or 1 multiline cell
    formula_cells: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    currency_symbols: dict[str, str] = field(default_factory=lambda: {"USD": "$", "RUB": "₽"})


# --- Вспомогательные функции для Decimal ----------------------------------------


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _fmt_dec(value: Decimal | None, places: str = "0.01", fallback: str = "н/д") -> str:
    if value is None:
        return fallback
    return str(_q(value, places))


def _parse_decimal(raw: Any, field_name: str, *, allow_zero: bool = True) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise ConfigError(f"Поле '{field_name}' обязательно и должно быть числом (получено: {raw!r}).")
    if isinstance(raw, str) and not raw.strip():
        raise ConfigError(f"Поле '{field_name}' не заполнено (пустая строка).")
    try:
        val = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"Поле '{field_name}' должно быть числом, получено: {raw!r}.") from exc
    if not val.is_finite():
        raise ConfigError(f"Поле '{field_name}' должно быть конечным числом, получено: {raw!r}.")
    if allow_zero and val < ZERO:
        raise ConfigError(f"Поле '{field_name}' должно быть >= 0, получено: {val}.")
    if not allow_zero and val <= ZERO:
        raise ConfigError(f"Поле '{field_name}' должно быть > 0, получено: {val}.")
    return val


def _parse_required_str(raw: Any, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"Поле '{field_name}' обязательно и не может быть пустым.")
    return raw.strip()


def _parse_currency(raw: Any, field_name: str) -> str:
    val = _parse_required_str(raw, field_name).upper()
    if val not in SUPPORTED_CURRENCIES:
        allowed = ", ".join(sorted(SUPPORTED_CURRENCIES))
        raise ConfigError(f"Поле '{field_name}': неподдерживаемая валюта {val!r} (допустимо: {allowed}).")
    return val


def _parse_date(raw: Any, field_name: str) -> str:
    if isinstance(raw, dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, dt.date):
        return raw.isoformat()
    val = _parse_required_str(raw, field_name)
    if not DATE_RE.match(val):
        raise ConfigError(f"Поле '{field_name}' должно быть датой в формате YYYY-MM-DD, получено: {val!r}.")
    try:
        dt.date.fromisoformat(val)
    except ValueError as exc:
        raise ConfigError(f"Поле '{field_name}' содержит некорректную дату: {val!r}.") from exc
    return val


def _parse_url(raw: Any, field_name: str, *, require_http: bool = True) -> str:
    val = _parse_required_str(raw, field_name)
    if require_http and not URL_RE.match(val):
        raise ConfigError(f"Поле '{field_name}' должно быть URL (http:// или https://), получено: {val!r}.")
    return val


# --- Загрузка config/pricing.yaml -----------------------------------------------


def load_pricing_config(path: Path) -> PricingConfig:
    """Строго загружает config/pricing.yaml. Никаких значений по умолчанию."""
    if not path.is_file():
        raise ConfigError(
            f"Файл конфигурации цен не найден: {path}. "
            "Скопируйте config/pricing.yaml.example в config/pricing.yaml и заполните все поля."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка разбора YAML в {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Файл {path} должен содержать YAML-словарь верхнего уровня.")

    for section in ("models", "infrastructure", "operator"):
        if section not in raw or not isinstance(raw[section], dict):
            raise ConfigError(f"В {path} отсутствует обязательная секция '{section}'.")

    raw_models = raw["models"]
    if not raw_models:
        raise ConfigError(f"В {path} секция 'models' пуста: укажите хотя бы одну модель.")

    models: dict[str, ModelPrice] = {}
    for model_name, cfg in raw_models.items():
        if not isinstance(model_name, str) or not model_name.strip():
            raise ConfigError(f"В {path}: имя модели в 'models' должно быть непустой строкой.")
        if not isinstance(cfg, dict):
            raise ConfigError(f"В {path}: описание модели '{model_name}' должно быть словарём.")
        prefix = f"models.{model_name}"
        models[model_name.strip()] = ModelPrice(
            model=model_name.strip(),
            input_per_1m=_parse_decimal(cfg.get("input_per_1m"), f"{prefix}.input_per_1m", allow_zero=True),
            output_per_1m=_parse_decimal(cfg.get("output_per_1m"), f"{prefix}.output_per_1m", allow_zero=True),
            currency=_parse_currency(cfg.get("currency"), f"{prefix}.currency"),
            source_url=_parse_url(cfg.get("source_url"), f"{prefix}.source_url", require_http=True),
            date=_parse_date(cfg.get("date"), f"{prefix}.date"),
        )

    raw_infra = raw["infrastructure"]
    infra = InfrastructurePrice(
        daily_cost=_parse_decimal(raw_infra.get("daily_cost"), "infrastructure.daily_cost", allow_zero=True),
        currency=_parse_currency(raw_infra.get("currency"), "infrastructure.currency"),
        source_url=_parse_url(raw_infra.get("source_url"), "infrastructure.source_url", require_http=True),
        date=_parse_date(raw_infra.get("date"), "infrastructure.date"),
        description=str(raw_infra.get("description") or "").strip(),
    )

    raw_op = raw["operator"]
    operator = OperatorPrice(
        hourly_rate_rub=_parse_decimal(raw_op.get("hourly_rate_rub"), "operator.hourly_rate_rub", allow_zero=False),
        minutes_per_request=_parse_decimal(
            raw_op.get("minutes_per_request"), "operator.minutes_per_request", allow_zero=False
        ),
        source_url=_parse_url(raw_op.get("source_url"), "operator.source_url", require_http=False),
        date=_parse_date(raw_op.get("date"), "operator.date"),
    )

    return PricingConfig(models=models, infrastructure=infra, operator=operator)


# --- Загрузка split_summary.json ------------------------------------------------


def load_split_summary(path: Path) -> SplitSummary:
    """Читает доли категорий обращений из split_summary.json."""
    if not path.is_file():
        raise InputDataError(f"Файл split_summary не найден: {path}.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputDataError(f"Некорректный JSON в {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("categories"), dict) or not data["categories"]:
        raise InputDataError(f"В {path} отсутствует непустой словарь 'categories'.")

    counts: dict[str, int] = {}
    for cat_name, info in data["categories"].items():
        if not isinstance(cat_name, str) or not cat_name.strip():
            raise InputDataError(f"В {path}: пустое имя категории.")
        if isinstance(info, dict) and "total" in info:
            cnt = info["total"]
        elif isinstance(info, int):
            cnt = info
        else:
            raise InputDataError(f"В {path}: для категории '{cat_name}' отсутствует целочисленное поле 'total'.")
        if isinstance(cnt, bool) or not isinstance(cnt, int) or cnt <= 0:
            raise InputDataError(
                f"В {path}: total для категории '{cat_name}' должно быть целым > 0 (получено {cnt!r})."
            )
        counts[cat_name] = cnt

    sum_counts = sum(counts.values())
    total_records = data.get("total_records", sum_counts)
    if isinstance(total_records, bool) or not isinstance(total_records, int) or total_records <= 0:
        raise InputDataError(f"В {path}: total_records должно быть целым > 0.")
    if total_records != sum_counts:
        raise InputDataError(
            f"В {path}: сумма total по категориям ({sum_counts}) не равна total_records ({total_records})."
        )

    categories = {
        name: CategoryShare(name=name, archive_count=cnt, share=Decimal(cnt) / Decimal(total_records))
        for name, cnt in counts.items()
    }
    return SplitSummary(total_records=total_records, categories=categories)


def load_category_map(path: Path | None) -> dict[str, str]:
    """Загружает необязательное отображение dialog_id/case_id -> категория."""
    if path is None:
        return {}
    if not path.is_file():
        raise InputDataError(f"Файл category-map не найден: {path}.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InputDataError(f"Файл category-map {path} должен быть словарём {{id: category}}.")
    return {str(k): str(v) for k, v in raw.items()}


# --- Загрузка замеров (eval report и/или telemetry) -----------------------------


def _parse_call_dict(item: dict[str, Any], ctx: str) -> LLMCallRecord:
    step = item.get("step")
    model = item.get("model")
    if not isinstance(step, str) or not step.strip():
        raise InputDataError(f"{ctx}: отсутствует непустое строковое поле 'step'.")
    if not isinstance(model, str) or not model.strip():
        raise InputDataError(f"{ctx}: отсутствует непустое строковое поле 'model'.")
    tokens_in = item.get("tokens_in")
    tokens_out = item.get("tokens_out")
    if isinstance(tokens_in, bool) or not isinstance(tokens_in, int) or tokens_in < 0:
        raise InputDataError(f"{ctx}: поле 'tokens_in' должно быть целым >= 0 (получено {tokens_in!r}).")
    if isinstance(tokens_out, bool) or not isinstance(tokens_out, int) or tokens_out < 0:
        raise InputDataError(f"{ctx}: поле 'tokens_out' должно быть целым >= 0 (получено {tokens_out!r}).")
    return LLMCallRecord(step=step.strip(), model=model.strip(), tokens_in=tokens_in, tokens_out=tokens_out)


def _extract_calls_from_turns(turns: list[Any], ctx: str) -> tuple[list[LLMCallRecord], bool]:
    """Извлекает LLM-вызовы из turns[].steps (формат T05 trace: tool='llm:<step>', args.model)."""
    found_trace = False
    calls: list[LLMCallRecord] = []
    for t_idx, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        steps = turn.get("steps")
        if not isinstance(steps, list):
            # A synchronous document reissue has no LLM work or trace entry.
            if turn.get("close") == "sync_document":
                continue
            return [], False
        found_trace = True
        for s_idx, st in enumerate(steps):
            if not isinstance(st, dict):
                continue
            tool = str(st.get("tool") or "")
            args = st.get("args") if isinstance(st.get("args"), dict) else {}
            tokens_in = int(st.get("tokens_in") or 0)
            tokens_out = int(st.get("tokens_out") or 0)
            if not tool.startswith("llm:") and tokens_in == 0 and tokens_out == 0:
                continue
            step_name = tool.removeprefix("llm:").strip() or "llm"
            model_name = args.get("model") or st.get("model")
            calls.append(
                _parse_call_dict(
                    {"step": step_name, "model": model_name, "tokens_in": tokens_in, "tokens_out": tokens_out},
                    f"{ctx}.turns[{t_idx}].steps[{s_idx}]",
                )
            )
    return calls, found_trace


def _load_telemetry_dumps(
    paths: list[Path],
    *,
    allow_incomplete_telemetry: bool = False,
) -> tuple[
    dict[str, list[LLMCallRecord]], dict[str, str | None], dict[str, bool], dict[str, bool], set[str], list[str]
]:
    """Читает один или несколько JSON-дампов /api/v1/manager/telemetry или per-dialog экспортов.

    Окно /manager/telemetry (recent_calls) ограничено 20 последними вызовами даже при all_time=true.
    При признаках обрезки окна (len >= 20 или сумма вызовов в steps > len(recent_calls)) по умолчанию
    поднимается InputDataError, чтобы не выдавать неполное окно за полную стоимость диалога.
    """
    calls_by_session: dict[str, list[LLMCallRecord]] = {}
    categories_by_session: dict[str, str | None] = {}
    escalated_by_session: dict[str, bool] = {}
    warnings: list[str] = []
    window_ids: dict[str, dict[Any, LLMCallRecord]] = defaultdict(dict)
    full_ids: dict[str, dict[Any, LLMCallRecord]] = defaultdict(dict)
    snapshots: dict[str, dict] = {}
    full_calls: dict[str, list[LLMCallRecord]] = {}
    outcomes: dict[str, bool] = {}

    for path in paths:
        if not path.is_file():
            raise InputDataError(f"Файл телеметрии не найден: {path}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InputDataError(f"Некорректный JSON в файле телеметрии {path}: {exc}") from exc

        if isinstance(payload, dict) and isinstance(payload.get("dialogs"), list):
            for idx, dlg in enumerate(payload["dialogs"]):
                if not isinstance(dlg, dict):
                    raise InputDataError(f"{path}: dialogs[{idx}] должен быть объектом.")
                sid = dlg.get("session_id") or dlg.get("conversation_id") or dlg.get("id")
                if not isinstance(sid, str) or not sid.strip():
                    raise InputDataError(f"{path}: dialogs[{idx}] без session_id/conversation_id/id.")
                sid = sid.strip()
                raw_calls = dlg.get("calls") if "calls" in dlg else dlg.get("llm_calls")
                if not isinstance(raw_calls, list):
                    raise InputDataError(f"{path}: dialogs[{idx}] ({sid}) без списка calls/llm_calls.")
                # Each dialogs[] entry describes the complete session, including zero or repeated calls.
                snapshot = {k: dlg.get(k) for k in ("category", "passed", "closed_by_agent", "escalated")}
                snapshot["calls"] = raw_calls
                if sid in snapshots:
                    if snapshots[sid] != snapshot:
                        raise InputDataError(f"{path}: несовместимые полные снимки диалога {sid}.")
                    continue
                snapshots[sid] = snapshot
                full_calls[sid] = []
                if dlg.get("category") is not None:
                    categories_by_session[sid] = str(dlg["category"])
                if isinstance(dlg.get("escalated"), bool):
                    escalated_by_session[sid] = dlg["escalated"]
                if isinstance(dlg.get("closed_by_agent"), bool):
                    outcomes[sid] = dlg["closed_by_agent"] and dlg.get("passed") is not False
                elif dlg.get("passed") is False:
                    outcomes[sid] = False
                for c_idx, call in enumerate(raw_calls):
                    if not isinstance(call, dict):
                        raise InputDataError(f"{path}: dialogs[{idx}].calls[{c_idx}] должен быть объектом.")
                    call_id = call.get("id")
                    record = _parse_call_dict(call, f"{path}:dialogs[{idx}].calls[{c_idx}]")
                    if call_id is not None:
                        if call_id in full_ids[sid]:
                            if full_ids[sid][call_id] != record:
                                raise InputDataError(f"Диалог {sid}: разные данные вызова {call_id}.")
                            continue
                        full_ids[sid][call_id] = record
                    full_calls[sid].append(record)
            continue

        is_recent_window = False
        if isinstance(payload, list):
            raw_list = payload
        elif isinstance(payload, dict) and isinstance(payload.get("recent_calls"), list):
            raw_list = payload["recent_calls"]
            is_recent_window = True
        elif isinstance(payload, dict) and isinstance(payload.get("calls"), list):
            raw_list = payload["calls"]
        elif isinstance(payload, dict) and isinstance(payload.get("llm_calls"), list):
            raw_list = payload["llm_calls"]
        else:
            raise InputDataError(
                f"Неизвестный формат телеметрии в {path}: ожидались поля 'recent_calls', 'calls', 'llm_calls', "
                "'dialogs' или JSON-массив вызовов."
            )

        if is_recent_window:
            reported_steps_calls = 0
            if isinstance(payload.get("steps"), list):
                reported_steps_calls = sum(int(st.get("calls") or 0) for st in payload["steps"] if isinstance(st, dict))
            truncated = len(raw_list) >= TELEMETRY_RECENT_CALLS_LIMIT or (reported_steps_calls > len(raw_list))
            if truncated:
                msg = (
                    f"Неполное окно /manager/telemetry в {path}: в recent_calls {len(raw_list)} записей "
                    f"(лимит окна {TELEMETRY_RECENT_CALLS_LIMIT}"
                    + (f", всего в steps: {reported_steps_calls}" if reported_steps_calls else "")
                    + "). Используйте полный per-dialog trace/экспорт T10 (llm_calls / dialogs[]) "
                    "или явно передайте --allow-incomplete-telemetry."
                )
                if not allow_incomplete_telemetry:
                    raise InputDataError(msg)
                warnings.append(msg)

        for idx, call in enumerate(raw_list):
            if not isinstance(call, dict):
                raise InputDataError(f"{path}: элемент телеметрии [{idx}] должен быть объектом.")
            call_id = call.get("id")
            sid = call.get("session_id") or call.get("conversation_id")
            if not isinstance(sid, str) or not sid.strip():
                raise InputDataError(
                    f"{path}: вызов [{idx}] не содержит session_id (нужен для группировки по обращениям)."
                )
            sid = sid.strip()
            record = _parse_call_dict(call, f"{path}:calls[{idx}]")
            if call_id is not None:
                if call_id in window_ids[sid]:
                    if window_ids[sid][call_id] != record:
                        raise InputDataError(f"Диалог {sid}: разные данные вызова {call_id}.")
                    continue
                window_ids[sid][call_id] = record
            calls_by_session.setdefault(sid, []).append(record)
            if call.get("category") is not None:
                categories_by_session[sid] = str(call["category"])

    for sid, complete in full_calls.items():
        window = calls_by_session.get(sid, [])
        # An identified call can match its own ID or an anonymous record of the same signature.
        # Multiplicity matters when two paid calls have identical token counts.
        known = full_ids[sid]
        anonymous = Counter(complete) - Counter(known.values())
        new_ids = Counter(call for key, call in window_ids[sid].items() if key not in known)
        if (
            Counter(window) - Counter(complete)
            or new_ids - anonymous
            or any(known[key] != window_ids[sid][key] for key in known.keys() & window_ids[sid].keys())
        ):
            raise InputDataError(f"Диалог {sid}: окно телеметрии противоречит полному снимку вызовов.")
        calls_by_session[sid] = complete
    return calls_by_session, categories_by_session, escalated_by_session, outcomes, set(full_calls), warnings


def load_measurements(
    *,
    eval_report_paths: list[Path],
    telemetry_paths: list[Path],
    split_summary: SplitSummary,
    category_map: dict[str, str] | None = None,
    allow_incomplete_telemetry: bool = False,
    warnings_out: list[str] | None = None,
) -> list[DialogMeasurement]:
    """Объединяет eval-отчёты и/или телеметрию в список замеров по диалогам (ровно 1 замер на диалог)."""
    if not eval_report_paths and not telemetry_paths:
        raise InputDataError("Нужно указать хотя бы один источник замеров: --eval-report и/или --telemetry.")

    cat_map = category_map or {}
    valid_categories = set(split_summary.categories.keys())
    telemetry_calls, telemetry_cats, telemetry_esc, telemetry_outcomes, full_sessions, tel_warnings = (
        _load_telemetry_dumps(telemetry_paths, allow_incomplete_telemetry=allow_incomplete_telemetry)
    )
    if warnings_out is not None:
        warnings_out.extend(tel_warnings)
    matched_sessions: set[str] = set()
    by_id: dict[str, DialogMeasurement] = {}

    for path in eval_report_paths:
        if not path.is_file():
            raise InputDataError(f"Файл eval-отчёта не найден: {path}.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InputDataError(f"Некорректный JSON в eval-отчёте {path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise InputDataError(f"В eval-отчёте {path} отсутствует список 'cases'.")

        for idx, case in enumerate(data["cases"]):
            if not isinstance(case, dict):
                raise InputDataError(f"{path}: cases[{idx}] должен быть объектом.")
            case_id = str(case.get("id") or case.get("case_id") or f"case_{idx}")
            conv_id = case.get("conversation_id") or case.get("session_id")
            conv_id_str = str(conv_id).strip() if conv_id else None

            category = (
                cat_map.get(case_id) or (cat_map.get(conv_id_str) if conv_id_str else None) or case.get("category")
            )
            if not isinstance(category, str) or not category.strip():
                raise InputDataError(f"{path}: сценарий '{case_id}' не привязан к категории из split_summary.")
            category = category.strip()
            if category not in valid_categories:
                raise InputDataError(
                    f"{path}: сценарий '{case_id}' имеет неизвестную категорию {category!r}. "
                    f"Допустимые категории из split_summary: {sorted(valid_categories)}."
                )

            calls: list[LLMCallRecord] | None = None
            if isinstance(case.get("llm_calls"), list):
                calls = [
                    _parse_call_dict(c, f"{path}:cases[{idx}].llm_calls[{c_i}]")
                    for c_i, c in enumerate(case["llm_calls"])
                ]
            elif case.get("cost_status") not in {"unavailable", "partial"} and isinstance(case.get("turns"), list):
                turn_calls, has_trace = _extract_calls_from_turns(case["turns"], f"{path}:cases[{idx}]")
                if has_trace:
                    calls = turn_calls

            dialog_id = conv_id_str or case_id
            telemetry_id = dialog_id if dialog_id in telemetry_calls else case_id
            other_calls = telemetry_calls.get(telemetry_id)
            if other_calls is not None:
                matched_sessions.add(telemetry_id)
                if calls is None:
                    calls = other_calls
                elif Counter(other_calls) - Counter(calls) or (
                    telemetry_id in full_sessions and Counter(other_calls) != Counter(calls)
                ):
                    raise InputDataError(f"Диалог {dialog_id}: eval и телеметрия противоречат друг другу по вызовам.")

            closed = case.get("closed_by_agent")
            if case.get("passed") is False:
                closed = False
            tel_closed = telemetry_outcomes.get(telemetry_id)
            if isinstance(closed, bool) and tel_closed is not None and tel_closed != closed:
                raise InputDataError(f"Диалог {dialog_id}: противоречивые исходы в eval и телеметрии.")
            escalated = case.get("escalated")
            tel_escalated = telemetry_esc.get(telemetry_id)
            if isinstance(escalated, bool) and tel_escalated is not None and tel_escalated != escalated:
                raise InputDataError(f"Диалог {dialog_id}: противоречивые признаки передачи в eval и телеметрии.")
            if not isinstance(escalated, bool):
                escalated = tel_escalated
            record = DialogMeasurement(
                dialog_id,
                category,
                tuple(calls) if calls is not None else None,
                escalated,
                closed if isinstance(closed, bool) else tel_closed,
            )
            previous = by_id.get(dialog_id)
            if previous is not None and previous != record:
                raise InputDataError(f"Диалог {dialog_id}: несовместимые снимки в eval-отчётах.")
            by_id[dialog_id] = record

    for sid, calls in telemetry_calls.items():
        if sid in matched_sessions:
            continue
        category = cat_map.get(sid) or telemetry_cats.get(sid)
        if category is not None:
            category = category.strip()
            if category not in valid_categories:
                raise InputDataError(f"Диалог телеметрии '{sid}' имеет неизвестную категорию {category!r}.")
        by_id[sid] = DialogMeasurement(sid, category, tuple(calls), telemetry_esc.get(sid), telemetry_outcomes.get(sid))

    dialogs = list(by_id.values())
    categorized = [d for d in dialogs if d.category is not None]
    uncategorized = [d for d in dialogs if d.category is None]
    if categorized and uncategorized:
        missing_ids = ", ".join(d.dialog_id for d in uncategorized[:5])
        raise InputDataError(
            f"Смешанные данные: часть диалогов привязана к категориям ({len(categorized)}), "
            f"а часть — нет ({len(uncategorized)}, например: {missing_ids}). "
            "Задайте категории для всех диалогов через --category-map или eval-отчёт."
        )

    return dialogs


# --- Расчёт unit-экономики (строго Decimal, ROUND_HALF_UP) ----------------------


def _currency_multiplier(currency: str, usd_rub_rate: Decimal | None, context: str) -> Decimal:
    if currency == "RUB":
        return Decimal("1")
    if currency == "USD":
        if usd_rub_rate is None or usd_rub_rate <= ZERO:
            raise ConfigError(
                f"Для пересчёта {context} из USD в RUB требуется положительный курс ЦБ "
                "(из шаблона .xlsx или аргумента --usd-rub-rate)."
            )
        return usd_rub_rate
    raise ConfigError(f"Неподдерживаемая валюта {currency!r} для {context}.")


def _call_cost_rub(call: LLMCallRecord, pricing: PricingConfig, usd_rub_rate: Decimal | None) -> Decimal:
    if call.model not in pricing.models:
        known = ", ".join(sorted(pricing.models))
        raise ConfigError(
            f"Модель {call.model!r} (шаг {call.step!r}) отсутствует в pricing.yaml. Известные модели: {known}."
        )
    mp = pricing.models[call.model]
    rate = _currency_multiplier(mp.currency, usd_rub_rate, f"модели {call.model!r}")
    cost_in = (Decimal(call.tokens_in) / ONE_MILLION) * mp.input_per_1m
    cost_out = (Decimal(call.tokens_out) / ONE_MILLION) * mp.output_per_1m
    return (cost_in + cost_out) * rate


def _pilot_costs(manual: Decimal, technical: Decimal | None, labor_share: Decimal | None):
    if labor_share is None:
        return None, None, None, None
    residual = manual * labor_share
    if technical is None:
        return residual, None, None, None
    total = technical + residual
    saving = manual - total
    return residual, total, saving, saving / manual * HUNDRED if manual > ZERO else ZERO


def _compute_load_scenario(
    *,
    active_variable_cost_rub: Decimal | None,
    avg_tokens_in_per_request: Decimal | None,
    avg_tokens_out_per_request: Decimal | None,
    labor_share: Decimal | None,
    complete_costs: bool = True,
    daily_infra_cost_rub: Decimal,
    operator_cost_per_request_rub: Decimal,
    requests_per_day: int,
    period_days: int,
) -> LoadScenarioEconomics:
    """Считает отдельный нагрузочный сценарий (например, 500 обращений/сутки со слайда 2)."""
    req_day_dec = Decimal(requests_per_day)
    period_days_dec = Decimal(period_days)
    requests_per_period = requests_per_day * period_days
    req_period_dec = Decimal(requests_per_period)

    period_infra_cost_rub = daily_infra_cost_rub * period_days_dec
    operator_daily_cost_rub = operator_cost_per_request_rub * req_day_dec
    operator_period_cost_rub = operator_cost_per_request_rub * req_period_dec

    if active_variable_cost_rub is None:
        return LoadScenarioEconomics(
            requests_per_day=requests_per_day,
            period_days=period_days,
            requests_per_period=requests_per_period,
            daily_tokens_in=None,
            daily_tokens_out=None,
            daily_variable_cost_rub=None,
            daily_infra_cost_rub=daily_infra_cost_rub,
            daily_total_cost_rub=None,
            period_variable_cost_rub=None,
            period_infra_cost_rub=period_infra_cost_rub,
            period_total_cost_rub=None,
            full_cost_per_request_rub=None,
            operator_daily_cost_rub=operator_daily_cost_rub,
            operator_period_cost_rub=operator_period_cost_rub,
            daily_residual_operator_cost_rub=operator_daily_cost_rub * labor_share if labor_share is not None else None,
            period_residual_operator_cost_rub=operator_period_cost_rub * labor_share
            if labor_share is not None
            else None,
            savings_per_request_rub=None,
            savings_daily_rub=None,
            savings_period_rub=None,
            savings_pct=None,
            net_savings_daily_rub=None,
            net_savings_period_rub=None,
            net_savings_pct=None,
        )

    daily_tokens_in = req_day_dec * (avg_tokens_in_per_request or ZERO)
    daily_tokens_out = req_day_dec * (avg_tokens_out_per_request or ZERO)
    daily_variable_cost_rub = req_day_dec * active_variable_cost_rub
    daily_total_cost_rub = daily_variable_cost_rub + daily_infra_cost_rub
    period_variable_cost_rub = req_period_dec * active_variable_cost_rub
    period_total_cost_rub = period_variable_cost_rub + period_infra_cost_rub
    full_cost_per_request_rub = period_total_cost_rub / req_period_dec

    daily_residual_operator, _, net_savings_daily_rub, _ = _pilot_costs(
        operator_daily_cost_rub, daily_total_cost_rub if complete_costs else None, labor_share
    )
    period_residual_operator, _, net_savings_period_rub, net_savings_pct = _pilot_costs(
        operator_period_cost_rub, period_total_cost_rub if complete_costs else None, labor_share
    )

    savings_per_request_rub = operator_cost_per_request_rub - full_cost_per_request_rub
    savings_daily_rub = operator_daily_cost_rub - daily_total_cost_rub
    savings_period_rub = operator_period_cost_rub - period_total_cost_rub
    savings_pct = (savings_period_rub / operator_period_cost_rub) * HUNDRED if operator_period_cost_rub > ZERO else ZERO
    return LoadScenarioEconomics(
        requests_per_day=requests_per_day,
        period_days=period_days,
        requests_per_period=requests_per_period,
        daily_tokens_in=daily_tokens_in,
        daily_tokens_out=daily_tokens_out,
        daily_variable_cost_rub=daily_variable_cost_rub,
        daily_infra_cost_rub=daily_infra_cost_rub,
        daily_total_cost_rub=daily_total_cost_rub,
        period_variable_cost_rub=period_variable_cost_rub,
        period_infra_cost_rub=period_infra_cost_rub,
        period_total_cost_rub=period_total_cost_rub,
        full_cost_per_request_rub=full_cost_per_request_rub,
        operator_daily_cost_rub=operator_daily_cost_rub,
        operator_period_cost_rub=operator_period_cost_rub,
        daily_residual_operator_cost_rub=daily_residual_operator,
        period_residual_operator_cost_rub=period_residual_operator,
        savings_per_request_rub=savings_per_request_rub,
        savings_daily_rub=savings_daily_rub,
        savings_period_rub=savings_period_rub,
        savings_pct=savings_pct,
        net_savings_daily_rub=net_savings_daily_rub,
        net_savings_period_rub=net_savings_period_rub,
        net_savings_pct=net_savings_pct,
    )


def _weighted_share(
    dialogs: list[DialogMeasurement], summary: SplitSummary, flag: Callable[[DialogMeasurement], bool | None]
) -> Decimal | None:
    if not dialogs or any(flag(d) is None for d in dialogs):
        return None
    if all(d.category is None for d in dialogs):
        return Decimal(sum(1 for d in dialogs if flag(d))) / len(dialogs)
    groups: dict[str, list[DialogMeasurement]] = defaultdict(list)
    for d in dialogs:
        groups[d.category].append(d)
    weights = {category: summary.categories[category].share for category in groups}
    total_weight = sum(weights.values(), ZERO)
    if not total_weight:
        return Decimal(sum(1 for d in dialogs if flag(d))) / len(dialogs)
    return (
        sum(
            weights[category] * Decimal(sum(1 for d in group if flag(d))) / len(group)
            for category, group in groups.items()
        )
        / total_weight
    )


def compute_unit_economics(
    dialogs: list[DialogMeasurement],
    split_summary: SplitSummary,
    pricing: PricingConfig,
    *,
    usd_rub_rate: Decimal | None,
    requests_per_period: int = DEFAULT_REQUESTS_PER_PERIOD,
    period_days: int = DEFAULT_PERIOD_DAYS,
    load_requests_per_day: int = DEFAULT_LOAD_REQUESTS_PER_DAY,
    telemetry_warnings: tuple[str, ...] = (),
) -> UnitEconomicsResult:
    """Агрегирует шаги агента, взвешивает по долям категорий и считает экономику за период и нагрузочный сценарий."""
    if requests_per_period <= 0:
        raise ValueError("requests_per_period должно быть > 0.")
    if period_days <= 0:
        raise ValueError("period_days должно быть > 0.")
    if load_requests_per_day <= 0:
        raise ValueError("load_requests_per_day должно быть > 0.")

    infra_rate = _currency_multiplier(pricing.infrastructure.currency, usd_rub_rate, "инфраструктуры")
    daily_infra_cost_rub = pricing.infrastructure.daily_cost * infra_rate

    req_period_dec = Decimal(requests_per_period)
    period_days_dec = Decimal(period_days)
    period_infra_cost_rub = daily_infra_cost_rub * period_days_dec

    operator_cost_per_request_rub = pricing.operator.hourly_rate_rub * pricing.operator.minutes_per_request / SIXTY
    operator_period_cost_rub = operator_cost_per_request_rub * req_period_dec

    total_dialogs = len(dialogs)
    known_outcomes = sum(d.needs_operator is not None for d in dialogs)
    escalated_dialogs = sum(d.escalated is True for d in dialogs)
    escalation_rate = _weighted_share(dialogs, split_summary, lambda d: d.confirmed_escalation)
    labor_share = _weighted_share(dialogs, split_summary, lambda d: d.needs_operator)
    dialogs = [d for d in dialogs if d.calls is not None]
    complete_costs = len(dialogs) == total_dialogs
    if not complete_costs:
        telemetry_warnings = (
            *telemetry_warnings,
            f"Телеметрия вызовов есть у {len(dialogs)} из {total_dialogs} обращений. "
            "Оценка технических расходов опирается на замеренные обращения. Чистая экономия не определена.",
        )

    # Пустые замеры — честный результат без подстановки нулей в переменную стоимость.
    if not dialogs:
        empty_cats = tuple(
            CategoryBreakdown(
                name=cat.name,
                archive_count=cat.archive_count,
                archive_share=cat.share,
                measured_dialogs=0,
                status="no_data",
                avg_variable_cost_rub=None,
            )
            for cat in split_summary.categories.values()
        )
        load_scenario = _compute_load_scenario(
            active_variable_cost_rub=None,
            avg_tokens_in_per_request=None,
            avg_tokens_out_per_request=None,
            labor_share=labor_share,
            daily_infra_cost_rub=daily_infra_cost_rub,
            operator_cost_per_request_rub=operator_cost_per_request_rub,
            requests_per_day=load_requests_per_day,
            period_days=period_days,
        )
        return UnitEconomicsResult(
            has_measurements=False,
            weighting_mode="none",
            measured_dialogs=0,
            total_dialogs=total_dialogs,
            known_outcomes=known_outcomes,
            escalated_dialogs=escalated_dialogs,
            escalation_rate=escalation_rate,
            covered_archive_share=ZERO,
            uncovered_archive_share=Decimal("1"),
            missing_categories=tuple(split_summary.categories.keys()),
            usd_rub_rate=usd_rub_rate,
            period_days=period_days,
            requests_per_period=requests_per_period,
            steps=(),
            categories=empty_cats,
            avg_tokens_in_per_request=None,
            avg_tokens_out_per_request=None,
            daily_tokens_in=None,
            daily_tokens_out=None,
            weighted_variable_cost_rub=None,
            unweighted_variable_cost_rub=None,
            active_variable_cost_rub=None,
            daily_infra_cost_rub=daily_infra_cost_rub,
            period_variable_cost_rub=None,
            period_infra_cost_rub=period_infra_cost_rub,
            period_total_cost_rub=None,
            full_cost_per_request_rub=None,
            operator_cost_per_request_rub=operator_cost_per_request_rub,
            operator_period_cost_rub=operator_period_cost_rub,
            period_residual_operator_cost_rub=operator_period_cost_rub * labor_share
            if labor_share is not None
            else None,
            period_pilot_total_cost_rub=None,
            savings_per_request_rub=None,
            savings_period_rub=None,
            savings_pct=None,
            net_savings_period_rub=None,
            net_savings_pct=None,
            load_scenario=load_scenario,
            telemetry_warnings=tuple(telemetry_warnings),
        )

    # Проверяем наличие всех моделей в pricing.yaml и считаем стоимость каждого диалога.
    dialog_costs: list[Decimal] = []
    step_keys_order: list[tuple[str, str]] = []
    step_keys_seen: set[tuple[str, str]] = set()
    raw_call_counts: dict[tuple[str, str], int] = {}

    for dlg in dialogs:
        d_cost = ZERO
        for call in dlg.calls:
            d_cost += _call_cost_rub(call, pricing, usd_rub_rate)
            key = (call.step, call.model)
            if key not in step_keys_seen:
                step_keys_seen.add(key)
                step_keys_order.append(key)
            raw_call_counts[key] = raw_call_counts.get(key, 0) + 1
        dialog_costs.append(d_cost)

    unweighted_variable_cost_rub = sum(dialog_costs, ZERO) / Decimal(len(dialogs))

    is_categorized = all(d.category is not None for d in dialogs)

    # Группировка по категориям и расчёт весов.
    dialogs_by_cat: dict[str, list[tuple[DialogMeasurement, Decimal]]] = {}
    if is_categorized:
        for dlg, cost in zip(dialogs, dialog_costs, strict=True):
            assert dlg.category is not None
            dialogs_by_cat.setdefault(dlg.category, []).append((dlg, cost))

    category_rows: list[CategoryBreakdown] = []
    missing_cats: list[str] = []
    covered_share = ZERO
    cat_avg_costs: dict[str, Decimal] = {}

    for cat_name, share_info in split_summary.categories.items():
        items = dialogs_by_cat.get(cat_name, [])
        if not items:
            missing_cats.append(cat_name)
            category_rows.append(
                CategoryBreakdown(
                    name=cat_name,
                    archive_count=share_info.archive_count,
                    archive_share=share_info.share,
                    measured_dialogs=0,
                    status="no_data",
                    avg_variable_cost_rub=None,
                )
            )
        else:
            avg_c = sum((c for _, c in items), ZERO) / Decimal(len(items))
            cat_avg_costs[cat_name] = avg_c
            covered_share += share_info.share
            category_rows.append(
                CategoryBreakdown(
                    name=cat_name,
                    archive_count=share_info.archive_count,
                    archive_share=share_info.share,
                    measured_dialogs=len(items),
                    status="measured",
                    avg_variable_cost_rub=avg_c,
                )
            )

    uncovered_share = Decimal("1") - covered_share

    # Нормализованные веса по замеренным категориям (без заполнения пустых категорий средним!).
    if is_categorized and covered_share > ZERO:
        weighting_mode = "archive_weighted"
        norm_weights = {
            cat_name: split_summary.categories[cat_name].share / covered_share for cat_name in cat_avg_costs
        }
        weighted_variable_cost_rub: Decimal | None = sum(
            (norm_weights[c] * cat_avg_costs[c] for c in cat_avg_costs),
            ZERO,
        )
        active_variable_cost_rub = weighted_variable_cost_rub
    else:
        weighting_mode = "unweighted"
        norm_weights = {}
        weighted_variable_cost_rub = None
        active_variable_cost_rub = unweighted_variable_cost_rub

    # Агрегация по шагам агента (step, model).
    # Важно: чтобы формула шаблона calls_per_request * (avg_in/1e6 * P_in + avg_out/1e6 * P_out) * rate
    # в точности равнялась взвешенной стоимости шага на 1 обращение, считаем:
    #   calls_per_req = sum_c w_c * mean_calls(c)
    #   avg_in_per_call = (sum_c w_c * mean_in_tokens_per_dialog(c)) / calls_per_req
    #   avg_out_per_call = (sum_c w_c * mean_out_tokens_per_dialog(c)) / calls_per_req
    step_aggs: list[StepAggregate] = []
    for step_name, model_name in step_keys_order:
        if weighting_mode == "archive_weighted":
            w_calls = ZERO
            w_in_per_dialog = ZERO
            w_out_per_dialog = ZERO
            for cat_name, weight in norm_weights.items():
                items = dialogs_by_cat[cat_name]
                n_dlgs = Decimal(len(items))
                cat_calls = sum(
                    sum(1 for c in dlg.calls if c.step == step_name and c.model == model_name) for dlg, _ in items
                )
                cat_in = sum(
                    sum(c.tokens_in for c in dlg.calls if c.step == step_name and c.model == model_name)
                    for dlg, _ in items
                )
                cat_out = sum(
                    sum(c.tokens_out for c in dlg.calls if c.step == step_name and c.model == model_name)
                    for dlg, _ in items
                )
                w_calls += weight * (Decimal(cat_calls) / n_dlgs)
                w_in_per_dialog += weight * (Decimal(cat_in) / n_dlgs)
                w_out_per_dialog += weight * (Decimal(cat_out) / n_dlgs)
        else:
            n_dlgs = Decimal(len(dialogs))
            tot_calls = sum(
                sum(1 for c in dlg.calls if c.step == step_name and c.model == model_name) for dlg in dialogs
            )
            tot_in = sum(
                sum(c.tokens_in for c in dlg.calls if c.step == step_name and c.model == model_name) for dlg in dialogs
            )
            tot_out = sum(
                sum(c.tokens_out for c in dlg.calls if c.step == step_name and c.model == model_name) for dlg in dialogs
            )
            w_calls = Decimal(tot_calls) / n_dlgs
            w_in_per_dialog = Decimal(tot_in) / n_dlgs
            w_out_per_dialog = Decimal(tot_out) / n_dlgs

        if w_calls > ZERO:
            avg_in = w_in_per_dialog / w_calls
            avg_out = w_out_per_dialog / w_calls
        else:
            avg_in = ZERO
            avg_out = ZERO

        mp = pricing.models[model_name]
        rate = _currency_multiplier(mp.currency, usd_rub_rate, f"модели {model_name!r}")
        step_cost_rub = (
            w_calls * ((avg_in / ONE_MILLION) * mp.input_per_1m + (avg_out / ONE_MILLION) * mp.output_per_1m) * rate
        )

        step_aggs.append(
            StepAggregate(
                step=step_name,
                model=model_name,
                calls_per_request=w_calls,
                avg_tokens_in=avg_in,
                avg_tokens_out=avg_out,
                price_in_per_1m=mp.input_per_1m,
                price_out_per_1m=mp.output_per_1m,
                currency=mp.currency,
                cost_per_request_rub=step_cost_rub,
                raw_call_count=raw_call_counts[(step_name, model_name)],
            )
        )

    avg_tokens_in_per_request = sum((s.calls_per_request * s.avg_tokens_in for s in step_aggs), ZERO)
    avg_tokens_out_per_request = sum((s.calls_per_request * s.avg_tokens_out for s in step_aggs), ZERO)
    base_req_per_day = req_period_dec / period_days_dec
    daily_tokens_in = avg_tokens_in_per_request * base_req_per_day
    daily_tokens_out = avg_tokens_out_per_request * base_req_per_day

    period_variable_cost_rub = req_period_dec * active_variable_cost_rub
    period_total_cost_rub = period_variable_cost_rub + period_infra_cost_rub
    full_cost_per_request_rub = period_total_cost_rub / req_period_dec

    period_residual_operator_cost_rub, period_pilot_total_cost_rub, net_savings_period_rub, net_savings_pct = (
        _pilot_costs(operator_period_cost_rub, period_total_cost_rub if complete_costs else None, labor_share)
    )

    savings_per_request_rub = operator_cost_per_request_rub - full_cost_per_request_rub
    savings_period_rub = operator_period_cost_rub - period_total_cost_rub
    savings_pct = (savings_period_rub / operator_period_cost_rub) * HUNDRED if operator_period_cost_rub > ZERO else ZERO
    load_scenario = _compute_load_scenario(
        active_variable_cost_rub=active_variable_cost_rub,
        avg_tokens_in_per_request=avg_tokens_in_per_request,
        avg_tokens_out_per_request=avg_tokens_out_per_request,
        labor_share=labor_share,
        complete_costs=complete_costs,
        daily_infra_cost_rub=daily_infra_cost_rub,
        operator_cost_per_request_rub=operator_cost_per_request_rub,
        requests_per_day=load_requests_per_day,
        period_days=period_days,
    )

    return UnitEconomicsResult(
        has_measurements=True,
        weighting_mode=weighting_mode,
        measured_dialogs=len(dialogs),
        total_dialogs=total_dialogs,
        known_outcomes=known_outcomes,
        escalated_dialogs=escalated_dialogs,
        escalation_rate=escalation_rate,
        covered_archive_share=covered_share,
        uncovered_archive_share=uncovered_share,
        missing_categories=tuple(missing_cats),
        usd_rub_rate=usd_rub_rate,
        period_days=period_days,
        requests_per_period=requests_per_period,
        steps=tuple(step_aggs),
        categories=tuple(category_rows),
        avg_tokens_in_per_request=avg_tokens_in_per_request,
        avg_tokens_out_per_request=avg_tokens_out_per_request,
        daily_tokens_in=daily_tokens_in,
        daily_tokens_out=daily_tokens_out,
        weighted_variable_cost_rub=weighted_variable_cost_rub,
        unweighted_variable_cost_rub=unweighted_variable_cost_rub,
        active_variable_cost_rub=active_variable_cost_rub,
        daily_infra_cost_rub=daily_infra_cost_rub,
        period_variable_cost_rub=period_variable_cost_rub,
        period_infra_cost_rub=period_infra_cost_rub,
        period_total_cost_rub=period_total_cost_rub,
        full_cost_per_request_rub=full_cost_per_request_rub,
        operator_cost_per_request_rub=operator_cost_per_request_rub,
        operator_period_cost_rub=operator_period_cost_rub,
        period_residual_operator_cost_rub=period_residual_operator_cost_rub,
        period_pilot_total_cost_rub=period_pilot_total_cost_rub,
        savings_per_request_rub=savings_per_request_rub,
        savings_period_rub=savings_period_rub,
        savings_pct=savings_pct,
        net_savings_period_rub=net_savings_period_rub,
        net_savings_pct=net_savings_pct,
        load_scenario=load_scenario,
        telemetry_warnings=tuple(telemetry_warnings),
    )


# --- Поиск структуры шаблона .xlsx и запись без порчи формул --------------------


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _merged_value(ws: Worksheet, row: int, col: int) -> Any:
    """Возвращает значение ячейки с учётом объединённых диапазонов."""
    val = ws.cell(row=row, column=col).value
    if val is not None:
        return val
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return ws.cell(row=rng.min_row, column=rng.min_col).value
    return None


def _top_left_of_merge(ws: Worksheet, row: int, col: int) -> tuple[int, int]:
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return (rng.min_row, rng.min_col)
    return (row, col)


def _merge_span(ws: Worksheet, row: int, col: int) -> tuple[int, int, int, int]:
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return (rng.min_row, rng.min_col, rng.max_row, rng.max_col)
    return (row, col, row, col)


def _as_positive_decimal(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool) or _is_formula(val):
        return None
    if isinstance(val, int | float | Decimal):
        d = Decimal(str(val))
        return d if d.is_finite() and d > ZERO else None
    if isinstance(val, str):
        cleaned = val.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            d = Decimal(cleaned)
        except InvalidOperation:
            return None
        return d if d.is_finite() and d > ZERO else None
    return None


def _col_letters_to_index(letters: str) -> int:
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def _find_cbr_rate_cell(
    ws: Worksheet,
    *,
    data_rows: tuple[int, ...] = (),
) -> tuple[tuple[int, int], Decimal | None]:
    """Ищет ячейку курса ЦБ: сначала по ссылке в формулах таблицы ($I$3), затем по всей строке метки «Курс ЦБ»."""
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0

    # 1. Ищем метку «Курс ЦБ» / «Курс USD» (например, в A3 без объединения или H3).
    rate_label_rows: list[tuple[int, int, int, int]] = []
    for r in range(1, max_r + 1):
        for c in range(1, max_c + 1):
            text = _norm_text(ws.cell(row=r, column=c).value)
            if not text or "курс" not in text:
                continue
            if any(tok in text for tok in ("цб", "usd", "доллар", "руб", "$", "валют")):
                rate_label_rows.append(_merge_span(ws, r, c))

    # 2. Проверяем абсолютную ссылку на ячейку курса в формулах (например, IF(H9="$",$I$3,1) -> I3).
    scan_rows = data_rows or tuple(range(1, max_r + 1))
    for r in scan_rows:
        for c in range(1, max_c + 1):
            val = ws.cell(row=r, column=c).value
            if not _is_formula(val):
                continue
            for col_letters, row_str in CELL_REF_RE.findall(str(val)):
                cr, cc = int(row_str), _col_letters_to_index(col_letters)
                if cr in scan_rows or cr > max_r + 5 or cc > max_c + 5:
                    continue
                tl = _top_left_of_merge(ws, cr, cc)
                cell_val = ws.cell(row=tl[0], column=tl[1]).value
                if _is_formula(cell_val):
                    continue
                parsed = _as_positive_decimal(cell_val)
                if parsed is not None or any(min_r <= tl[0] <= max_mr for min_r, _, max_mr, _ in rate_label_rows):
                    return tl, parsed

    # 3. Если формулы не дали ссылку, ищем положительное число по всей строке метки (A3 -> I3) и под ней.
    for min_r, min_c, max_mr, max_mc in rate_label_rows:
        candidates = [(min_r, cc) for cc in range(max_mc + 1, max_c + 1)]
        if max_mr + 1 <= max_r:
            candidates.append((max_mr + 1, min_c))
        for cr, cc in candidates:
            tl = _top_left_of_merge(ws, cr, cc)
            val = _as_positive_decimal(ws.cell(row=tl[0], column=tl[1]).value)
            if val is not None:
                return tl, val
        # Пустая ячейка курса в строке метки (например, пустой I3 до подстановки --usd-rub-rate).
        if max_c > max_mc:
            return _top_left_of_merge(ws, min_r, max_c), None

    raise TemplateLayoutError(
        f"На листе '{ws.title}' не найдена ячейка курса ЦБ: ожидалась ячейка курса "
        "рядом с меткой «Курс ЦБ» / «Курс USD» или по ссылке $I$3 в формулах."
    )


def _classify_header(text: str) -> str | None:
    """Сопоставляет текст заголовка столбца (или составного заголовка) с полем таблицы шагов."""
    if not text:
        return None
    # Не путаем расчётные столбцы (рубли / итого / сумма) с входными полями.
    if any(tok in text for tok in ("руб", "итого", "всего", "затрат")) and "валют" not in text:
        if "цена" not in text and "прайс" not in text:
            return None
    if "шаг" in text or "этап" in text or text == "step":
        return "step"
    if "модел" in text or text == "model":
        return "model"
    if "вызов" in text or "calls" in text:
        return "calls"
    if "валют" in text or "currency" in text:
        return "currency"

    is_in = any(tok in text for tok in ("вход", "input", "prompt"))
    is_out = any(tok in text for tok in ("выход", "output", "completion"))
    is_price = any(tok in text for tok in ("цена", "прайс", "тариф", "1 млн", "1млн", "price", "млн"))
    is_tokens = any(tok in text for tok in ("токен", "token"))

    if is_price and is_in and not is_out:
        return "price_in"
    if is_price and is_out and not is_in:
        return "price_out"
    if is_tokens and is_in and not is_out:
        return "tokens_in"
    if is_tokens and is_out and not is_in:
        return "tokens_out"
    return None


REQUIRED_COLUMNS = (
    "step",
    "model",
    "calls",
    "tokens_in",
    "tokens_out",
    "price_in",
    "price_out",
    "currency",
)

COLUMN_LABELS_RU = {
    "step": "шаг агента",
    "model": "модель",
    "calls": "вызовов на запрос",
    "tokens_in": "средние токены вход",
    "tokens_out": "средние токены выход",
    "price_in": "цена за 1 млн (вход)",
    "price_out": "цена за 1 млн (выход)",
    "currency": "валюта",
}

SUMMARY_ROW_KEYWORDS = ("итого", "всего", "total", "сумма", "комментар", "примечан", "пояснен")


def _find_step_table(ws: Worksheet) -> tuple[int, dict[str, int], tuple[int, ...]]:
    """Ищет строку заголовков таблицы шагов по тексту заголовков и доступные строки данных."""
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0

    best_row = -1
    best_cols: dict[str, int] = {}

    for r in range(1, max_r + 1):
        cols: dict[str, int] = {}
        for c in range(1, max_c + 1):
            cur = _norm_text(_merged_value(ws, r, c))
            parent = _norm_text(_merged_value(ws, r - 1, c)) if r > 1 else ""
            # Сначала пробуем саму ячейку, затем составной заголовок (для двухстрочных шапок).
            field_key = _classify_header(cur)
            if field_key is None and parent and parent != cur:
                field_key = _classify_header(f"{parent} {cur}")
            if field_key is not None and field_key not in cols:
                cols[field_key] = c
        if len(cols) > len(best_cols):
            best_cols = cols
            best_row = r
        if len(cols) == len(REQUIRED_COLUMNS):
            break

    missing = [col for col in REQUIRED_COLUMNS if col not in best_cols]
    if missing:
        missing_desc = ", ".join(f"{m} ({COLUMN_LABELS_RU[m]})" for m in missing)
        found_desc = ", ".join(sorted(best_cols.keys())) or "ни одного"
        raise TemplateLayoutError(
            f"На листе '{ws.title}' не найдена полная строка заголовков таблицы шагов агента. "
            f"Не найдены столбцы: {missing_desc}. Найдено (строка {best_row}): {found_desc}."
        )

    # Определяем строки таблицы данных ниже header_row до строки «Итого»/комментариев/пустого блока.
    data_rows: list[int] = []
    summary_row: int | None = None
    step_col = best_cols["step"]
    for r in range(best_row + 1, max_r + 1):
        row_texts = [_norm_text(ws.cell(row=r, column=c).value) for c in range(1, max_c + 1)]
        first_non_empty = next((t for t in row_texts if t and not t.startswith("=")), "")
        step_text = _norm_text(ws.cell(row=r, column=step_col).value)
        if any(kw in step_text for kw in SUMMARY_ROW_KEYWORDS) or any(
            first_non_empty.startswith(kw) for kw in SUMMARY_ROW_KEYWORDS
        ):
            summary_row = r
            break

        has_formula_in_row = any(_is_formula(ws.cell(row=r, column=c).value) for c in range(1, max_c + 1))
        has_any_content = any(ws.cell(row=r, column=c).value is not None for c in range(1, max_c + 1))
        if has_formula_in_row or has_any_content:
            data_rows.append(r)
        elif data_rows:
            # Пустая строка после блока строк с формулами/шаблонами — конец таблицы.
            break

    # Если в итоговой строке есть SUM(I9:I12), используем строго диапазон итога — серый пример в строке 8 не трогаем.
    if summary_row is not None:
        for c in range(1, max_c + 1):
            val = ws.cell(row=summary_row, column=c).value
            if _is_formula(val):
                m = SUM_RANGE_RE.search(str(val))
                if m:
                    start_r, end_r = int(m.group(1)), int(m.group(2))
                    if start_r > best_row and end_r >= start_r and end_r < summary_row:
                        data_rows = list(range(start_r, end_r + 1))
                        break
    elif data_rows and _norm_text(ws.cell(row=data_rows[0], column=step_col).value).startswith("пример"):
        data_rows = data_rows[1:]

    if not data_rows:
        raise TemplateLayoutError(
            f"На листе '{ws.title}' после строки заголовков {best_row} не найдено ни одной строки данных таблицы шагов."
        )

    return best_row, best_cols, tuple(data_rows)


def _detect_currency_symbols(
    ws: Worksheet,
    data_rows: tuple[int, ...],
    header_row: int,
    currency_col: int,
) -> dict[str, str]:
    """Определяет формат обозначения валюты в шаблоне: символы ('₽'/'$') или ISO ('RUB'/'USD')."""
    max_c = ws.max_column or 0
    for r in data_rows:
        for c in range(1, max_c + 1):
            val = ws.cell(row=r, column=c).value
            if not _is_formula(val):
                continue
            formula = str(val)
            if '"$"' in formula or "'$'" in formula:
                return {"USD": "$", "RUB": "₽"}
            if '"USD"' in formula.upper():
                return {"USD": "USD", "RUB": "RUB"}
    # Проверяем строку-пример (если есть между header_row и data_rows[0]) или ячейки столбца валюты.
    check_rows = list(range(header_row + 1, data_rows[0])) + list(data_rows)
    for r in check_rows:
        val = str(ws.cell(row=r, column=currency_col).value or "").strip()
        if val in {"$", "₽"}:
            return {"USD": "$", "RUB": "₽"}
        if val.upper() in {"USD", "RUB"}:
            return {"USD": "USD", "RUB": "RUB"}
    return {"USD": "$", "RUB": "₽"}


def _find_comment_cells(ws: Worksheet, *, min_row: int = 1) -> tuple[tuple[int, int], ...]:
    """Ищет блок комментария (до 3 строк) НИЖЕ таблицы шагов по метке «Комментарий» / «Примечание»."""
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    for r in range(max(1, min_row), max_r + 1):
        for c in range(1, max_c + 1):
            text = _norm_text(ws.cell(row=r, column=c).value)
            if not text or not any(kw in text for kw in ("комментар", "примечан", "пояснен")):
                continue
            min_r, min_c, max_mr, max_mc = _merge_span(ws, r, c)

            # Вариант 1: справа от метки находится объединённый блок (например, B15:I17)
            # или заполненная ячейка-плейсхолдер.
            if max_mc + 1 <= max_c:
                right_tl = _top_left_of_merge(ws, min_r, max_mc + 1)
                right_val = ws.cell(row=right_tl[0], column=right_tl[1]).value
                if not _is_formula(right_val) and right_tl != (min_r, min_c):
                    r_min_r, r_min_c, r_max_r, r_max_c = _merge_span(ws, right_tl[0], right_tl[1])
                    is_merged_box = (r_max_r > r_min_r) or (r_max_c > r_min_c)
                    has_placeholder = bool(_norm_text(right_val))
                    if is_merged_box or has_placeholder:
                        stacked_right: list[tuple[int, int]] = [right_tl]
                        if r_max_r == r_min_r:
                            for offset in (1, 2):
                                nr = right_tl[0] + offset
                                if nr <= max_r:
                                    left_label = _norm_text(ws.cell(row=nr, column=min_c).value)
                                    nxt_tl = _top_left_of_merge(ws, nr, right_tl[1])
                                    nxt_val = ws.cell(row=nxt_tl[0], column=nxt_tl[1]).value
                                    if not left_label and not _is_formula(nxt_val) and nxt_tl not in stacked_right:
                                        stacked_right.append(nxt_tl)
                        return tuple(stacked_right[:3])

            # Вариант 2: строки под меткой «Комментарий» (до 3 строк, например A17:A19 под A16).
            below_cells: list[tuple[int, int]] = []
            for offset in (1, 2, 3):
                nr = max_mr + offset
                if nr > max_r + 3:
                    break
                tl = _top_left_of_merge(ws, nr, min_c)
                if tl in below_cells:
                    continue
                val = ws.cell(row=tl[0], column=tl[1]).value
                if _is_formula(val):
                    break
                below_cells.append(tl)
            if below_cells:
                return tuple(below_cells[:3])

    raise TemplateLayoutError(
        f"На листе '{ws.title}' не найден блок комментария ниже таблицы шагов (ожидалась метка «Комментарий»)."
    )


def locate_template_layout(ws: Worksheet) -> TemplateLayout:
    """Определяет структуру листа шаблона без хардкода координат."""
    cbr_cell, cbr_rate = _find_cbr_rate_cell(ws)
    header_row, columns, data_rows = _find_step_table(ws)
    # Уточняем ячейку курса с учётом найденных строк шагов (формулы в data_rows).
    cbr_cell, cbr_rate = _find_cbr_rate_cell(ws, data_rows=data_rows)
    currency_symbols = _detect_currency_symbols(ws, data_rows, header_row, columns["currency"])
    comment_cells = _find_comment_cells(ws, min_row=max(data_rows) + 1)

    formula_cells: set[tuple[int, int]] = set()
    for r in range(1, (ws.max_row or 0) + 1):
        for c in range(1, (ws.max_column or 0) + 1):
            if _is_formula(ws.cell(row=r, column=c).value):
                formula_cells.add((r, c))

    return TemplateLayout(
        sheet_name=ws.title,
        cbr_rate_cell=cbr_cell,
        cbr_rate_value=cbr_rate,
        header_row=header_row,
        columns=columns,
        data_rows=data_rows,
        comment_cells=comment_cells,
        formula_cells=frozenset(formula_cells),
        currency_symbols=currency_symbols,
    )


def read_template_cbr_rate(template_path: Path, sheet_name: str | None = None) -> tuple[Decimal | None, TemplateLayout]:
    """Открывает шаблон .xlsx, проверяет его структуру и возвращает курс ЦБ (или None, если ячейка пуста) и layout."""
    if not template_path.is_file():
        raise TemplateLayoutError(f"Файл шаблона .xlsx не найден: {template_path}.")
    wb = openpyxl.load_workbook(template_path, data_only=False)
    try:
        ws = _select_worksheet(wb, sheet_name)
        layout = locate_template_layout(ws)
        return layout.cbr_rate_value, layout
    finally:
        wb.close()


def _select_worksheet(wb: openpyxl.Workbook, sheet_name: str | None) -> Worksheet:
    if sheet_name is not None:
        if sheet_name not in wb.sheetnames:
            raise TemplateLayoutError(f"Лист {sheet_name!r} не найден в шаблоне. Доступные листы: {wb.sheetnames}.")
        return wb[sheet_name]
    # Пробуем активный лист, а при ошибке — остальные листы.
    first_err: TemplateLayoutError | None = None
    ordered = [wb.active] + [wb[name] for name in wb.sheetnames if wb[name] is not wb.active]
    for ws in ordered:
        try:
            locate_template_layout(ws)
            return ws
        except TemplateLayoutError as exc:
            if first_err is None:
                first_err = exc
    assert first_err is not None
    raise first_err


def build_comment_lines(result: UnitEconomicsResult, pricing: PricingConfig) -> list[str]:
    """Формирует комментарий строго до 3 строк: допущения, покрытие и ссылки на прайсы."""
    if not result.has_measurements:
        line1 = "Допущения: замеры отсутствуют; доли категорий из архива чатов (не рабочий поток)."
    elif result.missing_categories:
        cov_pct = _fmt_dec(result.covered_archive_share * HUNDRED, "0.1")
        missing_str = ", ".join(result.missing_categories[:3])
        if len(result.missing_categories) > 3:
            missing_str += f" и ещё {len(result.missing_categories) - 3}"
        line1 = (
            f"Допущения: доли категорий из архива чатов (не рабочий поток); "
            f"покрытие {cov_pct}%, без замеров (не усреднялись): {missing_str}."
        )
    else:
        line1 = (
            f"Допущения: 1 запрос = 1 обращение ({result.measured_dialogs} диалогов); "
            "доли 9 категорий из архива чатов (не рабочий поток), покрытие 100%."
        )

    if result.measured_dialogs < result.total_dialogs:
        line1 += (
            f" Учтены вызовы {result.measured_dialogs}/{result.total_dialogs} диалогов; "
            f"известны исходы {result.known_outcomes}/{result.total_dialogs}."
        )
    used_models = {s.model for s in result.steps} or set(pricing.models.keys())
    model_refs = "; ".join(
        f"{m}: {pricing.models[m].source_url} ({pricing.models[m].date})"
        for m in sorted(used_models)
        if m in pricing.models
    )
    line2 = f"Прайсы LLM: {model_refs}."

    line3 = (
        f"База ({result.requests_per_period} обр./{result.period_days} дн.): "
        f"инфра {_fmt_dec(result.daily_infra_cost_rub)} ₽/сут "
        f"({_fmt_dec(result.period_infra_cost_rub)} ₽/период, "
        f"{pricing.infrastructure.source_url}, {pricing.infrastructure.date}); "
        f"оператор {_fmt_dec(result.operator_cost_per_request_rub)} ₽/обр ({pricing.operator.date}); "
        f"нагрузочный сценарий (не база): {result.load_scenario.requests_per_day} обр./сут."
    )
    return [line1, line2, line3]


def fill_xlsx_template(
    template_path: Path,
    output_path: Path,
    result: UnitEconomicsResult,
    pricing: PricingConfig,
    *,
    sheet_name: str | None = None,
) -> TemplateLayout:
    """Заполняет копию шаблона .xlsx, не трогая формулы и курс ЦБ."""
    if not template_path.is_file():
        raise TemplateLayoutError(f"Файл шаблона .xlsx не найден: {template_path}.")

    wb = openpyxl.load_workbook(template_path, data_only=False)
    try:
        ws = _select_worksheet(wb, sheet_name)
        layout = locate_template_layout(ws)

        if len(result.steps) > len(layout.data_rows):
            raise TemplateLayoutError(
                f"В шаблоне '{ws.title}' доступно {len(layout.data_rows)} строк для шагов агента, "
                f"а в расчёте получено {len(result.steps)} шагов."
            )

        def _safe_write(row: int, col: int, value: Any) -> None:
            if (row, col) == layout.cbr_rate_cell:
                raise TemplateLayoutError(f"Попытка перезаписать ячейку курса ЦБ {(row, col)}.")
            if (row, col) in layout.formula_cells or _is_formula(ws.cell(row=row, column=col).value):
                raise TemplateLayoutError(f"Попытка перезаписать ячейку с формулой {(row, col)}.")
            ws.cell(row=row, column=col).value = value

        # Если ячейка курса в шаблоне (например, I3) была пустой, а курс передан — заполняем её.
        # Уже заполненный курс ЦБ в шаблоне никогда не перезаписывается.
        if layout.cbr_rate_value is None and result.usd_rub_rate is not None:
            cr, cc = layout.cbr_rate_cell
            if (cr, cc) not in layout.formula_cells and not _is_formula(ws.cell(row=cr, column=cc).value):
                ws.cell(row=cr, column=cc).value = float(result.usd_rub_rate)

        # Записываем строки шагов агента (только в data_rows; строку-пример не трогаем).
        for idx, row_num in enumerate(layout.data_rows):
            if idx < len(result.steps):
                step = result.steps[idx]
                row_values = {
                    "step": step.step,
                    "model": step.model,
                    "calls": float(step.calls_per_request),
                    "tokens_in": float(step.avg_tokens_in),
                    "tokens_out": float(step.avg_tokens_out),
                    "price_in": float(step.price_in_per_1m),
                    "price_out": float(step.price_out_per_1m),
                    "currency": layout.currency_symbols.get(step.currency, step.currency),
                }
                for field_key, col_idx in layout.columns.items():
                    _safe_write(row_num, col_idx, row_values[field_key])
            else:
                # Очищаем неиспользуемые входные ячейки, формулы не трогаем.
                for col_idx in layout.columns.values():
                    if (row_num, col_idx) not in layout.formula_cells:
                        _safe_write(row_num, col_idx, None)

        # Записываем комментарий (строго <= 3 строк).
        comment_lines = build_comment_lines(result, pricing)[:3]
        if len(layout.comment_cells) >= len(comment_lines):
            for (r, c), line in zip(layout.comment_cells, comment_lines, strict=False):
                _safe_write(r, c, line)
        else:
            r, c = layout.comment_cells[0]
            _safe_write(r, c, "\n".join(comment_lines))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        return layout
    finally:
        wb.close()


# --- Генерация docs/cost_report.md ----------------------------------------------


def render_markdown_report(result: UnitEconomicsResult, pricing: PricingConfig) -> str:
    """Формирует воспроизводимый Markdown-отчёт по unit-экономике."""
    load = result.load_scenario
    rate_str = _fmt_dec(result.usd_rub_rate, "0.0001") if result.usd_rub_rate else "не использовался"
    w_var = _fmt_dec(result.weighted_variable_cost_rub, "0.0001")
    u_var = _fmt_dec(result.unweighted_variable_cost_rub, "0.0001")
    tok_in_day = _fmt_dec(result.daily_tokens_in, "0.01")
    tok_out_day = _fmt_dec(result.daily_tokens_out, "0.01")
    p_var = _fmt_dec(result.period_variable_cost_rub, "0.01")
    d_inf = _fmt_dec(result.daily_infra_cost_rub, "0.01")
    p_inf = _fmt_dec(result.period_infra_cost_rub, "0.01")
    p_tot = _fmt_dec(result.period_total_cost_rub, "0.01")
    full_req = _fmt_dec(result.full_cost_per_request_rub, "0.0001")
    op_hr = _fmt_dec(pricing.operator.hourly_rate_rub, "0.01")
    op_min = _fmt_dec(pricing.operator.minutes_per_request, "0.01")
    op_req = _fmt_dec(result.operator_cost_per_request_rub, "0.01")
    op_per = _fmt_dec(result.operator_period_cost_rub, "0.01")
    esc_pct_cell = (
        f"{_fmt_dec(result.escalation_rate * HUNDRED, '0.01')}%" if result.escalation_rate is not None else "н/д"
    )
    res_op_per = _fmt_dec(result.period_residual_operator_cost_rub, "0.01")
    pilot_tot_per = _fmt_dec(result.period_pilot_total_cost_rub, "0.01")
    net_sav_per = _fmt_dec(result.net_savings_period_rub, "0.01")
    net_sav_pct_cell = f"{_fmt_dec(result.net_savings_pct, '0.01')}%" if result.net_savings_pct is not None else "н/д"
    sav_req = _fmt_dec(result.savings_per_request_rub, "0.01")
    sav_per = _fmt_dec(result.savings_period_rub, "0.01")
    sav_pct_cell = f"{_fmt_dec(result.savings_pct, '0.01')}%" if result.savings_pct is not None else "н/д"

    l_tok_in_day = _fmt_dec(load.daily_tokens_in, "0.01")
    l_tok_out_day = _fmt_dec(load.daily_tokens_out, "0.01")
    l_d_var = _fmt_dec(load.daily_variable_cost_rub, "0.01")
    l_d_tot = _fmt_dec(load.daily_total_cost_rub, "0.01")
    l_p_var = _fmt_dec(load.period_variable_cost_rub, "0.01")
    l_p_inf = _fmt_dec(load.period_infra_cost_rub, "0.01")
    l_p_tot = _fmt_dec(load.period_total_cost_rub, "0.01")
    l_full_req = _fmt_dec(load.full_cost_per_request_rub, "0.0001")
    l_op_day = _fmt_dec(load.operator_daily_cost_rub, "0.01")
    l_op_per = _fmt_dec(load.operator_period_cost_rub, "0.01")
    l_res_op_per = _fmt_dec(load.period_residual_operator_cost_rub, "0.01")
    l_net_sav_day = _fmt_dec(load.net_savings_daily_rub, "0.01")
    l_net_sav_per = _fmt_dec(load.net_savings_period_rub, "0.01")
    l_net_sav_pct = f"{_fmt_dec(load.net_savings_pct, '0.01')}%" if load.net_savings_pct is not None else "н/д"
    l_sav_req = _fmt_dec(load.savings_per_request_rub, "0.01")
    l_sav_day = _fmt_dec(load.savings_daily_rub, "0.01")
    l_sav_per = _fmt_dec(load.savings_period_rub, "0.01")
    l_sav_pct_cell = f"{_fmt_dec(load.savings_pct, '0.01')}%" if load.savings_pct is not None else "н/д"

    total_archive = sum(c.archive_count for c in result.categories)
    measured_archive = sum(c.archive_count for c in result.categories if c.status == "measured")
    measured_cat_cnt = sum(1 for c in result.categories if c.status == "measured")
    total_cat_cnt = len(result.categories)
    cov_pct_str = _fmt_dec(result.covered_archive_share * HUNDRED, "0.01")
    uncov_pct_str = _fmt_dec(result.uncovered_archive_share * HUNDRED, "0.01")

    lines: list[str] = [
        "# Расчёт стоимости запроса и unit-экономики (T19)",
        "",
        "## Параметры расчёта",
        "",
        "- **Единица учёта**: 1 запрос = 1 обращение = все LLM-вызовы в диалоге.",
        f"- **Замерено диалогов**: {result.measured_dialogs} из {result.total_dialogs} с данными о вызовах.",
        f"- Исход известен для {result.known_outcomes} из {result.total_dialogs} обращений. "
        f"Подтверждено передач менеджеру: {result.escalated_dialogs}. Доля эскалаций: {esc_pct_cell}.",
        "- Для переданного или незавершённого обращения закладываем полный ручной разбор по ставке оператора. "
        "При неизвестном исходе затраты труда и чистая экономия не определены. Технические расходы учтены.",
        f"- **Режим взвешивания**: `{result.weighting_mode}`",
        f"- **Покрытие долей архива**: {cov_pct_str}% ({measured_archive} из {total_archive} обращений, "
        f"{measured_cat_cnt} из {total_cat_cnt} категорий; без замеров: {uncov_pct_str}%)",
        f"- **Курс USD/RUB (ЦБ)**: {rate_str}",
        f"- **Базовый период и объём**: {result.requests_per_period} обращений за {result.period_days} дн. "
        "(основной прогноз по подтверждённому объёму заказчика)",
        f"- **Отдельный нагрузочный сценарий (слайд 2, не базовый прогноз)**: "
        f"{load.requests_per_day} обращений/сутки ({load.requests_per_period} обращений за {load.period_days} дн.)",
        "",
        "> **Важное допущение**: доли категорий взяты из архива обращений (`split_summary.json`), "
        "а не из рабочего потока на витрине. Категории без замеров помечены явно и не заполняются средним.",
    ]

    if result.missing_categories and result.has_measurements:
        lines.extend(
            [
                "",
                f"> **Область прогноза (частичная выборка)**: замерено {measured_cat_cnt} из {total_cat_cnt} категорий "
                f"({measured_archive} из {total_archive} обращений архива, {cov_pct_str}%). "
                "Нормированная средняя стоимость ниже относится **только к составу замеренных категорий**; "
                "перенос на 100% потока является явным допущением до замера или оценки пропущенных категорий.",
            ]
        )

    for warn in result.telemetry_warnings:
        lines.extend(["", f"> **Предупреждение о неполноте данных**: {warn}"])

    lines.extend(
        [
            "",
            f"## Базовый расчёт за период ({result.period_days} дн., {result.requests_per_period} обращений)",
            "",
            "| Показатель | Значение |",
            "|---|---|",
            f"| Средняя переменная стоимость 1 обращения (взвешенная), ₽ | {w_var} |",
            f"| Средняя переменная стоимость 1 обращения (невзвешенная), ₽ | {u_var} |",
            f"| Суммарные входные токены в сутки (база {result.requests_per_period}/{result.period_days} обр./сут.) | "
            f"{tok_in_day} |",
            f"| Суммарные выходные токены в сутки (база {result.requests_per_period}/{result.period_days} обр./сут.) | "
            f"{tok_out_day} |",
            f"| Переменные расходы за период ({result.requests_per_period} × средняя переменная), ₽ | {p_var} |",
            f"| Суточная инфраструктура, ₽/сут. | {d_inf} |",
            f"| Инфраструктура за период (суточная ставка × {result.period_days} дн., "
            f"не зависит от числа обращений), ₽ | {p_inf} |",
            f"| **Итого за период ({result.requests_per_period} × переменная "
            f"+ инфраструктура за {result.period_days} дн.), ₽** | **{p_tot}** |",
            f"| Полная себестоимость 1 обращения за период (с инфраструктурой), ₽ | {full_req} |",
            "",
            f"## Сравнение с ручным маршрутом (оператором) — базовый объём "
            f"({result.requests_per_period} обр. / {result.period_days} дн.)",
            "",
            "| Показатель | Значение |",
            "|---|---|",
            f"| Стоимость часа оператора, ₽/ч | {op_hr} |",
            f"| Среднее время оператора на обращение, мин | {op_min} |",
            f"| Стоимость 1 обращения у оператора, ₽ | {op_req} |",
            f"| Затраты ручного маршрута за период ({result.requests_per_period} обр.), ₽ | {op_per} |",
            f"| Доля передач менеджеру в замерах, % | {esc_pct_cell} |",
            f"| Остаточные затраты оператора на переданных обращениях за период, ₽ | {res_op_per} |",
            f"| Итого в пилоте за период (агент + инфраструктура + остаточный оператор), ₽ | {pilot_tot_per} |",
            f"| **Чистая экономия за период с учётом передач менеджеру, ₽** | **{net_sav_per}** |",
            f"| **Чистая относительная экономия с учётом передач менеджеру, %** | **{net_sav_pct_cell}** |",
            f"| Верхняя граница экономии на 1 обращении (при полном замещении, без эскалаций), ₽ | {sav_req} |",
            f"| Верхняя граница экономии за период (при полном замещении, без эскалаций), ₽ | {sav_per} |",
            f"| Верхняя граница относительной экономии (при полном замещении, без эскалаций), % | {sav_pct_cell} |",
            "",
            f"## Нагрузочный сценарий (отдельный расчёт: {load.requests_per_day} обращений/сутки, не базовый прогноз)",
            "",
            f"> **Отдельный нагрузочный расчёт (слайд 2)**: {load.requests_per_day} обращений/сутки "
            f"({load.requests_per_period} обращений за {load.period_days} дн.). "
            "Используется для оценки поведения экономики при высокой нагрузке и **не является базовым прогнозом**.",
            "",
            "| Показатель (нагрузочный сценарий) | Значение |",
            "|---|---|",
            f"| Суммарные входные токены в сутки ({load.requests_per_day} обр./сут.) | {l_tok_in_day} |",
            f"| Суммарные выходные токены в сутки ({load.requests_per_day} обр./сут.) | {l_tok_out_day} |",
            f"| Переменные расходы в сутки ({load.requests_per_day} × средняя переменная), ₽ | {l_d_var} |",
            f"| Суточная инфраструктура, ₽ | {d_inf} |",
            f"| **Итого в сутки (агент + инфраструктура), ₽** | **{l_d_tot}** |",
            f"| Переменные расходы за {load.period_days} дн. ({load.requests_per_period} обр.), ₽ | {l_p_var} |",
            f"| Инфраструктура за {load.period_days} дн. (суточная ставка × {load.period_days} дн.), ₽ | {l_p_inf} |",
            f"| **Итого за {load.period_days} дн. (агент + инфраструктура), ₽** | **{l_p_tot}** |",
            f"| Полная себестоимость 1 обращения (нагрузочный сценарий), ₽ | {l_full_req} |",
            f"| Затраты оператора в сутки ({load.requests_per_day} обр.), ₽ | {l_op_day} |",
            f"| Затраты оператора за {load.period_days} дн. ({load.requests_per_period} обр.), ₽ | {l_op_per} |",
            f"| Остаточные затраты оператора на передачах за {load.period_days} дн., ₽ | {l_res_op_per} |",
            f"| Чистая экономия в сутки с учётом передач менеджеру, ₽ | {l_net_sav_day} |",
            f"| Чистая экономия за {load.period_days} дн. с учётом передач менеджеру, ₽ | {l_net_sav_per} |",
            f"| Чистая относительная экономия (нагрузочный сценарий), % | {l_net_sav_pct} |",
            f"| Верхняя граница экономии на 1 обращении (при полном замещении), ₽ | {l_sav_req} |",
            f"| Верхняя граница экономии в сутки (при полном замещении), ₽ | {l_sav_day} |",
            f"| Верхняя граница экономии за {load.period_days} дн. (при полном замещении), ₽ | {l_sav_per} |",
            f"| Верхняя граница относительной экономии (при полном замещении), % | {l_sav_pct_cell} |",
            "",
            "## Разбивка по шагам агента",
            "",
        ]
    )

    if result.steps:
        lines.extend(
            [
                "| Шаг агента | Модель | Вызовов / обращение | Ср. токены вход | Ср. токены выход | "
                "Цена 1 млн вход | Цена 1 млн выход | Валюта | Стоимость / обращение, ₽ |",
                "|---|---|---:|---:|---:|---:|---:|---|---:|",
            ]
        )
        for st in result.steps:
            lines.append(
                f"| {st.step} | `{st.model}` | {_fmt_dec(st.calls_per_request, '0.0001')} | "
                f"{_fmt_dec(st.avg_tokens_in, '0.01')} | {_fmt_dec(st.avg_tokens_out, '0.01')} | "
                f"{st.price_in_per_1m} | {st.price_out_per_1m} | {st.currency} | "
                f"{_fmt_dec(st.cost_per_request_rub, '0.0001')} |"
            )
    else:
        lines.append("_Нет замеров LLM-вызовов._")

    lines.extend(
        [
            "",
            "## Разбивка по категориям обращений (архив)",
            "",
            "| Категория | В архиве | Доля архива, % | Замерено диалогов | Статус | Ср. переменная стоимость, ₽ |",
            "|---|---:|---:|---:|---|---:|",
        ]
    )
    for cat in result.categories:
        status_ru = "замерено" if cat.status == "measured" else "**нет замеров**"
        cost_cell = (
            _fmt_dec(cat.avg_variable_cost_rub, "0.0001") if cat.avg_variable_cost_rub is not None else "нет замеров"
        )
        lines.append(
            f"| {cat.name} | {cat.archive_count} | {_fmt_dec(cat.archive_share * HUNDRED, '0.01')}% | "
            f"{cat.measured_dialogs} | {status_ru} | {cost_cell} |"
        )

    lines.extend(
        [
            "",
            "## Источники прайсов и тарифов",
            "",
            "| Сущность | Тариф | Валюта | Дата | Источник |",
            "|---|---|---|---|---|",
        ]
    )
    for m in sorted(pricing.models.values(), key=lambda x: x.model):
        lines.append(
            f"| Модель `{m.model}` | вход {m.input_per_1m} / выход {m.output_per_1m} за 1 млн | "
            f"{m.currency} | {m.date} | {m.source_url} |"
        )
    infra = pricing.infrastructure
    infra_label = f"Инфраструктура ({infra.description})" if infra.description else "Инфраструктура"
    lines.append(
        f"| {infra_label} | {infra.daily_cost} / сут. | {infra.currency} | {infra.date} | {infra.source_url} |"
    )
    op = pricing.operator
    lines.append(
        f"| Оператор поддержки | {op.hourly_rate_rub} ₽/ч, {op.minutes_per_request} мин/обр. | "
        f"RUB | {op.date} | {op.source_url} |"
    )
    lines.append("")
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Воспроизводимый расчёт стоимости запроса (T19).")
    parser.add_argument(
        "--eval-report",
        type=Path,
        action="append",
        default=[],
        help="Путь к JSON-отчёту eval/run_eval.py (можно указывать несколько раз).",
    )
    parser.add_argument(
        "--telemetry",
        type=Path,
        action="append",
        default=[],
        help="Путь к JSON-выгрузке /api/v1/manager/telemetry (можно указывать несколько раз).",
    )
    parser.add_argument(
        "--split-summary",
        type=Path,
        required=True,
        help="Путь к eval/split_summary.json с долями 9 категорий.",
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=DEFAULT_PRICING_PATH,
        help="Путь к config/pricing.yaml (без значений по умолчанию).",
    )
    parser.add_argument(
        "--category-map",
        type=Path,
        default=None,
        help="Необязательный JSON/YAML файл сопоставления session_id/case_id -> категория.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Путь к шаблону организаторов .xlsx (курс ЦБ берётся из шаблона и не меняется).",
    )
    parser.add_argument(
        "--xlsx-out",
        type=Path,
        default=None,
        help="Путь для сохранения заполненного .xlsx (обязателен при --template).",
    )
    parser.add_argument(
        "--sheet",
        type=str,
        default=None,
        help="Имя листа в шаблоне .xlsx (если не указано, ищется автоматически).",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=DEFAULT_REPORT_OUT,
        help="Путь для Markdown-отчёта (по умолчанию docs/cost_report.md).",
    )
    parser.add_argument(
        "--usd-rub-rate",
        type=str,
        default=None,
        help="Курс USD/RUB (используется без --template либо для заполнения пустой ячейки I3 в шаблоне).",
    )
    parser.add_argument(
        "--allow-incomplete-telemetry",
        action="store_true",
        help="Разрешить расчёт по неполному окну /manager/telemetry (recent_calls >= 20) с явным предупреждением.",
    )
    parser.add_argument(
        "--requests-per-period",
        type=int,
        default=DEFAULT_REQUESTS_PER_PERIOD,
        help="Базовый объём обращений за весь расчётный период (по умолчанию 500 за полгода, подтверждено заказчиком).",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=DEFAULT_PERIOD_DAYS,
        help="Длительность расчётного периода в днях (по умолчанию 180 — полгода).",
    )
    parser.add_argument(
        "--load-requests-per-day",
        type=int,
        default=DEFAULT_LOAD_REQUESTS_PER_DAY,
        help="Отдельный нагрузочный сценарий со слайда 2: обращений в сутки (по умолчанию 500; не базовый прогноз).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        pricing = load_pricing_config(args.pricing)
        split_summary = load_split_summary(args.split_summary)
        category_map = load_category_map(args.category_map)
        telemetry_warnings: list[str] = []
        dialogs = load_measurements(
            eval_report_paths=args.eval_report,
            telemetry_paths=args.telemetry,
            split_summary=split_summary,
            category_map=category_map,
            allow_incomplete_telemetry=args.allow_incomplete_telemetry,
            warnings_out=telemetry_warnings,
        )

        if args.template is not None:
            if args.xlsx_out is None:
                raise UnitEconomicsError("При указании --template необходимо задать --xlsx-out.")
            if args.template.resolve() == args.xlsx_out.resolve():
                raise UnitEconomicsError("Путь --xlsx-out не должен перезаписывать исходный --template.")
            template_rate, _ = read_template_cbr_rate(args.template, sheet_name=args.sheet)
            if template_rate is not None:
                usd_rub_rate = template_rate
            elif args.usd_rub_rate is not None:
                usd_rub_rate = _parse_decimal(args.usd_rub_rate, "--usd-rub-rate", allow_zero=False)
            else:
                usd_rub_rate = None
        elif args.usd_rub_rate is not None:
            usd_rub_rate = _parse_decimal(args.usd_rub_rate, "--usd-rub-rate", allow_zero=False)
        else:
            usd_rub_rate = None

        result = compute_unit_economics(
            dialogs,
            split_summary,
            pricing,
            usd_rub_rate=usd_rub_rate,
            requests_per_period=args.requests_per_period,
            period_days=args.period_days,
            load_requests_per_day=args.load_requests_per_day,
            telemetry_warnings=tuple(telemetry_warnings),
        )

        if args.template is not None and args.xlsx_out is not None:
            fill_xlsx_template(
                args.template,
                args.xlsx_out,
                result,
                pricing,
                sheet_name=args.sheet,
            )

        report_md = render_markdown_report(result, pricing)
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(report_md, encoding="utf-8")
    except UnitEconomicsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(f"Отчёт сохранён: {args.report_out}")
    if args.xlsx_out is not None:
        print(f"Шаблон заполнен: {args.xlsx_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
