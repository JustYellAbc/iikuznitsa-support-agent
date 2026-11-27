"""Единственная точка вызова LLM в проекте: llm_call().

Каждый вызов проверяет дневной бюджет, пишет в llm_calls модель, шаг, токены,
задержку и стоимость в рублях. Прямые вызовы SDK провайдера в обход — запрещены.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings, get_settings
from app.contracts.schemas import LLMCall, StepTrace
from app.db import repository

Profile = Literal["smart", "fast"]

_RETRY_STATUSES = {429, 500, 502, 503, 504}
log = logging.getLogger(__name__)
_PHASES = {
    "connect_tcp",
    "start_tls",
    "send_request_headers",
    "send_request_body",
    "receive_response_headers",
    "receive_response_body",
}


class BudgetExceededError(RuntimeError):
    """Дневной лимит расходов исчерпан. Агент передаёт диалог менеджеру, а не отвечает 500."""


class LLMError(RuntimeError):
    """Провайдер вернул ошибку или некорректный ответ."""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict[str, Any]]
    message: dict[str, Any]  # assistant-сообщение как есть — его можно вернуть в messages следующего вызова
    finish_reason: str | None
    model: str
    step: str
    tokens_in: int
    tokens_out: int
    cost_rub: float
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_step_trace(self, result_summary: str | None = None) -> StepTrace:
        return StepTrace(
            tool=f"llm:{self.step}",
            args={"model": self.model},
            result_summary=result_summary or _summary(self),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost_rub=self.cost_rub,
            latency_ms=self.latency_ms,
        )


def _summary(resp: LLMResponse) -> str:
    if resp.tool_calls:
        return "tool_calls: " + ", ".join(tc.get("function", {}).get("name", "?") for tc in resp.tool_calls)
    return (resp.content or "")[:200]


def model_for(profile: Profile, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return s.model_smart if profile == "smart" else s.model_fast


def compute_cost_rub(profile: Profile, tokens_in: int, tokens_out: int, settings: Settings | None = None) -> float:
    s = settings or get_settings()
    if profile == "smart":
        price_in, price_out = s.model_smart_price_in_usd, s.model_smart_price_out_usd
    else:
        price_in, price_out = s.model_fast_price_in_usd, s.model_fast_price_out_usd
    usd = tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out
    return round(usd * s.usd_rub_rate, 8)


def today_start(settings: Settings | None = None) -> datetime:
    """Начало текущих суток в BUDGET_TZ (по умолчанию Москва), в UTC."""
    tz = ZoneInfo((settings or get_settings()).budget_tz)
    local_midnight = datetime.combine(datetime.now(tz).date(), dtime.min, tzinfo=tz)
    return local_midnight.astimezone(UTC)


async def today_spend_rub() -> float:
    return await repository.spend_since(today_start())


async def check_budget() -> float:
    """Бросает BudgetExceededError, если потолок достигнут; иначе возвращает остаток в рублях."""
    budget = get_settings().daily_budget_rub
    spent = await today_spend_rub()
    if spent >= budget:
        raise BudgetExceededError(f"Дневной бюджет исчерпан: {spent:.2f} из {budget:.2f} ₽")
    return budget - spent


# --- HTTP-клиент ----------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(proxy=s.llm_proxy_url or None, timeout=s.llm_timeout_s)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Подмена клиента в тестах (httpx.MockTransport)."""
    global _client
    _client = client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class _AttemptTiming:
    def __init__(self):
        self.started = time.monotonic()
        self.phase = "dispatch"
        self.marks = {}

    def elapsed(self):
        return round((time.monotonic() - self.started) * 1000)

    async def trace(self, event, _info):
        # httpcore info may contain headers and exceptions. Persist only allowed event timings.
        parts = event.split(".")
        if len(parts) != 3:
            return
        _, phase, state = parts
        if phase in _PHASES and state in {"started", "complete", "failed"}:
            self.phase = phase
            self.marks[f"{phase}.{state}"] = self.elapsed()


async def _post_chat(payload: dict[str, Any], session_id: str | None = None, *, step: str = "") -> dict[str, Any]:
    s = get_settings()
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {}
    if s.llm_base_url.rstrip("/") == "https://opencode.ai/inference/go/openai/v1":
        headers["User-Agent"] = "SoftlineSupportLab/0.1"
        if session_id:
            headers["x-opencode-session"] = session_id
    last_error: Exception | None = None
    call_id = uuid4().hex[:12]
    for attempt in range(2):
        timing = _AttemptTiming()
        outcome = "error"
        try:
            resp = await _get_client().post(url, json=payload, headers=headers, extensions={"trace": timing.trace})
            outcome = f"http_{resp.status_code}"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except httpx.TransportError as e:
            outcome = type(e).__name__
            last_error = e
            # Once the body may have reached the provider, a second POST can duplicate inference.
            if not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                break
        else:
            if resp.status_code == 200:
                return resp.json()
            last_error = LLMError(f"LLM HTTP {resp.status_code}")
            if resp.status_code not in _RETRY_STATUSES:
                break
        finally:
            log.info(
                "provider_attempt session=%s step=%s call=%s attempt=%d outcome=%s phase=%s elapsed_ms=%d marks_ms=%s",
                session_id,
                step,
                call_id,
                attempt + 1,
                outcome,
                timing.phase,
                timing.elapsed(),
                json.dumps(timing.marks),
            )
        if attempt == 0:
            await asyncio.sleep(1.0)
    raise LLMError(str(last_error)) from last_error


def _mock_completion(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    last_user = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "") or ""
    content = f"[MOCK {model}] Получено: {str(last_user)[:200]}"
    prompt_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, prompt_chars // 4), "completion_tokens": max(1, len(content) // 4)},
    }


# --- Публичный API ----------------------------------------------------------------


async def llm_call(
    messages: list[dict[str, Any]],
    *,
    step: str,
    profile: Profile = "smart",
    session_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMResponse:
    """Chat Completions через OpenAI-совместимый API с учётом стоимости.

    step — имя шага для телеметрии (например "agent_loop", "escalation_summary", "kb_patch").
    Raises BudgetExceededError до запроса, если дневной лимит исчерпан; LLMError при ошибке провайдера.
    """
    s = get_settings()
    await check_budget()
    model = model_for(profile, s)

    payload: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    started = time.perf_counter()
    data = _mock_completion(messages, model) if s.mock_llm else await _post_chat(payload, session_id, step=step)
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("Unexpected LLM response structure") from e
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    cost_rub = compute_cost_rub(profile, tokens_in, tokens_out, s)
    logged_model = f"mock/{model}" if s.mock_llm else model

    await repository.log_llm_call(
        LLMCall(
            ts=datetime.now(UTC),
            session_id=session_id,
            step=step,
            model=logged_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_rub=cost_rub,
            latency_ms=latency_ms,
        )
    )
    return LLMResponse(
        content=message.get("content"),
        tool_calls=message.get("tool_calls") or [],
        message=message,
        finish_reason=choice.get("finish_reason"),
        model=logged_model,
        step=step,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_rub=cost_rub,
        latency_ms=latency_ms,
        raw=data,
    )
