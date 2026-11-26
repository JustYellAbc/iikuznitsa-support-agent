"""CRUD поверх app.db.database. Вход и выход — модели из contracts/schemas.py."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from app.contracts.schemas import (
    AgentTurn,
    KBPatch,
    LLMCall,
    Message,
    Mode,
    PatchStatus,
    Quote,
    Role,
    Session,
    StepStats,
    Ticket,
    TicketStatus,
    TicketType,
)
from app.db.database import get_db


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# --- Настройки ---------------------------------------------------------------


async def get_setting(key: str) -> str | None:
    return await get_db().fetchval("SELECT value FROM app_settings WHERE key = ?", (key,))


async def set_setting(key: str, value: str) -> None:
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- Сессии и сообщения -------------------------------------------------------


def _session(row: aiosqlite.Row) -> Session:
    return Session(
        id=row["id"],
        client_context=json.loads(row["client_context"]),
        mode=row["mode"],
        created_at=row["created_at"],
    )


async def create_session(client_context: dict[str, Any], mode: Mode) -> Session:
    session = Session(id=new_id("s"), client_context=client_context, mode=mode, created_at=now_iso())
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO sessions(id, client_context, mode, created_at) VALUES (?, ?, ?, ?)",
            (session.id, json.dumps(client_context, ensure_ascii=False), mode, to_iso(session.created_at)),
        )
    return session


async def get_session(session_id: str) -> Session | None:
    row = await get_db().fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return _session(row) if row else None


async def set_session_mode(session_id: str, mode: Mode) -> None:
    async with get_db().tx() as conn:
        await conn.execute("UPDATE sessions SET mode = ? WHERE id = ?", (mode, session_id))


def _message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        text=row["text"],
        sources=json.loads(row["sources"]),
        quote_id=row["quote_id"],
        created_at=row["created_at"],
    )


async def add_message(
    session_id: str, role: Role, text: str, sources: list[str] | None = None, quote_id: str | None = None
) -> Message:
    msg = Message(
        id=new_id("m"),
        session_id=session_id,
        role=role,
        text=text,
        sources=sources or [],
        quote_id=quote_id,
        created_at=now_iso(),
    )
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO messages(id, session_id, role, text, sources, quote_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                msg.id,
                session_id,
                role,
                text,
                json.dumps(msg.sources, ensure_ascii=False),
                quote_id,
                to_iso(msg.created_at),
            ),
        )
    return msg


async def list_messages(session_id: str) -> list[Message]:
    rows = await get_db().fetchall(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
    )
    return [_message(r) for r in rows]


async def count_client_messages(session_id: str) -> int:
    return await get_db().fetchval(
        "SELECT count(*) FROM messages WHERE session_id = ? AND role = 'client'", (session_id,)
    )


# --- Ходы агента ---------------------------------------------------------------


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


# --- Тикеты ----------------------------------------------------------------------


def _ticket(row: aiosqlite.Row) -> Ticket:
    return Ticket(
        id=row["id"],
        session_id=row["session_id"],
        type=row["type"],
        status=row["status"],
        reason=row["reason"],
        summary=row["summary"],
        collected_params=json.loads(row["collected_params"]),
        draft_answer=row["draft_answer"],
        operator_answer=row["operator_answer"],
        suggested_patch_id=row["suggested_patch_id"],
        created_at=row["created_at"],
    )


async def create_ticket(
    session_id: str,
    type: TicketType,
    reason: str,
    summary: str,
    collected_params: dict[str, Any] | None = None,
    draft_answer: str | None = None,
) -> Ticket:
    ticket = Ticket(
        id=new_id("t"),
        session_id=session_id,
        type=type,
        status="open",
        reason=reason,
        summary=summary,
        collected_params=collected_params or {},
        draft_answer=draft_answer,
        created_at=now_iso(),
    )
    ts = to_iso(ticket.created_at)
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO tickets(id, session_id, type, status, reason, summary, collected_params, "
            "draft_answer, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket.id,
                session_id,
                type,
                "open",
                reason,
                summary,
                json.dumps(ticket.collected_params, ensure_ascii=False),
                draft_answer,
                ts,
                ts,
            ),
        )
    return ticket


async def get_ticket(ticket_id: str) -> Ticket | None:
    row = await get_db().fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    return _ticket(row) if row else None


async def find_open_ticket(session_id: str, type: TicketType) -> Ticket | None:
    row = await get_db().fetchone(
        "SELECT * FROM tickets WHERE session_id = ? AND type = ? AND status != 'resolved' "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id, type),
    )
    return _ticket(row) if row else None


async def list_tickets(
    status: TicketStatus | None = None, since: datetime | None = None, session_id: str | None = None
) -> list[Ticket]:
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if since:
        where.append("updated_at >= ?")
        params.append(to_iso(since))
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    sql = "SELECT * FROM tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = await get_db().fetchall(sql + " ORDER BY created_at DESC LIMIT 200", tuple(params))
    return [_ticket(r) for r in rows]


_TICKET_FIELDS = {
    "status",
    "reason",
    "summary",
    "collected_params",
    "draft_answer",
    "operator_answer",
    "suggested_patch_id",
}


async def update_ticket(ticket_id: str, **fields: Any) -> Ticket | None:
    unknown = set(fields) - _TICKET_FIELDS
    if unknown:
        raise ValueError(f"Unknown ticket fields: {unknown}")
    if "collected_params" in fields:
        fields["collected_params"] = json.dumps(fields["collected_params"], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    async with get_db().tx() as conn:
        await conn.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", (*fields.values(), ticket_id))
    return await get_ticket(ticket_id)


# --- Патчи базы знаний -------------------------------------------------------------


def _patch(row: aiosqlite.Row) -> KBPatch:
    return KBPatch(**{k: row[k] for k in row.keys()})


async def create_kb_patch(question: str, answer: str, ticket_id: str | None = None, scope: str = "general") -> KBPatch:
    patch = KBPatch(
        id=new_id("p"),
        ticket_id=ticket_id,
        question=question,
        answer=answer,
        scope=scope,
        status="proposed",
        created_at=now_iso(),
    )
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO kb_patches(id, ticket_id, question, answer, scope, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patch.id, ticket_id, question, answer, scope, "proposed", to_iso(patch.created_at)),
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


async def set_kb_patch_status(patch_id: str, status: PatchStatus, approved_by: str | None) -> KBPatch | None:
    """Одобрение сразу публикует патч в kb_articles + kb_fts (живое обновление поиска)."""
    async with get_db().tx() as conn:
        await conn.execute(
            "UPDATE kb_patches SET status = ?, approved_by = ? WHERE id = ?", (status, approved_by, patch_id)
        )
        if status == "approved":
            async with conn.execute("SELECT question, answer, scope FROM kb_patches WHERE id = ?", (patch_id,)) as cur:
                row = await cur.fetchone()
            if row:
                cur = await conn.execute(
                    "INSERT INTO kb_articles(url, title, section, body, origin, created_at) "
                    "VALUES (?, ?, ?, ?, 'patch', ?)",
                    (f"kb-patch:{patch_id}", row["question"], row["scope"], row["answer"], now_iso()),
                )
                await conn.execute(
                    "INSERT INTO kb_fts(rowid, title, body) VALUES (?, ?, ?)",
                    (cur.lastrowid, row["question"], row["answer"]),
                )
    return await get_kb_patch(patch_id)


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


# --- КП ---------------------------------------------------------------------------------


async def save_quote(quote: Quote, pdf_path: str | None = None) -> None:
    async with get_db().tx() as conn:
        await conn.execute(
            "INSERT INTO quotes(id, session_id, version, status, data, pdf_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "status = excluded.status, data = excluded.data, pdf_path = excluded.pdf_path",
            (quote.id, quote.session_id, quote.version, quote.status, quote.model_dump_json(), pdf_path, now_iso()),
        )


async def get_quote(quote_id: str) -> tuple[Quote, str | None] | None:
    row = await get_db().fetchone("SELECT data, pdf_path FROM quotes WHERE id = ?", (quote_id,))
    if row is None:
        return None
    return Quote.model_validate_json(row["data"]), row["pdf_path"]


# --- Счётчики для /health -----------------------------------------------------------------


async def count_offers() -> int:
    return await get_db().fetchval("SELECT count(*) FROM offers")


async def count_kb_articles() -> int:
    return await get_db().fetchval("SELECT count(*) FROM kb_articles")
