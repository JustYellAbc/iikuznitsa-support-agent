"""Build a commercial proposal from one live T06 snapshot and verified commercial conditions."""

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.needs_catalog import allowed_quantity
from app.contracts.t04 import CommercialTerms, Id, Product, Quote, QuoteLine
from app.core.errors import invalid_input
from app.services.catalog_snapshot import open_index
from app.services.price_policy import UNDEFINED_TERMS, assess, snapshot_from_row


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Customer(Inputs):
    company: Annotated[str, StringConstraints(min_length=1, max_length=180)]
    inn: Annotated[str, StringConstraints(pattern=r"^[0-9]{10}$")]
    kpp: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}[0-9A-Z]{2}[0-9]{3}$")]


class Item(Inputs):
    offerId: Id
    packages: Annotated[int, Field(strict=True, ge=1, le=100000)]


class ProposalRequest(Inputs):
    customer: Customer
    items: Annotated[list[Item], Field(min_length=1, max_length=20)]
    baseRevision: Annotated[int, Field(strict=True, ge=0)]


class Conditions(Inputs):
    """Private, operator-verified values for this SKU. Dates are absolute, prices include tax."""

    source: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    article: str | None = None
    payment: str | None = None
    delivery: str | None = None
    validUntil: date | None = None
    vatRate: Annotated[Decimal, Field(ge=0, le=100)] | None = None


class Detail(Inputs):
    product: Product
    article: str | None
    feedDate: str
    packages: int
    unit: int | None
    reason: str | None
    conditions: Conditions | None
    decision: str | None = None


class Snapshot(Inputs):
    customer: Customer
    quote: Quote
    details: list[Detail]
    dated: date


def clean(value: str | None) -> str | None:
    return value.strip() if value and value.strip().casefold() not in UNDEFINED_TERMS else None


def amount_vat(line: Detail) -> int | None:
    rate = line.conditions.vatRate if line.conditions else None
    if line.unit is None or rate is None:
        return None
    return int((line.unit * line.packages * rate / (100 + rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def catalog_lines(index: Path, items: list[Item], terms_file: Path) -> list[Detail]:
    if not index.is_file():
        raise invalid_input("Каталог временно недоступен. Попробуйте позже.")
    rules = json.loads(terms_file.read_text(encoding="utf-8")) if terms_file.is_file() else {}
    details = []
    with open_index(index) as db:
        feed_date = db.execute("SELECT snapshot FROM metadata").fetchone()[0]
        for item in items:
            row = db.execute("SELECT * FROM offers WHERE id=?", (item.offerId,)).fetchone()
            if row is None or not row["source"].startswith("https://"):
                raise invalid_input(f"Позиция {item.offerId} больше не доступна в каталоге. Обновите подбор.")
            sale = assess(snapshot_from_row(row, feed_date))
            product = sale.to_product()
            raw = json.loads(row["details"])
            notes = raw.get("sales_notes", "")
            if notes and not allowed_quantity(notes, item.packages):
                raise invalid_input(f"Для {item.offerId} проверьте количество упаковок. Условие каталога: {notes}")
            rule = Conditions.model_validate(rules[item.offerId]) if item.offerId in rules else None
            if rule and rule.validUntil and rule.validUntil < date.today():
                rule = None
            details.append(
                Detail(
                    product=product,
                    packages=item.packages,
                    feedDate=feed_date,
                    article=clean(row["vendor_code"]) or (clean(rule.article) if rule else None),
                    unit=product.unitPriceKopecks if sale.allow_quote_draft and sale.show_price else None,
                    reason=sale.handoff_reason,
                    conditions=rule,
                )
            )
    return details


def calculate(customer: Customer, details: list[Detail], quote_id: str, revision: int) -> Snapshot:
    def common(field: str) -> str | None:
        values = {getattr(d.conditions, field) if d.conditions else None for d in details}
        return clean(values.pop()) if len(values) == 1 else None

    complete_vat = all(amount_vat(d) is not None for d in details)
    validity = [d.conditions.validUntil if d.conditions else None for d in details]
    terms = CommercialTerms(
        payment=common("payment"),
        delivery=common("delivery"),
        validUntil=min(validity).isoformat() if all(validity) else None,
        taxNote="НДС учтён в конечных ценах, разбивка приведена в КП." if complete_vat else None,
    )
    ready = all(terms.model_dump().values()) and all(d.article and d.unit and not d.reason for d in details)
    lines = [
        QuoteLine(
            offerId=d.product.offerId,
            name=d.product.name,
            source=d.product.source,
            packages=d.packages,
            unitPriceKopecks=d.unit,
        )
        for d in details
    ]
    subtotal = sum((line.unitPriceKopecks or 0) * line.packages for line in lines)
    quote = Quote(
        quoteId=quote_id,
        revision=revision,
        state="ready" if ready else "needs_review",
        lines=lines,
        knownSubtotalKopecks=subtotal,
        totalKopecks=subtotal if ready else None,
        terms=terms,
    )
    return Snapshot(customer=customer, details=details, quote=quote, dated=date.today())
