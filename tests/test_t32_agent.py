"""Exercise the production buyer handler, tools and journal with a scripted HTTP provider."""

import asyncio
import json

import httpx
import pytest

from app.agent.test_catalog_selection import index as index
from app.agent.test_catalog_selection import say
from app.agent.test_knowledge import ANSWER, PATCHES, QUESTION, propose, source_case
from app.config import get_settings
from app.core import llm
from app.db.repository import list_turns


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


@pytest.mark.usefixtures("index")
def test_memory_correction_after_plain_text_uses_buyer_journal(api, provider):
    responses, requests = provider
    responses.extend(
        [
            tool("respond", kind="clarify", question="Какие условия нужны?"),
            {"content": "Вы уточнили количество."},
            tool("recall_message", author="buyer", which="last", quote_to_customer=True),
            tool("recall_message", author="customer", which="last", quote_to_customer=True),
        ]
    )
    cid = api.new_conversation()
    statement = "Закупка рассчитана на 40 пользователей."
    say(api, cid, statement)
    reply = say(api, cid, "Сколько пользователей я указал?")[-1]
    assert reply["text"] == f"Вы написали: «{statement}»"
    assert "recall_message" in requests[-2]["messages"][-1]["content"]
    issue = json.loads(requests[-1]["messages"][-1]["content"])["issues"][0]
    assert issue["field"] == "author" and "customer" in issue["constraint"]
    assert not reply["sources"]


@pytest.mark.usefixtures("index")
def test_compare_reads_named_alternatives_and_leaves_purchase_selection_empty(api, provider):
    answers, requests = provider
    answers.append(tool("compare_catalog", first="PaperDesk Pro", second="PaperDesk Extra"))
    cid = api.new_conversation()
    events = say(api, cid, "Сравните PaperDesk Pro и PaperDesk Extra")
    reply = events[-1]
    assert len(requests) == 1 and reply["purpose"] == "clarification"
    assert "790 ₽" in reply["text"] and "PaperDesk Extra — точного совпадения" in reply["text"]
    assert len(reply["sources"]) == 1 and all(e["type"] not in {"products", "handoff"} for e in events)


@pytest.mark.usefixtures("index")
def test_repeated_search_without_new_offers_stops_and_clarifies(api, provider):
    answers, requests = provider
    answers.extend(tool("search_catalog", query="PaperDesk Pro") for _ in range(3))
    cid = api.new_conversation()
    events = say(api, cid, "Подберите PaperDesk Pro для покупки")
    assert len(requests) == 3 and "те же позиции" in events[-1]["text"]
    trace = api.client.portal.call(list_turns, cid)[-1].steps
    assert sum(step.tool == "search_catalog" for step in trace) == 1
    assert any(step.tool == "catalog_repeated_results" for step in trace)


@pytest.mark.usefixtures("index")
def test_refining_search_can_use_same_card_without_an_extra_model_round(api, provider):
    answers, requests = provider
    queries = ["PaperDesk", "PaperDesk Pro", "PaperDesk для покупки", "PaperDesk Pro"]
    answers.extend(tool("search_catalog", query=q) for q in queries)
    answers.append(tool("respond", kind="product", refs=["r1"], field="delivery"))
    cid = api.new_conversation()
    result = say(api, cid, "Какая поставка у PaperDesk Pro?")
    assert "Электронный" in result[-1]["text"] and result[-1]["sources"][0]["url"].endswith("/desk")
    trace = api.client.portal.call(list_turns, cid)[-1].steps
    assert sum(s.tool == "search_catalog" for s in trace) == 3
    assert not any(s.tool == "get_offer" for s in trace)
    assert len(requests) == len(queries) + 1


@pytest.mark.usefixtures("index")
@pytest.mark.parametrize("model_offers_help", [True, False])
def test_empty_catalog_results_offer_operator_before_opening_case(api, provider, model_offers_help):
    answers, requests = provider
    search = tool("search_catalog", query="NotACatalogProduct")
    answers.extend(
        [search, tool("respond", kind="handoff", reason="outside_catalog")] if model_offers_help else [search] * 3
    )
    cid = api.new_conversation()
    first = say(api, cid, "Хочу купить NotACatalogProduct")
    assert "оператора" in first[-1]["text"] and first[-1]["purpose"] == "clarification"
    assert not any(e["type"] == "handoff" for e in first)
    assert len(requests) == (2 if model_offers_help else 3)
    accepted = say(api, cid, "Да, подключайте.")
    assert len([e for e in accepted if e["type"] == "handoff"]) == 1


@pytest.mark.usefixtures("index")
def test_exact_sku_with_wrong_buyer_reports_verified_restriction(api, provider):
    answers, requests = provider
    answers.append(tool("get_offer", offer_id="new20"))
    events = say(api, api.new_conversation(), "Я физлицо. Хочу купить артикул new20 для себя.")
    assert len(requests) == 1
    assert "Юрлицо" in events[-1]["text"] and events[-1]["sources"][0]["url"].endswith("/new20")
    assert not any(e["type"] in {"products", "handoff"} for e in events)


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
def test_multi_offer_summary_keeps_evidence_and_does_not_repeat_card_names(api, provider):
    responses, _ = provider
    responses.extend(
        [
            tool("get_offer", offer_id="new20"),
            tool("get_offer", offer_id="new5"),
            tool("respond", kind="product", refs=["r1", "r2"], field="summary"),
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="product", refs=["r1"], field="term"),
        ]
    )
    cid = api.new_conversation()
    result = say(api, cid, "Сравните артикулы new20 и new5.")
    cards, reply = result[-2:]
    assert [p["offerId"] for p in cards["products"]] == ["new20", "new5"]
    assert len(reply["sources"]) == 2 and "2 позиции" in reply["text"]
    assert all(p["name"] not in reply["text"] for p in cards["products"])
    details = say(api, cid, "Какой срок у артикула new20?")[-1]["text"]
    assert "12 мес." in details and "мес.." not in details


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
@pytest.mark.parametrize(
    ("first", "change", "seats", "offer", "price"),
    [
        ("new20", "А на 5 пользователей какая цена и срок?", 5, "new5", "321 ₽"),
        (
            "new5",
            "Теперь нужно на 20 пользователей. Сколько будет стоить и как получить КП?",
            20,
            "new20",
            "1 234,56 ₽",
        ),
    ],
)
def test_seat_change_after_shown_card_reselects_the_same_product(api, provider, first, change, seats, offer, price):
    responses, requests = provider
    initial = 20 if first == "new20" else 5
    responses.extend(tool("select_licenses", variants=[{"name": "MDaemon", "seats": n}]) for n in (initial, seats))
    cid = api.new_conversation()
    shown = say(api, cid, f"Нужен MDaemon на {initial} пользователей для компании, покупаем впервые.")
    assert [p["offerId"] for p in shown[-2]["products"]] == [first]
    events = say(api, cid, change)
    assert len(requests) == 2 and not responses
    assert [p["offerId"] for p in events[-2]["products"]] == [offer]
    assert price in events[-1]["text"] and "называется" not in events[-1]["text"]
    assert ("Подготовить КП" in events[-1]["text"]) == ("КП" in change)


@pytest.mark.usefixtures("index")
def test_mandatory_price_route_uses_verified_sku_once(api, provider):
    responses, requests = provider
    responses.append(tool("get_offer", offer_id="ask"))
    events = say(api, api.new_conversation(), "Сколько стоит артикул ask?")
    assert len(requests) == 1 and not responses
    assert "Цена по запросу" in events[-2]["text"]
    assert "подтвердить менеджер" in events[-2]["text"]
    h = events[-1]["handoff"]
    assert h["reason"] == "price_request" and [p["offerId"] for p in h["products"]] == ["ask"]


@pytest.mark.usefixtures("index")
def test_ambiguous_turns_offer_help_then_recover_without_a_ticket(api, provider):
    from app.agent.handoff import ASK_PRODUCT, OFFER_HELP

    responses, _ = provider
    responses.extend(
        [
            tool("respond", kind="handoff", reason="insufficient_evidence"),
            tool("respond", kind="handoff", reason="technical"),
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="product", refs=["r1"], field="price"),
        ]
    )
    cid = api.new_conversation()
    first = say(api, cid, "Как с этим быть?")
    assert first[-1]["text"] == ASK_PRODUCT and first[-1]["purpose"] == "clarification"
    second = say(api, cid, "А с другим вариантом как быть?")
    assert second[-1]["text"] == OFFER_HELP and second[-1]["purpose"] == "clarification"
    resolved = say(api, cid, "Вот артикул new20. Сколько стоит?")
    assert resolved[-1]["purpose"] == "answer" and "1 234,56" in resolved[-1]["text"]
    assert all(e["data"]["type"] != "handoff" for e in api.events(cid))


def test_confirmed_help_after_two_misses_opens_one_case(api, provider):
    responses, _ = provider
    responses.extend([tool("respond", kind="handoff")] * 2)
    cid = api.new_conversation()
    say(api, cid, "Что с этим делать?")
    say(api, cid, "Пока не понимаю.")
    result = say(api, cid, "Да, пожалуйста")
    assert [e["handoff"]["reason"] for e in result if e["type"] == "handoff"] == ["insufficient_evidence"]


@pytest.mark.usefixtures("index")
def test_stalled_clarification_cannot_trigger_an_unrelated_knowledge_search(api, provider):
    from app.agent.handoff import OFFER_HELP

    responses, requests = provider
    responses.append(tool("respond", kind="clarify", question="О какой операции идёт речь?"))
    cid = api.new_conversation()
    say(api, cid, "А это как там сделать?")
    unresolved = say(api, cid, "Не понял, всё ещё не получается. Что делать?")
    assert unresolved[-1]["text"] == OFFER_HELP and len(requests) == 1
    responses.extend([tool("get_offer", offer_id="new20"), tool("respond", kind="product", refs=["r1"], field="price")])
    answered = say(api, cid, "Всё ещё не понимаю, сколько стоит артикул new20?")
    assert "1 234,56" in answered[-1]["text"] and answered[-1]["purpose"] == "answer"


def test_repeated_unregistered_tool_stops_without_showing_invented_price(api, provider):
    limit = get_settings().agent_max_model_calls
    responses, requests = provider
    responses.extend(tool("set_price", amount=12345678) for _ in range(limit))
    cid = api.new_conversation()
    events = say(api, cid, "Посоветуйте программу.")
    h = events[-1]["handoff"]
    assert h["reason"] == "technical" and h["products"] == []
    assert "12345678" not in events[-2]["text"] and len(requests) == limit
    api.login()
    trace = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()[0]["steps"]
    assert any(s["tool"] == "support_failure" and s["args"]["reason"] == "call_limit" for s in trace)


@pytest.mark.usefixtures("index")
def test_buyer_can_get_a_result_after_the_old_model_and_tool_limits(api, provider):
    responses, requests = provider
    for model_round in range(5):
        calls = [tool("get_offer", offer_id="new20")["tool_calls"][0] for _ in range(2)]
        for position, call in enumerate(calls):
            call["id"] = f"step-{model_round}-{position}"
        responses.append({"tool_calls": calls})
    responses.append(tool("respond", kind="product", refs=["r1"], field="price"))
    events = say(api, api.new_conversation(), "Цена по артикулу new20?")
    assert len(requests) == 6 and "1 234,56 ₽" in events[-1]["text"]


def test_tool_limit_counts_parallel_calls_before_executing_the_batch(api, provider):
    responses, requests = provider
    limit = get_settings().agent_max_tool_calls
    calls = [{**tool("get_offer", offer_id="new20")["tool_calls"][0], "id": f"lookup-{n}"} for n in range(limit + 1)]
    responses.append({"tool_calls": calls})
    cid = api.new_conversation()
    events = say(api, cid, "Цена по артикулу new20?")
    assert len(requests) == 1 and events[-1]["handoff"]["reason"] == "technical"
    api.login()
    trace = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()[0]["steps"]
    assert not any(s["tool"] == "get_offer" for s in trace)
    assert any(s["tool"] == "support_failure" and s["args"]["reason"] == "call_limit" for s in trace)


def test_turn_deadline_cancels_pending_provider_call(api, provider, monkeypatch):
    from app.agent import tool_agent

    cancelled = []

    async def pending(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(get_settings(), "agent_turn_timeout_s", 0.03)
    monkeypatch.setattr(tool_agent, "llm_call", pending)
    cid = api.new_conversation()
    events = say(api, cid, "Подберите офис.")
    assert cancelled == [True] and events[-1]["handoff"]["reason"] == "technical"
    api.login()
    trace = api.call("manager", "GET", f"/api/v1/manager/conversations/{cid}/trace").json()[0]["steps"]
    assert any(s["tool"] == "support_failure" and s["args"]["reason"] == "timeout" for s in trace)


def test_tools_retrieve_new_manager_publication_for_different_buyer(api, provider):
    _, hid = source_case(api)
    pid = propose(api, hid)
    assert api.call("manager", "POST", f"{PATCHES}/{pid}/approve").status_code == 200
    responses, requests = provider
    responses.extend([tool("search_knowledge", query=QUESTION), {"content": ANSWER}])
    cid = api.new_conversation("second")
    assert api.say(cid, "Хочу управлять уведомлениями магазина", role="second").status_code == 202
    api.idle()
    reply = api.events(cid, role="second")[-1]["data"]
    assert reply["text"] == ANSWER and len(requests) == 2
    [source] = reply["sources"]
    assert source["url"].endswith(f"/api/v1/knowledge/{pid}")
    assert api.call("second", "GET", source["url"]).text == f"{QUESTION}\n\n{ANSWER}"


@pytest.mark.usefixtures("index")
@pytest.mark.parametrize(
    "question", ["И что дальше?", "Я выбрал этот товар. Можете предложить подготовить коммерческое предложение?"]
)
def test_quote_acceptance_uses_verified_selection_without_another_model_call(api, provider, question):
    responses, requests = provider
    responses.extend(
        [
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="product", refs=["r1"], field="price"),
            tool("get_offer", offer_id="new20"),
            tool("respond", kind="clarify", question="Оформить запрос на расчёт и КП по выбранным позициям?"),
        ]
    )
    cid = api.new_conversation()
    say(api, cid, "Цена по артикулу new20?")
    say(api, cid, question)
    for prompt in ("Да", "Эй бля подготавливай КП"):
        receipt = api.say(cid, prompt, key=prompt.encode().hex())
        assert receipt.status_code == 202 and receipt.headers["X-Chat-Action"] == "open-quote-form"
        assert set(receipt.json()) == {"conversationId", "actionId"}
        repeat = api.say(cid, prompt, key=prompt.encode().hex())
        assert repeat.headers["X-Chat-Action"] == "open-quote-form" and repeat.json() == receipt.json()
    current = api.call("customer", "GET", f"/api/v1/conversations/{cid}/quotes/current").json()
    assert [p["offerId"] for p in current["selection"]] == ["new20"]
    assert current["revision"] == 0 and len(requests) == 4


@pytest.mark.parametrize("last", ["Для какого товара подготовить КП?", "Подключить оператора, чтобы оформить КП?"])
def test_unrelated_yes_does_not_open_quote(last):
    from app.agent.quote_intent import offers_quote

    assert not offers_quote(last)


@pytest.mark.usefixtures("index")
def test_missing_capacity_only_retries_if_the_evidence_changes(api, provider):
    responses, requests = provider
    responses.extend([tool("select_licenses", variants=[{"name": "PaperDesk Pro", "seats": 40}])] * 2)
    events = say(api, api.new_conversation(), "Хочу лицензию PaperDesk Pro.")
    assert len(requests) == 2 and "сколько пользователей" in events[-1]["text"]
    assert all(event["type"] not in {"handoff", "products"} for event in events)


@pytest.mark.usefixtures("index")
@pytest.mark.parametrize(
    ("opening", "reply", "codes"),
    [
        ("MDaemon на 20 пользователей для компании", "Это новая покупка, берём впервые.", {"new20"}),
        (
            "MDaemon для компании",
            "Первая покупка. Покажите для сравнения варианты на 5 и 20 пользователей.",
            {"new5", "new20"},
        ),
    ],
)
def test_named_selection_resumes_after_customer_supplies_fields(api, provider, opening, reply, codes):
    responses, requests = provider
    responses.append(tool("select_licenses", variants=[{"name": "MDaemon"}]))
    cid = api.new_conversation()
    assert say(api, cid, opening)[-1]["purpose"] == "clarification"
    events = say(api, cid, reply)
    if len(codes) == 1:
        assert {p["offerId"] for e in events for p in e.get("products", [])} == codes
    else:
        assert all(code in events[-1]["text"] for code in codes)
        assert {s["url"].rsplit("/", 1)[-1] for s in events[-1]["sources"]} == codes
        assert not api.call("customer", "GET", f"/api/v1/conversations/{cid}/quotes/current").json()["selection"]
    assert len(requests) == 1


@pytest.mark.usefixtures("index")
def test_named_selection_allows_new_topic_after_clarification(api, provider):
    responses, requests = provider
    responses.extend(
        [
            tool("select_licenses", variants=[{"name": "MDaemon"}]),
            tool("select_licenses", variants=[{"name": "PaperDesk Pro"}]),
        ]
    )
    cid = api.new_conversation()
    say(api, cid, "MDaemon для компании")
    events = say(api, cid, "Новая покупка. Вместо этого нужен PaperDesk Pro для юрлица.")
    assert {p["offerId"] for e in events for p in e.get("products", [])} == {"desk"}
    assert len(requests) == 2


@pytest.mark.parametrize("request_text", ["два КП", "2 коммерческих предложения", "КП на каждый товар"])
def test_separate_quotes_do_not_publish_a_combined_basket(api, index, provider, request_text):  # noqa: F811
    import sqlite3

    with sqlite3.connect(index) as db:
        db.execute("UPDATE offers SET available=1 WHERE id='verify'")
    responses, _ = provider
    responses.append(tool("select_licenses", variants=[{"name": "PaperDesk Pro"}, {"name": "Widget Server"}]))
    events = say(
        api,
        api.new_conversation(),
        f"Пришлите {request_text} на PaperDesk Pro и Widget Server. Для юрлица, покупаем впервые.",
    )
    reply = events[-1]
    assert reply["purpose"] == "clarification" and "отдельное КП" in reply["text"]
    assert "910 ₽" not in reply["text"] and all(e["type"] not in {"products", "quote"} for e in events)
