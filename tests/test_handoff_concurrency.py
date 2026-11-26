import asyncio
from threading import Event

from app.agent import runner
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest
from app.db import repository


def test_button_and_background_turn_create_one_handoff(api, monkeypatch):
    async def ask_for_operator(session, history, user_text, emit_step):
        return AgentReply(
            turn=AgentTurn(
                turn_id="turn_test",
                session_id=session.id,
                steps=[],
                decision="escalate",
                escalation_reason="customer_request",
                total_cost_rub=0,
                total_latency_ms=0,
            ),
            handoff=HandoffRequest(reason="customer_request", question=user_text),
        )

    # Let the button arrive while the agent is deciding whether a ticket already exists.
    original = repository.find_open_handoff
    first_read = Event()
    second_read = asyncio.Event()

    async def concurrent_lookup(cid, reason):
        found = await original(cid, reason)
        if first_read.is_set():
            second_read.set()
        else:
            first_read.set()
            try:
                await asyncio.wait_for(second_read.wait(), timeout=0.5)
            except TimeoutError:
                pass  # With the fix, the second reader waits for the first transaction to finish.
        return found

    monkeypatch.setattr(runner, "run_turn", ask_for_operator)
    monkeypatch.setattr(repository, "find_open_handoff", concurrent_lookup)
    cid = api.new_conversation()
    assert api.say(cid, "Позовите оператора").status_code == 202
    assert first_read.wait(5)
    response = api.call("customer", "POST", f"/api/v1/conversations/{cid}/actions", json={"type": "request_operator"})
    assert response.status_code == 202
    api.idle()
    created = [event for event in api.events(cid) if event["data"]["type"] == "handoff"]
    assert len(created) == 1
    assert response.json()["actionId"] == created[0]["actionId"]
    api.login()
    queue = api.call("manager", "GET", "/api/v1/manager/handoffs")
    assert queue.json()["items"] == [created[0]["data"]["handoff"]]
