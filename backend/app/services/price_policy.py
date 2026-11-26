"""Политика состояния цены и доступности оффера по снимку фида (T14). Только детерминированные правила.

Три состояния снимка (priceState контракта T04):
- listed — available=true и цена > 0: цену снимка можно назвать;
- on_request — цена 0, пустая или нечисловая: 0 ₽ не показываем, цену запрашиваем у менеджера;
- availability_check — available=false и цена > 0: правила в файле политики до Q&A с заказчиком
  ($DATA_DIR/config/price_policy.json, если он есть, иначе встроенный price_policy.json).

Флаги allow_* разрешают действие только со стороны состояния цены. Условия документа и согласие
покупателя проверяет коммерческий контур отдельно.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.contracts.t04 import HandoffReason, PriceState, Product, rub_to_kopecks

# Встроенная политика; на стенде её переопределяет $DATA_DIR/config/price_policy.json без пересборки образа
POLICY_PATH = Path(__file__).with_name("price_policy.json")
OVERRIDE_PATH = Path("config") / "price_policy.json"
# Значения «Срок действия», которые срок не подтверждают (тот же набор служебных исключений, что в catalog/index.py)
UNDEFINED_TERMS = frozenset(
    {
        "",
        "не определено",
        "мес.",
        "другое или отсутствует",
        "не указано",
        "нет данных",
    }
)
# Валюты, для которых разрешён рублёвый расчёт (совпадает с T07)
SUPPORTED_CURRENCIES = frozenset({"RUB", "RUR"})
PRICE_STATES: tuple[PriceState, ...] = ("listed", "availability_check", "on_request")
# Состояния индекса T06 (catalog/index.py) -> priceState T04
T06_PRICE_STATES: dict[str, PriceState] = {
    "priced_available": "listed",
    "verify_availability": "availability_check",
    "request_price": "on_request",
}


@dataclass(frozen=True)
class AvailabilityCheckRule:
    """Что разрешено при available=false и цене > 0. Меняется в файле политики вместе с policy_source."""

    show_snapshot_price_with_caveat: bool
    allow_quote_draft: bool
    allow_auto_invoice: bool
    confirmed: bool
    policy_source: str


class PolicyError(ValueError):
    """Файл политики не читается или не проходит проверку."""


def load_availability_rule(path: Path = POLICY_PATH) -> AvailabilityCheckRule:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{path}: не читается как JSON: {exc}") from exc
    data = data.get("availability_check") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: нет объекта availability_check")
    names = {f.name for f in fields(AvailabilityCheckRule)}
    if set(data) != names:
        raise PolicyError(f"{path}: availability_check ожидает ключи {sorted(names)}")
    if not all(isinstance(data[name], bool) for name in names - {"policy_source"}):
        raise PolicyError(f"{path}: флаги availability_check должны быть true/false")
    if not isinstance(data["policy_source"], str) or not data["policy_source"].strip():
        raise PolicyError(f"{path}: правило без policy_source")
    return AvailabilityCheckRule(**data)


@dataclass(frozen=True)
class ActivePolicy:
    rule: AvailabilityCheckRule
    path: Path
    overridden: bool  # True — взят файл из $DATA_DIR, а не встроенный


_active: ActivePolicy | None = None


def activate(data_dir: Path | None = None) -> ActivePolicy:
    """Перечитать политику при старте бэкенда. Битый файл переопределения — ошибка, а не откат на встроенный."""
    global _active
    override = (data_dir or get_settings().data_dir) / OVERRIDE_PATH
    path = override if override.exists() else POLICY_PATH
    _active = ActivePolicy(load_availability_rule(path), path, path != POLICY_PATH)
    return _active


def active_policy() -> ActivePolicy:
    return _active or activate()


def reset() -> None:
    global _active
    _active = None


@dataclass(frozen=True)
class OfferSnapshot:
    """Исходные поля оффера из снимка фида, без интерпретации."""

    offer_id: str
    name: str
    url: str
    available: bool | None  # None — атрибута available в фиде нет
    price: str | None  # строка из XML как есть
    currency: str | None
    license_terms: tuple[str, ...]  # все значения param «Срок действия» в порядке фида
    snapshot_date: str | None  # yml_catalog/@date


@dataclass(frozen=True)
class OfferAssessment:
    """Вычисленный статус пригодности поверх исходного снимка."""

    snapshot: OfferSnapshot
    price_state: PriceState
    unit_price_kopecks: int | None  # цена снимка за упаковку; None для on_request
    show_price: bool  # можно ли назвать цену покупателю
    price_caveat: str | None  # оговорка, с которой называется цена
    license_term: str | None  # None — срок снимком не подтверждён, не выдумываем
    allow_quote_draft: bool
    allow_auto_invoice: bool
    handoff_reason: HandoffReason | None  # что передать менеджеру, если покупатель идёт дальше
    policy_source: str

    def to_product(self) -> Product:
        s = self.snapshot
        # По контракту T04 скрытая цена передаётся как unitPriceKopecks=None (priceState=on_request).
        price = s.price if self.show_price and self.unit_price_kopecks is not None else None
        return Product.from_catalog(s.offer_id, s.name, s.url, bool(s.available), price, self.license_term)


def is_supported_currency(currency: str | None) -> bool:
    """Поддерживается ли валюта снимка для рублёвого расчёта (только RUB / RUR)."""
    return bool(currency and currency.strip().upper() in SUPPORTED_CURRENCIES)


def snapshot_from_row(row: Mapping[str, Any], snapshot_date: str | None = None) -> OfferSnapshot:
    """Собрать OfferSnapshot из строки таблицы offers (T06) с сохранением сырых параметров."""
    keys = set(row.keys())
    params = row["params"] if "params" in keys else {}
    if isinstance(params, str):
        params = json.loads(params)
    raw_terms: tuple[str, ...] = ()
    if "details" in keys and row["details"]:
        details = json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
        raw_params = details.get("raw_params") if isinstance(details, dict) else None
        if isinstance(raw_params, dict) and "Срок действия" in raw_params:
            raw_terms = tuple(str(v) for v in raw_params["Срок действия"])
    if not raw_terms and isinstance(params, dict):
        raw_terms = tuple(str(v) for v in params.get("Срок действия", ()))
    raw_avail = row["available"]
    available = None if raw_avail is None else bool(raw_avail)
    raw_currency = row["currency"] if "currency" in keys else None
    return OfferSnapshot(
        offer_id=str(row["id"]),
        name=str(row["name"]),
        url=str(row["source"]),
        available=available,
        price=None if row["price"] is None else str(row["price"]),
        currency=str(raw_currency) if raw_currency else None,
        license_terms=raw_terms,
        snapshot_date=snapshot_date,
    )


def price_kopecks(price: str | None) -> int | None:
    """Цена снимка в копейках; ноль, пустое и нечисловое значение -> None."""
    try:
        return rub_to_kopecks(price)
    except (InvalidOperation, ValueError):
        return None


def classify(available: bool | None, price: str | None) -> PriceState:
    """То же правило, что Product.from_catalog: без цены -> on_request, иначе по available."""
    if price_kopecks(price) is None:
        return "on_request"
    return "listed" if available else "availability_check"


def confirmed_term(values: tuple[str, ...]) -> str | None:
    """Срок из снимка, только если он единственный и определён (исключая служебные заглушки T06)."""
    terms = {v.strip() for v in values if v.strip().casefold() not in UNDEFINED_TERMS}
    return terms.pop() if len(terms) == 1 else None


def assess(snapshot: OfferSnapshot, rule: AvailabilityCheckRule | None = None) -> OfferAssessment:
    rule = rule or active_policy().rule
    term = confirmed_term(snapshot.license_terms)
    if not is_supported_currency(snapshot.currency):
        return OfferAssessment(
            snapshot=snapshot,
            price_state="on_request",
            unit_price_kopecks=None,
            show_price=False,
            price_caveat=None,
            license_term=term,
            allow_quote_draft=False,
            allow_auto_invoice=False,
            handoff_reason="price_request",
            policy_source="contracts/t04/openapi.yaml: currency must be RUB/RUR for ruble pricing",
        )
    state = classify(snapshot.available, snapshot.price)
    kopecks = price_kopecks(snapshot.price)
    date = f" от {snapshot.snapshot_date}" if snapshot.snapshot_date else ""
    common = {"snapshot": snapshot, "price_state": state, "license_term": term}
    if state == "listed":
        return OfferAssessment(
            **common,
            unit_price_kopecks=kopecks,
            show_price=True,
            price_caveat=None,
            allow_quote_draft=True,
            allow_auto_invoice=True,
            handoff_reason=None,
            policy_source="contracts/t04/openapi.yaml: Product.priceState=listed",
        )
    if state == "on_request":
        return OfferAssessment(
            **common,
            unit_price_kopecks=None,
            show_price=False,
            price_caveat=None,
            allow_quote_draft=False,
            allow_auto_invoice=False,
            handoff_reason="price_request",
            policy_source="contracts/t04/openapi.yaml: Product.priceState=on_request",
        )
    return OfferAssessment(
        **common,
        unit_price_kopecks=kopecks,
        show_price=rule.show_snapshot_price_with_caveat,
        price_caveat=(
            f"Цена по снимку каталога{date}. Наличие и возможность поставки подтвердит менеджер."
            if rule.show_snapshot_price_with_caveat
            else None
        ),
        allow_quote_draft=rule.allow_quote_draft,
        allow_auto_invoice=rule.allow_auto_invoice,
        handoff_reason="availability",
        policy_source=rule.policy_source,
    )
