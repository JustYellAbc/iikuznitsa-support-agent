"""Async SQLite (aiosqlite) в режиме WAL: одно соединение на процесс, записи сериализуются локом."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    client_context TEXT NOT NULL DEFAULT '{}',
    mode TEXT NOT NULL DEFAULT 'autopilot',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    quote_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    steps TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL,
    escalation_reason TEXT,
    total_cost_rub REAL NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id, created_at);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    summary TEXT NOT NULL,
    collected_params TEXT NOT NULL DEFAULT '{}',
    draft_answer TEXT,
    operator_answer TEXT,
    suggested_patch_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status, created_at);

CREATE TABLE IF NOT EXISTS kb_patches (
    id TEXT PRIMARY KEY,
    ticket_id TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_rub REAL NOT NULL,
    latency_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_ts ON llm_calls(ts);

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    pdf_path TEXT,
    created_at TEXT NOT NULL
);

-- Каталог (заполняет scripts/ingest_feed.py)
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL,
    offers_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY,
    vendor_code TEXT,
    name TEXT NOT NULL,
    vendor TEXT,
    category_id TEXT,
    price REAL,
    has_price INTEGER NOT NULL DEFAULT 0,
    currency TEXT,
    available INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    description TEXT,
    params_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_offers_category ON offers(category_id);
CREATE INDEX IF NOT EXISTS ix_offers_vendor ON offers(vendor);

CREATE VIRTUAL TABLE IF NOT EXISTS offers_fts USING fts5(
    name, vendor, vendor_code, description,
    content='offers', tokenize='unicode61 remove_diacritics 2'
);

-- База знаний (scripts/ingest_kb.py + одобренные патчи)
CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    section TEXT,
    body TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'site',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    title, body,
    content='kb_articles', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция записи. Все INSERT/UPDATE — только внутри неё."""
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    async def fetchall(self, sql: str, params: tuple | dict = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: tuple | dict = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchval(self, sql: str, params: tuple | dict = ()) -> Any:
        row = await self.fetchone(sql, params)
        return None if row is None else row[0]


_db: Database | None = None


async def init_db(path: Path) -> Database:
    global _db
    await close_db()
    _db = Database(path)
    await _db.connect()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database is not initialised: call init_db() first")
    return _db
