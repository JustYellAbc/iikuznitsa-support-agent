"""CRUD поверх app.db.database.

Функции с параметром conn пишут в переданную транзакцию (async with get_db().tx() as conn),
без него открывают свою. Так событие журнала и изменение состояния фиксируются атомарно.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from app.contracts.schemas import (
    AgentTurn,
    Draft,
    KBPatch,
    LLMCall,
    Message,
    Mode,
    PatchStatus,
    Session,
    StepStats,
)
from app.contracts.t04 import Handoff, HandoffReason, QuoteLineRef
from app.core.errors import ApiError, invalid_input, not_found
from app.db.database import get_db

EVENTS_PAGE = 100


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def event_time() -> str:
    """Метка `at` журнала в формате контракта: 2026-11-26T09:00:01.000Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@asynccontextmanager
async def _tx(conn: aiosqlite.Connection | None) -> AsyncIterator[aiosqlite.Connection]:
    if conn is not None:
        yield conn
    else:
        async with get_db().tx() as own:
            yield own


async def _fetchone(conn: aiosqlite.Connection, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


# --- Настройки ---------------------------------------------------------------


async def get_setting(key: str) -> str | None:
    return await get_db().fetchval("SELECT value FROM app_settings WHERE key = ?", (key,))


async def set_setting(key: str, value: str) -> None:
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- Сессии входа ----------------------------------------------------------------


@dataclass(frozen=True)
class AuthSession:
    role: str
    principal: str


async def create_auth_session(token_hash: str, role: str, principal: str, expires_at: datetime) -> None:
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions(token_hash, role, principal, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, role, principal, now_iso(), to_iso(expires_at)),
        )


async def get_auth_session(token_hash: str) -> AuthSession | None:
    row = await get_db().fetchone(
        "SELECT role, principal FROM auth_sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now_iso())
    )
    return AuthSession(row["role"], row["principal"]) if row else None


async def delete_auth_session(token_hash: str) -> None:
    async with get_db().tx() as conn:
        await conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


# --- Диалоги ---------------------------------------------------------------------


@dataclass(frozen=True)
class Conversation:
    session: Session
    owner_id: str


async def create_conversation(owner_id: str, mode: Mode) -> Session:
    session = Session(id=new_id("c"), mode=mode, created_at=now_iso())
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO conversations(id, owner_id, mode, created_at) VALUES (?, ?, ?, ?)",
            (session.id, owner_id, mode, to_iso(session.created_at)),
        )
    return session


async def get_conversation(conversation_id: str) -> Conversation | None:
    row = await get_db().fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if row is None:
        return None
    return Conversation(Session(id=row["id"], mode=row["mode"], created_at=row["created_at"]), row["owner_id"])


async def set_conversation_mode(conversation_id: str, mode: Mode) -> None:
    async with get_db().tx() as conn:
        await conn.execute("UPDATE conversations SET mode = ? WHERE id = ?", (mode, conversation_id))


async def manager_has_access(conversation_id: str) -> bool:
    """Оператор видит диалог, если по нему есть обращение или черновик."""
    value = await get_db().fetchval(
        "SELECT EXISTS(SELECT 1 FROM handoffs WHERE conversation_id = ?) "
        "OR EXISTS(SELECT 1 FROM drafts WHERE conversation_id = ?)",
        (conversation_id, conversation_id),
    )
    return bool(value)


# --- Журнал событий ------------------------------------------------------------------


async def append_events(
    conversation_id: str, action_id: str, datas: list[dict[str, Any]], conn: aiosqlite.Connection | None = None
) -> list[dict[str, Any]]:
    """Дописывает события подряд и возвращает их в формате Event контракта."""
    result = []
    async with _tx(conn) as c:
        row = await _fetchone(
            c, "SELECT coalesce(max(seq), 0) FROM events WHERE conversation_id = ?", (conversation_id,)
        )
        seq = row[0]
        for data in datas:
            seq += 1
            event = {"seq": seq, "at": event_time(), "actionId": action_id, "data": data}
            await c.execute(
                "INSERT INTO events(conversation_id, seq, at, action_id, data) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, seq, event["at"], action_id, json.dumps(data, ensure_ascii=False)),
            )
            result.append(event)
    return result


def _event(row: aiosqlite.Row) -> dict[str, Any]:
    return {"seq": row["seq"], "at": row["at"], "actionId": row["action_id"], "data": json.loads(row["data"])}


async def list_events(conversation_id: str, after: int, limit: int = EVENTS_PAGE) -> tuple[list[dict], bool]:
    async with get_db().committed_read():
        rows = await get_db().fetchall(
            "SELECT * FROM events WHERE conversation_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (conversation_id, after, limit + 1),
        )
    return [_event(r) for r in rows[:limit]], len(rows) > limit


async def count_agent_inputs(conversation_id: str) -> int:
    """Реплики покупателя, на которые отвечал агент (без ответов в открытое обращение)."""
    return await get_db().fetchval(
        "SELECT count(*) FROM events WHERE conversation_id = ? AND json_extract(data, '$.type') = 'message' "
        "AND json_extract(data, '$.author') = 'customer' AND json_extract(data, '$.handoffId') IS NULL",
        (conversation_id,),
    )


async def last_products(conversation_id: str) -> list[dict[str, Any]]:
    # A published new search supersedes the old selection even when it only asks a question.
    # A pending pilot draft has no published answer/clarification and does not change it.
    row = await get_db().fetchone(
        """WITH searches AS (
               SELECT json_extract(s.value, '$.args.journal_action_id') AS action_id, t.decision
               FROM turns t, json_each(t.steps) s
               WHERE t.session_id = ? AND (json_extract(s.value, '$.tool') = 'catalog_search'
                    OR json_extract(s.value, '$.args.replaces_products') = 1)
           ), latest AS (
               SELECT action_id FROM events WHERE conversation_id = ? AND (
                   json_extract(data, '$.type') = 'products' OR
                   (action_id IN (SELECT action_id FROM searches)
                    AND json_extract(data, '$.author') = 'agent'
                    AND (json_extract(data, '$.purpose') IN ('answer','clarification')
                         OR (json_extract(data, '$.purpose') = 'notice' AND action_id IN (
                             SELECT action_id FROM searches WHERE decision = 'escalate'))))
               ) ORDER BY seq DESC LIMIT 1
           )
           SELECT data FROM events WHERE conversation_id = ? AND action_id = (SELECT action_id FROM latest)
           AND json_extract(data, '$.type') = 'products' ORDER BY seq DESC LIMIT 1""",
        (conversation_id, conversation_id, conversation_id),
    )
    return json.loads(row["data"])["products"] if row else []


_ROLE_BY_AUTHOR = {"customer": "client", "agent": "agent", "manager": "operator"}


async def history_messages(conversation_id: str) -> list[Message]:
    """Сообщения журнала в формате истории агента."""
    rows = await get_db().fetchall(
        "SELECT * FROM events WHERE conversation_id = ? AND json_extract(data, '$.type') = 'message' ORDER BY seq",
        (conversation_id,),
    )
    result = []
    for r in rows:
        data = json.loads(r["data"])
        result.append(
            Message(
                id=f"{conversation_id}:{r['seq']}",
                session_id=conversation_id,
                role=_ROLE_BY_AUTHOR[data["author"]],
                text=data["text"],
                sources=[s["url"] for s in data["sources"]],
                created_at=r["at"],
            )
        )
    return result


# --- Обращения ----------------------------------------------------------------------


@dataclass(frozen=True)
class HandoffRow:
    handoff: Handoff
    action_id: str


HANDOFF_SELECT = (
    "SELECT h.*, l.quote_id, l.revision, l.offer_id FROM handoffs h LEFT JOIN quote_handoffs l ON h.id=l.handoff_id "
)


def _handoff(row: aiosqlite.Row) -> HandoffRow:
    return HandoffRow(
        Handoff(
            handoffId=row["id"],
            conversationId=row["conversation_id"],
            reason=row["reason"],
            question=row["question"],
            products=json.loads(row["products"]),
            status=row["status"],
            quoteLine=QuoteLineRef(quoteId=row["quote_id"], revision=row["revision"], offerId=row["offer_id"])
            if row["quote_id"]
            else None,
        ),
        row["action_id"],
    )


async def create_handoff(
    conversation_id: str,
    action_id: str,
    reason: HandoffReason,
    question: str,
    products: list[dict[str, Any]],
    conn: aiosqlite.Connection | None = None,
) -> Handoff:
    handoff = Handoff(
        handoffId=new_id("h"),
        conversationId=conversation_id,
        reason=reason,
        question=question,
        products=products,
        status="open",
    )
    ts = now_iso()
    async with _tx(conn) as c:
        await c.execute(
            "INSERT INTO handoffs(id, conversation_id, action_id, reason, question, products, status, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                handoff.handoffId,
                conversation_id,
                action_id,
                reason,
                question,
                json.dumps(products, ensure_ascii=False),
                ts,
                ts,
            ),
        )
    return handoff


async def get_handoff(handoff_id: str) -> HandoffRow | None:
    row = await get_db().fetchone(HANDOFF_SELECT + "WHERE h.id = ?", (handoff_id,))
    return _handoff(row) if row else None


async def find_open_handoff(conversation_id: str, reason: HandoffReason) -> HandoffRow | None:
    row = await get_db().fetchone(
        HANDOFF_SELECT + "WHERE conversation_id = ? AND reason = ? AND status = 'open' AND l.handoff_id IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (conversation_id, reason),
    )
    return _handoff(row) if row else None


async def find_open_quote_handoff(conversation_id: str, reason: HandoffReason, offer_id: str) -> HandoffRow | None:
    row = await get_db().fetchone(
        HANDOFF_SELECT + "WHERE h.conversation_id = ? AND h.reason = ? AND h.status = 'open' "
        "AND l.offer_id = ? AND EXISTS ("
        "SELECT 1 FROM quote_revisions qr WHERE qr.quote_id = l.quote_id AND qr.revision = l.revision "
        "AND qr.reselect = 0 AND qr.revision = (SELECT max(revision) FROM quote_revisions WHERE quote_id = l.quote_id)"
        ") ORDER BY h.created_at DESC LIMIT 1",
        (conversation_id, reason, offer_id),
    )
    return _handoff(row) if row else None


async def list_open_handoffs() -> list[Handoff]:
    rows = await get_db().fetchall(
        HANDOFF_SELECT + "WHERE status = 'open' AND (l.quote_id IS NULL OR "
        "l.revision=(SELECT max(revision) FROM quote_revisions WHERE quote_id=l.quote_id)) "
        "ORDER BY created_at DESC LIMIT 200"
    )
    return [_handoff(r).handoff for r in rows]


async def list_resolved_handoffs() -> list[Handoff]:
    rows = await get_db().fetchall(HANDOFF_SELECT + "WHERE status='resolved' ORDER BY updated_at DESC")
    return [_handoff(r).handoff for r in rows]


async def resolve_handoff(handoff_id: str, conn: aiosqlite.Connection | None = None) -> None:
    async with _tx(conn) as c:
        await c.execute("UPDATE handoffs SET status = 'resolved', updated_at = ? WHERE id = ?", (now_iso(), handoff_id))


# --- Idempotency-Key ------------------------------------------------------------------


@dataclass(frozen=True)
class StoredResult:
    request_hash: str
    status: int
    body: Any


async def get_idempotent(principal: str, method: str, path: str, key: str) -> StoredResult | None:
    row = await get_db().fetchone(
        "SELECT request_hash, status, body FROM idempotency "
        "WHERE principal = ? AND method = ? AND path = ? AND key = ?",
        (principal, method, path, key),
    )
    return StoredResult(row["request_hash"], row["status"], json.loads(row["body"])) if row else None


async def save_idempotent(
    principal: str, method: str, path: str, key: str, result: StoredResult, conn: aiosqlite.Connection | None = None
) -> None:
    async with _tx(conn) as conn:
        await conn.execute(
            "INSERT INTO idempotency(principal, method, path, key, request_hash, status, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                principal,
                method,
                path,
                key,
                result.request_hash,
                result.status,
                json.dumps(result.body, ensure_ascii=False),
                now_iso(),
            ),
        )


@dataclass
class CommandReceipt:
    identity: tuple[str, str, str, str]
    request_hash: str
    saved: bool = False

    async def save(self, status, payload, conn=None):
        await save_idempotent(*self.identity, StoredResult(self.request_hash, status, payload), conn=conn)
        self.saved = True


active_command: ContextVar[CommandReceipt | None] = ContextVar("active_command", default=None)


async def record_quote_response(conn, status, payload):
    if command := active_command.get():
        await command.save(status, payload, conn)
    return status, payload


# --- Ходы агента ---------------------------------------------------------------------


async def save_turn(turn: AgentTurn) -> None:
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO turns(turn_id, session_id, steps, decision, escalation_reason, "
            "total_cost_rub, total_latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn.turn_id,
                turn.session_id,
                json.dumps([s.model_dump() for s in turn.steps], ensure_ascii=False),
                turn.decision,
                turn.escalation_reason,
                turn.total_cost_rub,
                turn.total_latency_ms,
                now_iso(),
            ),
        )


async def list_turns(session_id: str) -> list[AgentTurn]:
    rows = await get_db().fetchall("SELECT * FROM turns WHERE session_id = ? ORDER BY created_at", (session_id,))
    return [
        AgentTurn(
            turn_id=r["turn_id"],
            session_id=r["session_id"],
            steps=json.loads(r["steps"]),
            decision=r["decision"],
            escalation_reason=r["escalation_reason"],
            total_cost_rub=r["total_cost_rub"],
            total_latency_ms=r["total_latency_ms"],
        )
        for r in rows
    ]


# --- Черновики pilot -------------------------------------------------------------------


async def sent_draft_actions(conversation_id: str) -> set[str]:
    rows = await get_db().fetchall(
        "SELECT action_id FROM drafts WHERE conversation_id=? AND status='sent'", (conversation_id,)
    )
    return {row["action_id"] for row in rows}


async def visible_turns(conversation_id: str) -> list[AgentTurn]:
    """Only restore decisions that have actually reached this buyer."""
    turns = await list_turns(conversation_id)
    if not any(t.decision == "draft" for t in turns):
        return turns
    sent = await sent_draft_actions(conversation_id)
    return [t for t in turns if t.decision != "draft" or any(s.args.get("journal_action_id") in sent for s in t.steps)]


def _draft(row: aiosqlite.Row) -> Draft:
    return Draft(
        id=row["id"],
        conversation_id=row["conversation_id"],
        action_id=row["action_id"],
        events=json.loads(row["events"]),
        status=row["status"],
        created_at=row["created_at"],
    )


async def create_draft(
    conversation_id: str, action_id: str, events: list[dict[str, Any]], conn: aiosqlite.Connection | None = None
) -> Draft:
    draft = Draft(
        id=new_id("d"),
        conversation_id=conversation_id,
        action_id=action_id,
        events=events,
        status="pending",
        created_at=now_iso(),
    )
    ts = to_iso(draft.created_at)
    async with _tx(conn) as c:
        await c.execute(
            "INSERT INTO drafts(id, conversation_id, action_id, events, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (draft.id, conversation_id, action_id, json.dumps(events, ensure_ascii=False), ts, ts),
        )
    return draft


async def get_draft(draft_id: str) -> Draft | None:
    row = await get_db().fetchone("SELECT * FROM drafts WHERE id = ?", (draft_id,))
    return _draft(row) if row else None


async def list_drafts(status: str | None = "pending") -> list[Draft]:
    if status:
        rows = await get_db().fetchall("SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC", (status,))
    else:
        rows = await get_db().fetchall("SELECT * FROM drafts ORDER BY created_at DESC")
    return [_draft(r) for r in rows]


async def mark_draft_sent(draft_id: str, conn: aiosqlite.Connection | None = None) -> None:
    async with _tx(conn) as c:
        await c.execute("UPDATE drafts SET status = 'sent', updated_at = ? WHERE id = ?", (now_iso(), draft_id))


# --- Патчи базы знаний -------------------------------------------------------------


def _patch(row: aiosqlite.Row) -> KBPatch:
    return KBPatch(**{k: row[k] for k in row.keys()})


async def create_kb_patch(
    question: str, answer: str, handoff_id: str | None = None, scope: str = "general", phrasings: tuple[str, ...] = ()
) -> KBPatch:
    patch = KBPatch(
        id=new_id("p"),
        handoff_id=handoff_id,
        question=question,
        answer=answer,
        scope=scope,
        status="proposed",
        created_at=now_iso(),
    )
    async with get_db().tx() as conn:
        if scope == "public_faq":
            if not handoff_id or not await _fetchone(conn, "SELECT id FROM handoffs WHERE id=?", (handoff_id,)):
                raise invalid_input("Для публичной статьи выберите исходное обращение.")
            if not question.strip() or not answer.strip():
                raise invalid_input("Заполните общий вопрос и ответ.")
        await conn.execute(
            "INSERT INTO kb_patches(id, handoff_id, question, answer, scope, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patch.id, handoff_id, question, answer, scope, "proposed", to_iso(patch.created_at)),
        )
        await conn.executemany(
            "INSERT INTO kb_phrasings(patch_id,question) VALUES (?,?)",
            [(patch.id, q) for q in dict.fromkeys(q.strip() for q in phrasings) if q],
        )
    return patch


async def get_kb_patch(patch_id: str) -> KBPatch | None:
    row = await get_db().fetchone("SELECT * FROM kb_patches WHERE id = ?", (patch_id,))
    return _patch(row) if row else None


async def list_kb_patches(status: PatchStatus | None = None) -> list[KBPatch]:
    if status:
        rows = await get_db().fetchall("SELECT * FROM kb_patches WHERE status = ? ORDER BY created_at DESC", (status,))
    else:
        rows = await get_db().fetchall("SELECT * FROM kb_patches ORDER BY created_at DESC")
    return [_patch(r) for r in rows]


async def publication_history() -> list[dict]:
    async with get_db().committed_read() as conn:
        rows = await conn.execute_fetchall("""SELECT p.id,p.handoff_id,p.question,p.answer,p.approved_by,
            CASE WHEN p.status='approved' AND r.patch_id IS NULL
                 AND NOT EXISTS (SELECT 1 FROM kb_public_snapshots s WHERE s.id=p.id)
                 THEN 'proposed' ELSE p.status END AS status,
            r.decided_at AS retired_at,r.decided_by,r.replacement_id FROM kb_patches p
            LEFT JOIN kb_retirements r ON r.patch_id=p.id WHERE p.scope='public_faq' ORDER BY p.created_at DESC""")
        variants = await conn.execute_fetchall("SELECT patch_id,question FROM kb_phrasings ORDER BY rowid")
    by_id: dict[str, list[str]] = {}
    for v in variants:
        by_id.setdefault(v["patch_id"], []).append(v["question"])
    return [dict(row, phrasings=by_id.get(row["id"], [])) for row in rows]


async def live_article(conn: aiosqlite.Connection, patch_id: str) -> aiosqlite.Row | None:
    return await _fetchone(conn, "SELECT * FROM kb_live_publications WHERE id=?", (patch_id,))


async def _retire_patch(conn: aiosqlite.Connection, patch_id: str, manager: str, replacement: str | None) -> None:
    active = await live_article(conn, patch_id)
    if not active or patch_id == replacement:
        raise ApiError(409, "stale_revision", "Эта версия статьи уже снята или недоступна для замены.")
    await conn.execute("INSERT INTO kb_retirements VALUES (?,?,?,?)", (patch_id, replacement, manager, now_iso()))


async def withdraw_kb_patch(patch_id: str, manager: str) -> None:
    async with get_db().tx() as conn:
        await _retire_patch(conn, patch_id, manager, None)


async def set_kb_patch_status(
    patch_id: str, status: PatchStatus, approved_by: str, replaces: str | None = None
) -> KBPatch:
    """Serialize moderation, publication and optional withdrawal of the previous version."""
    async with get_db().tx() as conn:
        row = await _fetchone(conn, "SELECT * FROM kb_patches WHERE id=?", (patch_id,))
        if row is None:
            raise not_found("Черновик не найден.")
        # Older versions could report approval before the case closed. Such a
        # patch never had a public snapshot. An explicit manager decision can recover it.
        premature = (
            row["status"] == "approved"
            and row["scope"] == "public_faq"
            and not await _fetchone(
                conn,
                "SELECT id FROM kb_public_snapshots WHERE id=? "
                "UNION SELECT patch_id FROM kb_retirements WHERE patch_id=?",
                (patch_id, patch_id),
            )
        )
        if row["status"] != "proposed" and not premature:
            raise ApiError(409, "already_resolved", "По этому черновику уже принято решение.")
        if status == "approved" and row["scope"] == "public_faq":
            case = await _fetchone(conn, "SELECT status FROM handoffs WHERE id=?", (row["handoff_id"],))
            if not case or case["status"] != "resolved":
                raise ApiError(409, "terms_pending", "Сначала закройте исходное обращение.")
            if not row["question"].strip() or not row["answer"].strip():
                raise invalid_input("Заполните общий вопрос и ответ.")
        if replaces:
            if status != "approved" or row["scope"] != "public_faq":
                raise invalid_input("Заменить статью можно только при публикации общего правила.")
            await _retire_patch(conn, replaces, approved_by, patch_id)
        await conn.execute(
            "UPDATE kb_patches SET status = ?, approved_by = ? WHERE id = ?", (status, approved_by, patch_id)
        )
        if status == "approved":
            cur = await conn.execute(
                "INSERT INTO kb_articles(url, title, section, body, origin, created_at) "
                "VALUES (?, ?, ?, ?, 'patch', ?)",
                (f"kb-patch:{patch_id}", row["question"], row["scope"], row["answer"], now_iso()),
            )
            await conn.execute(
                "INSERT INTO kb_fts(rowid, title, body) VALUES (?, ?, ?)",
                (cur.lastrowid, row["question"], row["answer"]),
            )
        return _patch(row).model_copy(update={"status": status, "approved_by": approved_by})


# --- LLM-вызовы и телеметрия -----------------------------------------------------------


async def log_llm_call(call: LLMCall) -> int:
    async with get_db().tx() as conn:
        cur = await conn.execute(
            "INSERT INTO llm_calls(ts, session_id, step, model, tokens_in, tokens_out, cost_rub, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(call.ts),
                call.session_id,
                call.step,
                call.model,
                call.tokens_in,
                call.tokens_out,
                call.cost_rub,
                call.latency_ms,
            ),
        )
        return cur.lastrowid


async def spend_since(since: datetime) -> float:
    value = await get_db().fetchval("SELECT sum(cost_rub) FROM llm_calls WHERE ts >= ?", (to_iso(since),))
    return float(value or 0.0)


async def step_stats(since: datetime | None = None) -> list[StepStats]:
    sql = (
        "SELECT step, count(*) AS calls, avg(tokens_in) AS ti, avg(tokens_out) AS tout, avg(cost_rub) AS ac, "
        "sum(cost_rub) AS tc, avg(latency_ms) AS al FROM llm_calls"
    )
    params: tuple = ()
    if since:
        sql += " WHERE ts >= ?"
        params = (to_iso(since),)
    rows = await get_db().fetchall(sql + " GROUP BY step ORDER BY tc DESC", params)
    return [
        StepStats(
            step=r["step"],
            calls=r["calls"],
            avg_tokens_in=round(r["ti"], 1),
            avg_tokens_out=round(r["tout"], 1),
            avg_cost_rub=round(r["ac"], 6),
            total_cost_rub=round(r["tc"], 6),
            avg_latency_ms=round(r["al"], 1),
        )
        for r in rows
    ]


async def recent_llm_calls(limit: int = 20) -> list[LLMCall]:
    rows = await get_db().fetchall("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))
    return [LLMCall(**{k: r[k] for k in r.keys()}) for r in rows]


# --- Счётчики для /health -----------------------------------------------------------------


async def count_offers() -> int:
    return await get_db().fetchval("SELECT count(*) FROM offers")


async def count_kb_articles() -> int:
    return await get_db().fetchval("SELECT count(*) FROM kb_articles")
