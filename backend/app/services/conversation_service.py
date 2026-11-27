"""Команды T04: диалоги, реплики, обращения к оператору, журнал и Idempotency-Key.

Приём команд сериализован одним локом процесса: проверка ключа, проверка состояния
и запись в журнал не перемежаются между запросами. Ход агента выполняется в фоне.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.product_answers import refresh_products
from app.config import get_settings
from app.contracts.t04 import (
    ID_PATTERN,
    ConfirmQuoteAction,
    CustomerAction,
    HandoffData,
    ManagerDecision,
    MessageAction,
    MessageData,
    Product,
    ReplyDecision,
    RequestDocumentAction,
    RequestOperatorAction,
    Source,
)
from app.core import events
from app.core.auth import Principal
from app.core.errors import ApiError, invalid_input, not_found
from app.db import repository
from app.db.database import get_db
from app.services import quotes, turn_service

OPERATOR_QUESTION = "Покупатель попросил подключить оператора."
CONVERSATION_NOT_FOUND = "Диалог не найден."
HANDOFF_NOT_FOUND = "Обращение не найдено."

_command_lock = asyncio.Lock()
_URL_RE = re.compile(r"https://[^\s<>\"'()]+")

Result = tuple[int, dict[str, Any]]


# --- Idempotency-Key ------------------------------------------------------------------


def _request_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def idempotent(
    principal: Principal, method: str, path: str, key: str | None, body: Any, handler: Callable[[], Awaitable[Result]]
) -> Result:
    """Повтор принятой команды возвращает исходный ответ; другое тело с тем же ключом — 409 key_conflict.

    Отказ до принятия (исключение ApiError) ключ не занимает.
    """
    if not key or len(key) > 128 or not re.fullmatch(ID_PATTERN, key):
        raise invalid_input("Нужен заголовок Idempotency-Key: 1–128 символов [A-Za-z0-9._-].")
    request_hash = _request_hash(body)
    async with _command_lock:
        stored = await repository.get_idempotent(principal.id, method, path, key)
        if stored is not None:
            if stored.request_hash != request_hash:
                raise ApiError(409, "key_conflict", "Ключ уже использован для другой команды.")
            return stored.status, stored.body
        command = repository.CommandReceipt((principal.id, method, path, key), request_hash)
        token = repository.active_command.set(command)
        try:
            status, payload = await handler()
            if not command.saved:
                await command.save(status, payload)
            return status, payload
        finally:
            repository.active_command.reset(token)


# --- Доступ ---------------------------------------------------------------------------


async def conversation_for(principal: Principal, conversation_id: str) -> repository.Conversation:
    conv = await repository.get_conversation(conversation_id)
    if conv is None:
        raise not_found(CONVERSATION_NOT_FOUND)
    if principal.role == "customer" and conv.owner_id != principal.id:
        raise not_found(CONVERSATION_NOT_FOUND)
    if principal.role == "manager" and not await repository.manager_has_access(conversation_id):
        raise not_found(CONVERSATION_NOT_FOUND)
    return conv


# --- Журнал ------------------------------------------------------------------------------


async def append(conversation_id: str, action_id: str, datas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Записать события в журнал и разослать подписчикам SSE."""
    written = await repository.append_events(conversation_id, action_id, datas)
    events.publish(conversation_id, written)
    return written


async def read_events(principal: Principal, conversation_id: str, after: int) -> dict[str, Any]:
    await conversation_for(principal, conversation_id)
    page, has_more = await repository.list_events(conversation_id, after)
    return {"events": page, "nextAfter": page[-1]["seq"] if page else after, "hasMore": has_more}


def receipt(conversation_id: str, action_id: str) -> dict[str, Any]:
    return {"conversationId": conversation_id, "actionId": action_id}


# --- Покупатель ---------------------------------------------------------------------------


async def create_conversation(principal: Principal) -> Result:
    session = await repository.create_conversation(principal.id, await turn_service.default_mode())
    return 201, {"conversationId": session.id}


async def submit_action(principal: Principal, conversation_id: str, action: CustomerAction) -> Result:
    conv = await conversation_for(principal, conversation_id)
    if isinstance(action, MessageAction):
        return await _customer_message(conv, action)
    if isinstance(action, RequestOperatorAction):
        return await _request_operator(conv)
    if isinstance(action, ConfirmQuoteAction | RequestDocumentAction):
        return await quotes.quote_action(conversation_id, action)
    raise invalid_input("Неизвестная команда.")


async def _customer_message(conv: repository.Conversation, action: MessageAction) -> Result:
    s = get_settings()
    cid = conv.session.id
    text = action.text.strip()
    if not text:
        raise invalid_input("Пустое сообщение.")
    if len(text) > s.max_message_chars:
        raise invalid_input(f"Сообщение длиннее {s.max_message_chars} символов.")

    action_id = repository.new_id("a")
    if action.handoffId is not None:
        found = await repository.get_handoff(action.handoffId)
        if found is None or found.handoff.conversationId != cid:
            raise not_found(HANDOFF_NOT_FOUND)
        await quotes.check_line_revision(found.handoff)
        if found.handoff.status != "open":
            raise ApiError(409, "already_resolved", "Обращение уже закрыто.")
        data = MessageData(author="customer", purpose="input", text=text, sources=[], handoffId=action.handoffId)
        await append(cid, action_id, [data.model_dump(mode="json")])
        return 202, receipt(cid, action_id)

    if turn_service.is_busy(cid):
        raise ApiError(409, "agent_busy", "Агент ещё отвечает на предыдущее сообщение.")
    if await repository.count_agent_inputs(cid) >= s.max_turns_per_session:
        raise ApiError(
            429,
            "rate_limited",
            "Достигнут лимит сообщений в диалоге. Дождитесь ответа оператора или начните новый диалог.",
        )
    result = await quotes.from_message(cid, text)
    if result:
        return result
    history = await repository.history_messages(cid)
    data = MessageData(author="customer", purpose="input", text=text, sources=[])
    await append(cid, action_id, [data.model_dump(mode="json")])
    turn_service.start_turn(conv.session, action_id, text, history)
    return 202, receipt(cid, action_id)


async def _request_operator(conv: repository.Conversation) -> Result:
    """Кнопка «Позвать оператора»: повтор при открытом обращении возвращает исходную квитанцию."""
    cid = conv.session.id
    async with get_db().tx() as conn:
        existing = await repository.find_open_handoff(cid, "customer_request")
        if existing is not None:
            return 202, receipt(cid, existing.action_id)
        action_id = repository.new_id("a")
        saved_products = [Product.model_validate(p) for p in await repository.last_products(cid)]
        products = await asyncio.to_thread(
            refresh_products, get_settings().data_dir / "private" / "catalog.sqlite3", saved_products
        )
        question = OPERATOR_QUESTION
        history = await repository.history_messages(cid)
        if history and history[-1].role == "agent":
            turns = await repository.list_turns(cid)
            if turns and turns[-1].decision == "answer" and turns[-1].steps:
                last_step = turns[-1].steps[-1]
                if last_step.tool == "product_facts":
                    question = last_step.args.get("question", question)
        handoff = await repository.create_handoff(
            cid, action_id, "customer_request", question, [p.model_dump(mode="json") for p in products], conn=conn
        )
        written = await repository.append_events(
            cid, action_id, [HandoffData(handoff=handoff).model_dump(mode="json")], conn=conn
        )
    events.publish(cid, written)
    return 202, receipt(cid, action_id)


# --- Оператор -------------------------------------------------------------------------------


async def list_handoffs() -> dict[str, Any]:
    return {"items": [h.model_dump(mode="json") for h in await repository.list_open_handoffs()]}


def sources_from_text(text: str, products: list[Any]) -> list[Source]:
    """Ссылки из ответа оператора; ссылка на карточку товара из обращения берёт её подпись."""
    known = {p.source.url: p.source for p in products}
    result: list[Source] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        source = known.get(url) or Source(url=url, label=url.split("/")[2])
        if source not in result:
            result.append(source)
    return result


async def resolve_handoff(handoff_id: str, decision: ManagerDecision) -> Result:
    found = await repository.get_handoff(handoff_id)
    if found is None:
        raise not_found(HANDOFF_NOT_FOUND)
    handoff = found.handoff
    if handoff.status != "open":
        raise ApiError(409, "already_resolved", "Обращение уже закрыто.")
    await quotes.check_line_revision(handoff)
    if not isinstance(decision, ReplyDecision):
        return await quotes.approve_price(handoff, decision)

    text = decision.reply.strip()
    if not text:
        raise invalid_input("Пустой ответ.")
    cid = handoff.conversationId
    action_id = repository.new_id("a")
    message = MessageData(
        author="manager",
        purpose="answer" if decision.type == "close" else "clarification",
        text=text,
        sources=sources_from_text(text, handoff.products),
        handoffId=handoff_id,
    )
    datas = [message.model_dump(mode="json")]
    async with get_db().tx() as conn:
        if decision.type == "close":
            await quotes.invalidate_exception(conn, handoff)
            await repository.resolve_handoff(handoff_id, conn=conn)
            closed = handoff.model_copy(update={"status": "resolved"})
            datas.append(HandoffData(handoff=closed).model_dump(mode="json"))
        written = await repository.append_events(cid, action_id, datas, conn=conn)
    events.publish(cid, written)
    return 202, receipt(cid, action_id)
