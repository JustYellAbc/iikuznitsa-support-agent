"""Модели публичного API по контракту T04 0.3.0 (contracts/t04/openapi.yaml).

Имена полей повторяют контракт (camelCase). Деньги — целые копейки RUB за упаковку.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

Id = Annotated[str, Field(min_length=1, max_length=128, pattern=ID_PATTERN)]
Text = Annotated[str, Field(min_length=1, max_length=8000)]
Revision = Annotated[int, Field(ge=1)]
Kopecks = Annotated[int, Field(ge=1)]

ErrorCode = Literal[
    "unauthenticated",
    "forbidden",
    "not_found",
    "stale_revision",
    "terms_pending",
    "confirmation_required",
    "key_conflict",
    "already_resolved",
    "invalid_input",
    "rate_limited",
    "agent_busy",
]
HandoffReason = Literal[
    "customer_request",
    "tender",
    "price_request",
    "availability",
    "outside_catalog",
    "special_terms",
    "technical",
    "insufficient_evidence",
]
Author = Literal["customer", "agent", "manager"]
Purpose = Literal["input", "greeting", "clarification", "answer", "notice"]
PriceState = Literal["listed", "availability_check", "on_request"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OmitNone(Contract):
    """Необязательные поля контракта без null: при None ключ не выводится."""

    omit_if_none: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        for name in self.omit_if_none:
            if data.get(name) is None:
                data.pop(name, None)
        return data


class Source(Contract):
    url: Annotated[str, Field(pattern=r"^https://", max_length=2048)]
    label: Text


class ConversationRef(Contract):
    conversationId: Id


class Receipt(Contract):
    conversationId: Id
    actionId: Id


class Error(Contract):
    code: ErrorCode
    message: Text


# --- Команды -------------------------------------------------------------------


class MessageAction(OmitNone):
    omit_if_none: ClassVar[frozenset[str]] = frozenset({"handoffId"})
    type: Literal["message"]
    text: Text
    handoffId: Id | None = None


class RequestOperatorAction(Contract):
    type: Literal["request_operator"]


class ConfirmQuoteAction(Contract):
    type: Literal["confirm_quote"]
    quoteId: Id
    revision: Revision


class RequestDocumentAction(Contract):
    type: Literal["request_document"]
    quoteId: Id
    revision: Revision
    kind: Literal["quote_pdf", "demo_invoice"]


CustomerAction = Annotated[
    MessageAction | RequestOperatorAction | ConfirmQuoteAction | RequestDocumentAction,
    Field(discriminator="type"),
]


class ReplyDecision(Contract):
    type: Literal["reply", "close"]
    reply: Text


class ApproveLinePriceDecision(Contract):
    type: Literal["approve_line_price"]
    quoteId: Id
    revision: Revision
    offerId: Id
    unitPriceKopecks: Kopecks


ManagerDecision = Annotated[ReplyDecision | ApproveLinePriceDecision, Field(discriminator="type")]


# --- Товары и обращения -------------------------------------------------------------


def rub_to_kopecks(price: Decimal | float | str | None) -> int | None:
    """Цена каталога в рублях -> целые копейки; ноль и пустая цена -> None («Запросить цену»)."""
    if price is None or str(price).strip() == "":
        return None
    kopecks = (Decimal(str(price)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(kopecks) if kopecks > 0 else None


class Product(Contract):
    offerId: Id
    name: Text
    source: Source
    available: bool
    priceState: PriceState
    unitPriceKopecks: Kopecks | None
    licenseTerm: Text | None

    @model_validator(mode="after")
    def _price_state_matches(self) -> "Product":
        if self.priceState == "listed":
            ok = self.available and self.unitPriceKopecks is not None
        elif self.priceState == "availability_check":
            ok = not self.available and self.unitPriceKopecks is not None
        else:
            ok = self.unitPriceKopecks is None
        if not ok:
            raise ValueError(f"priceState={self.priceState} conflicts with available/unitPriceKopecks")
        return self

    @classmethod
    def from_catalog(
        cls,
        offer_id: str,
        name: str,
        url: str,
        available: bool,
        price_rub: Decimal | float | str | None,
        license_term: str | None = None,
        label: str = "Карточка Softline",
    ) -> "Product":
        """Единое правило T04: цена 0 -> on_request, недоступно с ценой -> availability_check."""
        kopecks = rub_to_kopecks(price_rub)
        if kopecks is None:
            state: PriceState = "on_request"
        elif available:
            state = "listed"
        else:
            state = "availability_check"
        term = (license_term or "").strip()
        return cls(
            offerId=offer_id,
            name=name,
            source=Source(url=url, label=label),
            available=available,
            priceState=state,
            unitPriceKopecks=kopecks,
            licenseTerm=None if term in ("", "Не определено") else term,
        )


class QuoteLineRef(Contract):
    quoteId: Id
    revision: Revision
    offerId: Id


class Handoff(OmitNone):
    omit_if_none: ClassVar[frozenset[str]] = frozenset({"quoteLine"})
    handoffId: Id
    conversationId: Id
    reason: HandoffReason
    question: Text
    products: list[Product]
    status: Literal["open", "resolved"]
    quoteLine: QuoteLineRef | None = None


class HandoffList(Contract):
    items: list[Handoff]


# --- Журнал --------------------------------------------------------------------------


class MessageData(OmitNone):
    omit_if_none: ClassVar[frozenset[str]] = frozenset({"handoffId"})
    type: Literal["message"] = "message"
    author: Author
    purpose: Purpose
    text: Text
    sources: list[Source]
    handoffId: Id | None = None


class ProductsData(Contract):
    type: Literal["products"] = "products"
    products: Annotated[list[Product], Field(min_length=1)]


class HandoffData(Contract):
    type: Literal["handoff"] = "handoff"
    handoff: Handoff


EventData = Annotated[MessageData | ProductsData | HandoffData, Field(discriminator="type")]


class Event(Contract):
    seq: Annotated[int, Field(ge=1)]
    at: str
    actionId: Id
    data: EventData


class EventPage(Contract):
    events: Annotated[list[Event], Field(max_length=100)]
    nextAfter: Annotated[int, Field(ge=0)]
    hasMore: bool
