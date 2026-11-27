"""Exercise the production buyer handler, tools and journal with a scripted HTTP provider."""

import json

import httpx
import pytest

from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say
from app.agent.test_knowledge import ANSWER, PATCHES, QUESTION, propose, source_case
from app.config import get_settings
from app.core import llm


def tool(name, **arguments):
    return {
        "tool_calls": [
            {
                "id": "test-call",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    }


@pytest.fixture
def provider(monkeypatch):
    responses = []
    requests = []
    monkeypatch.setenv("AGENT_ENGINE", "tools")
    monkeypatch.setenv("MOCK_LLM", "false")
    monkeypatch.setenv("LLM_BASE_URL", "https://test.invalid/v1")
    get_settings.cache_clear()

    def respond(req):
        requests.append(json.loads(req.content))
        message = responses.pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    monkeypatch.setattr(llm, "_client", httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return responses, requests


@pytest.mark.usefixtures("index")
def test_model_uses_tools_and_cannot_reuse_twenty_for_thirty_seven(api, provider):
    responses, requests = provider
    responses.extend(
        [
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="product", refs=["r1"], field="price"),
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="product", refs=["r1"], field="price"),
            {"content": "Рассмотреть другую вместимость?"},
        ]
    )
    cid = api.new_conversation()
    first = say(api, cid, "Подскажите цену по артикулу new20. Почта demo@example.org.")
    assert [p["offerId"] for e in first if e["type"] == "products" for p in e["products"]] == ["new20"]
    assert "1 234,56 ₽" in first[-1]["text"]
    second = say(api, cid, "Теперь нужно 37 мест.")
    assert second[-1]["purpose"] == "clarification"
    assert all(e["type"] != "products" for e in second)
    tender = say(api, cid, "Тендерная закупка.")
    assert tender[-1]["handoff"]["products"] == []
    assert len(requests) == 5 and not responses
    sent = json.dumps(requests, ensure_ascii=False)
    assert "demo@example.org" not in sent and "Примеры для проверки" not in sent
    assert "Вместимость отличается" in sent


@pytest.mark.usefixtures("index")
def test_mandatory_price_route_uses_verified_sku_once(api, provider):
    responses, requests = provider
    responses.append(tool("get_offer", offer_id="ask"))
    events = say(api, api.new_conversation(), "Сколько стоит артикул ask?")
    assert len(requests) == 1 and not responses
    assert "Цена по запросу" in events[-2]["text"]
    h = events[-1]["handoff"]
    assert h["reason"] == "price_request" and [p["offerId"] for p in h["products"]] == ["ask"]


def test_repeated_unregistered_tool_stops_without_showing_invented_price(api, provider):
    from app.agent.tool_agent import MAX_CALLS

    responses, requests = provider
    responses.extend(tool("set_price", amount=12345678) for _ in range(MAX_CALLS))
    cid = api.new_conversation()
    events = say(api, cid, "Посоветуйте программу.")
    h = events[-1]["handoff"]
    assert h["reason"] == "technical" and h["products"] == []
    assert "12345678" not in events[-2]["text"] and len(requests) == MAX_CALLS
    api.login()
    trace = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()[0]["steps"]
    assert any(s["tool"] == "support_failure" and s["args"]["reason"] == "call_limit" for s in trace)


def test_tools_retrieve_new_manager_publication_for_different_buyer(api, provider):
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    responses, requests = provider
    responses.extend([tool("search_knowledge", query="уведомления"), {"content": ANSWER}])
    cid = api.new_conversation("second")
    assert api.say(cid, QUESTION, role="second").status_code == 202
    api.idle()
    reply = api.events(cid, role="second")[-1]["data"]
    assert reply["text"] == ANSWER and len(requests) == 2
    [source] = reply["sources"]
    assert source["url"].endswith(f"/api/v1/knowledge/{pid}")
    assert api.call("second", "GET", source["url"]).text == f"{QUESTION}\n\n{ANSWER}"
