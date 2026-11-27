import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import get_settings
from app.contracts.schemas import LLMCall
from app.core import llm
from app.core.llm import BudgetExceededError, LLMError, compute_cost_rub, llm_call
from app.db import repository
from app.db.database import close_db, get_db, init_db

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path):
    await init_db(tmp_path / "test.db")
    yield get_db()
    llm.set_http_client(None)
    await close_db()


def provider(handler):
    """Подменяет HTTP-клиент llm.py и выключает mock-режим."""
    get_settings().mock_llm = False
    get_settings().llm_api_key = "sk-test"
    llm.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok_response(tokens_in=1000, tokens_out=500, content="Привет"):
    return httpx.Response(
        200,
        json={
            "model": "x",
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        },
    )


async def test_cost_formula_uses_usd_prices_and_cbr_rate():
    s = get_settings()
    # smart: 2 $ за 1M input, 8 $ за 1M output, курс 82.1664
    assert compute_cost_rub("smart", 1_000_000, 0) == pytest.approx(2 * 82.1664)
    assert compute_cost_rub("smart", 0, 1_000_000) == pytest.approx(8 * 82.1664)
    assert compute_cost_rub("fast", 1000, 500) == pytest.approx((1000 * 0.4 + 500 * 1.6) / 1e6 * s.usd_rub_rate)


async def test_llm_call_logs_tokens_cost_and_latency(db):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return ok_response(tokens_in=1200, tokens_out=300)

    provider(handler)
    resp = await llm_call([{"role": "user", "content": "hi"}], step="agent_loop", session_id="s_1")

    assert resp.content == "Привет"
    assert (resp.tokens_in, resp.tokens_out) == (1200, 300)
    assert resp.cost_rub == pytest.approx(compute_cost_rub("smart", 1200, 300))
    assert seen["auth"] == "Bearer sk-test"
    assert '"model":"gpt-4.1"' in seen["body"].replace(" ", "")

    [call] = await repository.recent_llm_calls()
    assert (call.step, call.model, call.session_id) == ("agent_loop", "gpt-4.1", "s_1")
    assert (call.tokens_in, call.tokens_out) == (1200, 300)
    assert call.cost_rub == pytest.approx(resp.cost_rub)
    assert call.latency_ms >= 0
    assert await llm.today_spend_rub() == pytest.approx(resp.cost_rub)


async def test_fast_profile_uses_fast_model_and_prices(db):
    provider(lambda r: ok_response(1000, 1000))
    resp = await llm_call([{"role": "user", "content": "x"}], step="kb_patch", profile="fast")
    assert resp.model == "gpt-4.1-mini"
    assert resp.cost_rub == pytest.approx(compute_cost_rub("fast", 1000, 1000))


async def test_tool_calls_are_passed_through(db):
    tool_call = {"id": "c1", "type": "function", "function": {"name": "search_catalog", "arguments": "{}"}}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    provider(handler)
    resp = await llm_call([{"role": "user", "content": "x"}], step="agent_loop", tools=[{"type": "function"}])
    assert resp.tool_calls == [tool_call]
    assert resp.finish_reason == "tool_calls"
    assert resp.to_step_trace().result_summary == "tool_calls: search_catalog"


async def test_budget_exceeded_blocks_request(db):
    calls = []
    provider(lambda r: calls.append(r) or ok_response())
    await repository.log_llm_call(
        LLMCall(
            ts=datetime.now(UTC),
            session_id=None,
            step="x",
            model="m",
            tokens_in=1,
            tokens_out=1,
            cost_rub=100.0,
            latency_ms=1,
        )
    )
    with pytest.raises(BudgetExceededError):
        await llm_call([{"role": "user", "content": "x"}], step="agent_loop")
    assert calls == []


async def test_yesterday_spend_does_not_count(db):
    provider(lambda r: ok_response())
    yesterday = llm.today_start() - timedelta(minutes=1)
    await repository.log_llm_call(
        LLMCall(
            ts=yesterday, session_id=None, step="x", model="m", tokens_in=1, tokens_out=1, cost_rub=1000.0, latency_ms=1
        )
    )
    assert await llm.today_spend_rub() == 0
    await llm_call([{"role": "user", "content": "x"}], step="agent_loop")


async def test_retry_on_503_then_success(db, monkeypatch):
    monkeypatch.setattr(llm.asyncio, "sleep", lambda s: _noop())
    responses = iter([httpx.Response(503, text="busy"), ok_response()])
    provider(lambda r: next(responses))
    resp = await llm_call([{"role": "user", "content": "x"}], step="agent_loop")
    assert resp.content == "Привет"


async def test_client_error_is_not_logged(db):
    provider(lambda r: httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError):
        await llm_call([{"role": "user", "content": "x"}], step="agent_loop")
    assert await repository.recent_llm_calls() == []


async def test_cancelled_provider_wait_reports_phase_without_replaying_or_logging_content(db, caplog):
    waiting = asyncio.Event()

    async def hang(request):
        trace = request.extensions["trace"]
        await trace("http11.send_request_body.complete", {"body": "private-prompt"})
        await trace("http11.receive_response_headers.started", {"headers": "private-headers"})
        waiting.set()
        await asyncio.Future()

    provider(hang)
    caplog.set_level(logging.INFO, logger=llm.__name__)
    task = asyncio.create_task(
        llm_call([{"role": "user", "content": "private-prompt"}], step="support_tools", session_id="c_test")
    )
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    logs = [record.getMessage() for record in caplog.records if record.name == llm.__name__]
    assert len(logs) == 1
    assert all(
        token in logs[0]
        for token in (
            "session=c_test",
            "step=support_tools",
            "attempt=1",
            "outcome=cancelled",
            "phase=receive_response_headers",
            "send_request_body.complete",
        )
    )
    assert not any(token in logs[0] for token in ("private-prompt", "private-headers", "sk-test"))
    assert await repository.recent_llm_calls() == []


async def test_no_second_inference_after_read_timeout(db, caplog):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timed out")

    provider(fail)
    caplog.set_level(logging.INFO, logger=llm.__name__)
    with pytest.raises(LLMError):
        await llm_call([], step="support_tools")
    assert len(calls) == 1
    assert "outcome=ReadTimeout" in caplog.text


async def test_go_reconnects_before_request_submission_and_counts_one_completion(db, monkeypatch, caplog):
    settings = get_settings()
    settings.llm_base_url = "https://opencode.ai/inference/go/openai/v1/"
    assert llm._get_client().timeout.connect == min(2.5, settings.llm_timeout_s)
    await llm.close_http_client()
    attempts = 0

    def sometimes_reachable(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectTimeout("unreachable")
        return ok_response()

    provider(sometimes_reachable)
    caplog.set_level(logging.INFO, logger=llm.__name__)
    monkeypatch.setattr(llm.asyncio, "sleep", lambda _: _noop())
    await llm_call([], step="support_tools")
    assert attempts == 3
    assert len(await repository.recent_llm_calls()) == 1
    records = [rec.getMessage() for rec in caplog.records if rec.name == llm.__name__]
    assert len({rec.split(" call=")[1].split()[0] for rec in records}) == 1
    assert "attempt=3 outcome=http_200" in records[-1]


async def test_mock_mode_logs_without_network(db):
    llm.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("network"))))
    resp = await llm_call([{"role": "user", "content": "Сколько стоит MDaemon?"}], step="agent_stub")
    assert "MDaemon" in resp.content
    assert resp.model == "mock/gpt-4.1"
    assert resp.tokens_in > 0 and resp.cost_rub > 0
    [call] = await repository.recent_llm_calls()
    assert call.model == "mock/gpt-4.1"


async def _noop():
    return None
