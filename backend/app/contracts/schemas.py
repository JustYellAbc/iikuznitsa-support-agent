"""Контракты данных команды — источник правды для OpenAPI.

ЗАМОРОЖЕНО после Фазы 1. Любое изменение — только по согласованию команды
и с зелёным `pytest tests/test_contracts.py`.

Деньги (Decimal) в JSON сериализуются строкой с двумя знаками: "77556.00".
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["autopilot", "pilot"]
Role = Literal["client", "agent", "operator"]
Decision = Literal["answer", "draft", "escalate"]
TicketType = Literal["draft", "escalation"]
TicketStatus = Literal["open", "in_progress", "resolved"]
PatchStatus = Literal["proposed", "approved", "rejected"]
QuoteStatus = Literal["draft", "approved"]
SseEventType = Literal["step_trace", "message_done", "ticket_created", "operator_reply"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Инструменты агента -----------------------------------------------------


class ToolResult(Strict):
    """Результат любого инструмента. Факт без source_ref агент клиенту не выдаёт."""

    ok: bool
    data: Any = None
    source_ref: str | None = None
    error: str | None = None


# --- Сессии и сообщения -----------------------------------------------------


class SessionCreate(Strict):
    client_context: dict[str, Any] = {}


class Session(Strict):
    id: str
    client_context: dict[str, Any] = {}
    mode: Mode = "autopilot"
    created_at: datetime


class MessageCreate(Strict):
    text: str = Field(min_length=1, max_length=8000)


class Message(Strict):
    id: str
    session_id: str
    role: Role
    text: str
    sources: list[str] = []
    quote_id: str | None = None
    created_at: datetime


# --- Трейс агента -----------------------------------------------------------


class StepTrace(Strict):
    tool: str
    args: dict[str, Any]
    result_summary: str
    source_ref: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    latency_ms: int = 0


class AgentTurn(Strict):
    turn_id: str
    session_id: str
    steps: list[StepTrace]
    decision: Decision
    escalation_reason: str | None = None
    total_cost_rub: float
    total_latency_ms: int


class AgentReply(Strict):
    """Что agent/runner.py возвращает каркасу после одного хода.

    decision="answer"   — text уходит клиенту (в pilot-режиме каркас сам превращает его в черновик);
    decision="draft"    — text становится draft_answer тикета для менеджера;
    decision="escalate" — создаётся тикет-эскалация; text (если есть) уходит клиенту как уведомление.
    """

    turn: AgentTurn
    text: str = ""
    sources: list[str] = []
    quote_id: str | None = None
    ticket_summary: str | None = None
    collected_params: dict[str, Any] = {}


# --- Тикеты и база знаний ---------------------------------------------------


class Ticket(Strict):
    id: str
    session_id: str
    type: TicketType
    status: TicketStatus
    reason: str
    summary: str
    collected_params: dict[str, Any] = {}
    draft_answer: str | None = None
    operator_answer: str | None = None
    suggested_patch_id: str | None = None
    created_at: datetime


class KBPatch(Strict):
    id: str
    ticket_id: str | None = None
    question: str
    answer: str
    scope: str = "general"
    status: PatchStatus
    approved_by: str | None = None
    created_at: datetime


# --- FinOps -----------------------------------------------------------------


class LLMCall(Strict):
    id: int | None = None
    ts: datetime
    session_id: str | None
    step: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_rub: float
    latency_ms: int


# --- КП ---------------------------------------------------------------------


class QuoteItem(Strict):
    sku: str
    name: str
    qty: int
    term: str | None = None
    unit_price: Decimal
    line_total: Decimal
    vat_amount: Decimal


class Quote(Strict):
    id: str
    session_id: str
    version: int
    customer_name: str
    items: list[QuoteItem]
    total: Decimal
    total_vat: Decimal
    currency: str = "RUB"
    payment_terms: str
    delivery_terms: str
    valid_until: date
    status: QuoteStatus = "draft"
    pdf_url: str


# --- HTTP: ответы и запросы API ---------------------------------------------


class Health(Strict):
    status: Literal["ok"]
    offers_count: int
    kb_count: int
    today_spend_rub: float


class SessionState(Strict):
    session: Session
    messages: list[Message]
    turns: list[AgentTurn]
    open_tickets: list[Ticket]
    busy: bool = Field(description="Агент сейчас обрабатывает ход; новое сообщение получит 409")


class SseEvent(Strict):
    """Формат SSE: `event: <type>` + `data: <json payload>`.

    step_trace     — payload: StepTrace (идёт в реальном времени, пока агент думает)
    message_done   — payload: Message (агента или служебное уведомление)
    ticket_created — payload: Ticket без внутренних полей (summary="", collected_params={}, draft_answer=null)
    operator_reply — payload: Message (role="operator")
    """

    type: SseEventType
    payload: dict[str, Any]


class TicketWithContext(Strict):
    ticket: Ticket
    messages: list[Message]
    turns: list[AgentTurn]


class OperatorReply(Strict):
    text: str | None = Field(default=None, max_length=8000)
    send_draft: bool = Field(default=False, description="Отправить draft_answer тикета как есть")
    propose_patch: bool = Field(default=False, description="Предложить ответ в БЗ (KBPatch status=proposed)")


class ModeUpdate(Strict):
    mode: Mode
    session_id: str | None = Field(default=None, description="Пусто — режим по умолчанию для новых сессий")


class ModeState(Strict):
    default_mode: Mode
    session: Session | None = None


class StepStats(Strict):
    step: str
    calls: int
    avg_tokens_in: float
    avg_tokens_out: float
    avg_cost_rub: float
    total_cost_rub: float
    avg_latency_ms: float


class TelemetrySummary(Strict):
    today_spend_rub: float
    daily_budget_rub: float
    budget_left_rub: float
    usd_rub_rate: float
    steps: list[StepStats]
    recent_calls: list[LLMCall]
