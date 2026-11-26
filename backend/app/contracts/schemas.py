"""Внутренние контракты команды: стык с агентом, трейс, FinOps и расширения T05.

Публичный API покупателя и оператора описан в contracts/t04.py (контракт T04).
Изменения здесь — только по согласованию команды и с зелёным `pytest tests/test_contracts.py`.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.t04 import HandoffReason, Product, Source

Mode = Literal["autopilot", "pilot"]
Role = Literal["client", "agent", "operator"]
Decision = Literal["answer", "draft", "escalate"]
PatchStatus = Literal["proposed", "approved", "rejected"]
DraftStatus = Literal["pending", "sent"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Инструменты агента -----------------------------------------------------


class ToolResult(Strict):
    """Результат любого инструмента. Факт без source_ref агент клиенту не выдаёт."""

    ok: bool
    data: Any = None
    source_ref: str | None = None
    error: str | None = None


# --- Диалог глазами агента ---------------------------------------------------------


class Session(Strict):
    """Диалог (conversationId в T04) с режимом обработки ответов агента."""

    id: str
    client_context: dict[str, Any] = {}
    mode: Mode = "autopilot"
    created_at: datetime


class Message(Strict):
    """Реплика из журнала в истории, которую получает агент."""

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


class HandoffRequest(Strict):
    """Что агент передаёт оператору. products — только контекст покупателя."""

    reason: HandoffReason
    question: str = Field(min_length=1, max_length=8000)
    products: list[Product] = []


class AgentReply(Strict):
    """Что agent/runner.py возвращает каркасу после одного хода.

    Каркас пишет в журнал по порядку: products (если есть) -> message (если text) -> handoff (если есть).

    decision="answer"   — ответ клиенту; в pilot-режиме каркас превращает его в черновик менеджеру;
    decision="draft"    — ответ сразу уходит менеджеру черновиком;
    decision="escalate" — обязателен handoff; text (если есть) уходит клиенту до карточки обращения.
    """

    turn: AgentTurn
    text: str = ""
    purpose: Literal["answer", "clarification", "notice"] = "answer"
    sources: list[Source] = []
    products: list[Product] = []
    handoff: HandoffRequest | None = None


# --- База знаний и черновики -------------------------------------------------


class KBPatch(Strict):
    id: str
    handoff_id: str | None = None
    question: str
    answer: str
    scope: str = "general"
    status: PatchStatus
    approved_by: str | None = None
    created_at: datetime


class KBPatchCreate(Strict):
    question: str = Field(min_length=1, max_length=8000)
    answer: str = Field(min_length=1, max_length=8000)
    scope: str = "general"
    handoff_id: str | None = None


class Draft(Strict):
    """Ответ агента в режиме pilot: события журнала ждут отправки менеджером."""

    id: str
    conversation_id: str
    action_id: str
    events: list[dict[str, Any]]
    status: DraftStatus
    created_at: datetime


class DraftSend(Strict):
    text: str | None = Field(default=None, min_length=1, max_length=8000, description="Заменить текст ответа")


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


# --- HTTP: служебные маршруты T05 -------------------------------------------------


class Health(Strict):
    status: Literal["ok"]
    offers_count: int
    kb_count: int
    today_spend_rub: float


class ManagerLogin(Strict):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ModeUpdate(Strict):
    mode: Mode
    conversation_id: str | None = Field(default=None, description="Пусто — режим по умолчанию для новых диалогов")


class ModeState(Strict):
    default_mode: Mode
    conversation: Session | None = None


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
