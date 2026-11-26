"""Прогон примеров T04 (contracts/t04/examples.json) через настоящий API стенда.

Агента заменяет сценарный runner: он отвечает теми событиями, которые пример ждёт от агента.
Каждый ответ сервера проверяется схемой contracts/t04/openapi.yaml (их же verify.Contract)
и сравнивается с примером. ID сервера сопоставляются с ID примера, метки `at` не сравниваются.
Блок КП и документов (c-docs) в T05 пока не реализован.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.agent import runner
from app.config import get_settings
from app.contracts.schemas import AgentReply, AgentTurn, HandoffRequest, StepTrace
from app.contracts.t04 import Product, Source
from app.core import security
from app.db.repository import new_id
from tests.conftest import Api

T04 = Path(__file__).resolve().parents[1] / "contracts" / "t04"
SCENARIOS = ["c-tender", "c-help", "c-price", "c-need"]
ID_KEYS = {"conversationId", "actionId", "handoffId"}

ROUTES = {
    "createConversation": ("POST", "/api/v1/conversations"),
    "submitAction": ("POST", "/api/v1/conversations/{conversationId}/actions"),
    "readEvents": ("GET", "/api/v1/conversations/{conversationId}/events"),
    "listHandoffs": ("GET", "/api/v1/manager/handoffs"),
    "resolveHandoff": ("POST", "/api/v1/manager/handoffs/{handoffId}/resolution"),
    "downloadDocument": ("GET", "/api/v1/documents/{documentId}"),
}


def _load_verify():
    spec = importlib.util.spec_from_file_location("t04_verify", T04 / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = _load_verify()
CONTRACT = VERIFY.Contract(yaml.safe_load((T04 / "openapi.yaml").read_text(encoding="utf-8")))
EXAMPLES = {s["id"]: s for s in json.loads((T04 / "examples.json").read_text(encoding="utf-8"))}


def test_examples_cover_all_t04_operations():
    assert set(ROUTES) == set(CONTRACT.operations)


# --- Сценарный агент ------------------------------------------------------------------


def agent_script(scenario: dict) -> dict[str, list[dict]]:
    """Текст реплики покупателя -> события, которые пример ждёт от агента в ответ на неё."""
    texts: dict[str, str] = {}
    for step in scenario["steps"]:
        body = step.get("body") or {}
        if (
            step["operationId"] == "submitAction"
            and step["status"] == 202
            and body.get("type") == "message"
            and "handoffId" not in body
        ):
            texts[step["response"]["actionId"]] = body["text"]
    script: dict[str, list[dict]] = {}
    seen: set[tuple[str, int]] = set()
    for step in scenario["steps"]:
        if step["operationId"] != "readEvents" or step["status"] != 200:
            continue
        for event in step["response"]["events"]:
            text = texts.get(event["actionId"])
            data = event["data"]
            if text is None or (event["actionId"], event["seq"]) in seen:
                continue
            seen.add((event["actionId"], event["seq"]))
            if data["type"] == "message" and data["author"] == "customer":
                continue
            script.setdefault(text, []).append(data)
    return script


class ScriptedAgent:
    def __init__(self, script: dict[str, list[dict]]):
        self.script = script
        self.gate: asyncio.Event | None = None

    async def close_gate(self):
        self.gate = asyncio.Event()

    async def open_gate(self):
        if self.gate is not None:
            self.gate.set()

    async def run_turn(self, session, history, user_text, emit_step) -> AgentReply:
        if self.gate is not None:
            await self.gate.wait()
        datas = self.script[user_text]
        reply: dict[str, Any] = {}
        for data in datas:
            if data["type"] == "products":
                reply["products"] = [Product(**p) for p in data["products"]]
            elif data["type"] == "message":
                reply.update(text=data["text"], purpose=data["purpose"], sources=[Source(**s) for s in data["sources"]])
            elif data["type"] == "handoff":
                h = data["handoff"]
                reply["handoff"] = HandoffRequest(
                    reason=h["reason"], question=h["question"], products=[Product(**p) for p in h["products"]]
                )
        step = StepTrace(tool="scripted_agent", args={"text": user_text}, result_summary=f"{len(datas)} events")
        await emit_step(step)
        turn = AgentTurn(
            turn_id=new_id("turn"),
            session_id=session.id,
            steps=[step],
            decision="escalate" if "handoff" in reply else "answer",
            escalation_reason=reply["handoff"].reason if "handoff" in reply else None,
            total_cost_rub=0.0,
            total_latency_ms=0,
        )
        return AgentReply(turn=turn, **reply)


# --- Сопоставление ответов с примером ---------------------------------------------------------


class Aliases:
    def __init__(self):
        self.real: dict[str, str] = {}

    def bind(self, example: str, actual: Any, where: str):
        assert isinstance(actual, str), f"{where}: ID must be a string"
        if example in self.real:
            assert self.real[example] == actual, f"{where}: {example} -> {self.real[example]}, got {actual}"
        else:
            assert actual not in self.real.values(), f"{where}: {actual} already maps to another example ID"
            self.real[example] = actual

    def sub(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.real.get(v, v) if k in ID_KEYS else self.sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sub(v) for v in value]
        return value


def match(expected: Any, actual: Any, aliases: Aliases, where: str = "$"):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{where}: expected object, got {actual!r}"
        assert set(expected) == set(actual), f"{where}: keys {sorted(actual)} != {sorted(expected)}"
        for k, v in expected.items():
            if k in ID_KEYS:
                aliases.bind(v, actual[k], f"{where}.{k}")
            elif k == "at":
                assert isinstance(actual[k], str) and actual[k].endswith("Z"), f"{where}.at"
            else:
                match(v, actual[k], aliases, f"{where}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), (
            f"{where}: expected {len(expected)} items, got {actual!r}"
        )
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            match(e, a, aliases, f"{where}[{i}]")
    else:
        assert expected == actual, f"{where}: expected {expected!r}, got {actual!r}"


# --- Прогон -------------------------------------------------------------------------------------


def _url(step: dict, aliases: Aliases) -> tuple[str, str, dict | None]:
    method, template = ROUTES[step["operationId"]]
    params = aliases.sub(dict(step.get("params", {})))
    query = {"after": params.pop("after")} if "after" in params else None
    return method, template.format(**params), query


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_scenario(scenario_id, api: Api, client, monkeypatch):
    scenario = EXAMPLES[scenario_id]
    agent = ScriptedAgent(agent_script(scenario))
    monkeypatch.setattr(runner, "run_turn", agent.run_turn)
    aliases = Aliases()
    roles = {s["role"] for s in scenario["steps"]}
    if "manager" in roles:
        api.login("manager")
    if "other_customer" in roles:
        api.new_conversation("other_customer")

    steps = scenario["steps"]
    for i, step in enumerate(steps):
        label = f"{scenario_id} / {step['label']}"
        nxt = steps[i + 1] if i + 1 < len(steps) else {}
        busy_next = nxt.get("status") == 409 and nxt.get("response", {}).get("code") == "agent_busy"
        if busy_next:
            client.portal.call(agent.close_gate)
        if step["operationId"] in ("readEvents", "listHandoffs"):
            api.idle()

        method, url, query = _url(step, aliases)
        if step["status"] == 429:
            conv = url.split("/")[4]
            limit = get_settings().rate_limit_session_msg_per_min
            for _ in range(limit):
                security.conversation_limiter.hit(f"conversation:{conv}", limit)
        body = aliases.sub(step.get("body")) if "body" in step else None
        response = api.call(step["role"], method, url, json=body, key=step.get("key"), params=query, auto_key=False)

        if step["status"] == 429:
            security.conversation_limiter.reset()
            assert int(response.headers["retry-after"]) >= 1, label
        if step.get("response", {}).get("code") == "agent_busy":
            client.portal.call(agent.open_gate)

        assert response.status_code == step["status"], f"{label}: {response.status_code} {response.text}"
        actual = response.json()
        CONTRACT.check_exchange({**step, "response": actual})
        match(step["response"], actual, aliases, label)

    api.idle()
