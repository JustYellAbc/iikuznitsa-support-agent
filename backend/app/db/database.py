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

-- Диалоги и сессии входа (cookie support_session хранится только хэшем)
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'autopilot',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversations_owner ON conversations(owner_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Журнал T04: seq растёт на 1 внутри диалога
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    action_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    action_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    question TEXT NOT NULL,
    products TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_handoffs_status ON handoffs(status, created_at);
CREATE INDEX IF NOT EXISTS ix_handoffs_conversation ON handoffs(conversation_id);

CREATE TABLE IF NOT EXISTS quote_revisions (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    quote_id TEXT NOT NULL, revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
    action_id TEXT NOT NULL, document_id TEXT NOT NULL UNIQUE, pdf BLOB NOT NULL,
    issued_action TEXT, reselect INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, revision), UNIQUE (quote_id, revision)
);
CREATE TABLE IF NOT EXISTS quote_selection_resets (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
    after_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_invoices (
    quote_id TEXT NOT NULL, revision INTEGER NOT NULL,
    document_id TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    pdf BLOB,
    PRIMARY KEY (quote_id, revision),
    FOREIGN KEY (quote_id, revision) REFERENCES quote_revisions(quote_id, revision)
);
CREATE TABLE IF NOT EXISTS quote_handoffs (
    handoff_id TEXT PRIMARY KEY REFERENCES handoffs(id),
    quote_id TEXT NOT NULL, revision INTEGER NOT NULL, offer_id TEXT NOT NULL,
    FOREIGN KEY (quote_id, revision) REFERENCES quote_revisions(quote_id, revision)
);
CREATE TABLE IF NOT EXISTS quote_terms_reviews (
    handoff_id TEXT PRIMARY KEY REFERENCES handoffs(id),
    quote_id TEXT NOT NULL, revision INTEGER NOT NULL,
    UNIQUE (quote_id, revision),
    FOREIGN KEY (quote_id, revision) REFERENCES quote_revisions(quote_id, revision)
);

-- Idempotency-Key: результат принятой команды в области пользователя, метода и пути
CREATE TABLE IF NOT EXISTS idempotency (
    principal TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal, method, path, key)
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES conversations(id),
    steps TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL,
    escalation_reason TEXT,
    total_cost_rub REAL NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id, created_at);

-- Черновики режима pilot: события ждут отправки менеджером
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    action_id TEXT NOT NULL,
    events TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_patches (
    id TEXT PRIMARY KEY,
    handoff_id TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_phrasings (
    patch_id TEXT NOT NULL REFERENCES kb_patches(id),
    question TEXT NOT NULL,
    PRIMARY KEY (patch_id, question)
);
CREATE TABLE IF NOT EXISTS kb_retirements (
    patch_id TEXT PRIMARY KEY REFERENCES kb_patches(id),
    replacement_id TEXT REFERENCES kb_patches(id),
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
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

CREATE VIEW IF NOT EXISTS kb_public_snapshots AS
    SELECT p.id, a.title, a.body FROM kb_patches p
    JOIN kb_articles a ON a.url='kb-patch:' || p.id
    JOIN handoffs h ON h.id=p.handoff_id
    WHERE p.scope='public_faq' AND p.status='approved' AND p.approved_by IS NOT NULL
      AND a.origin='patch' AND a.section='public_faq'
      AND h.status='resolved' AND a.created_at >= h.updated_at
      AND length(trim(a.title)) > 0 AND length(trim(a.body)) > 0;
CREATE VIEW IF NOT EXISTS kb_live_publications AS
    SELECT s.* FROM kb_public_snapshots s
    WHERE NOT EXISTS (SELECT 1 FROM kb_retirements r WHERE r.patch_id=s.id);
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
        # Restore the selection boundary for conversations invalidated before it was stored explicitly.
        await self._conn.execute(
            """INSERT OR IGNORE INTO quote_selection_resets(conversation_id,after_seq)
               SELECT e.conversation_id,MAX(e.seq) FROM events e
               WHERE e.conversation_id IN (SELECT conversation_id FROM quote_revisions WHERE reselect=1
                                           EXCEPT SELECT conversation_id FROM quote_selection_resets)
                 AND json_extract(e.data,'$.author')='agent' AND json_extract(e.data,'$.purpose')='clarification'
                 AND json_extract(e.data,'$.text') LIKE ?
               GROUP BY e.conversation_id""",
            ("На % мест нужно заново подобрать варианты. Уточните товар, затем подтвердите новые позиции в форме КП.",),
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def committed_read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Readers sharing the writer connection must wait for a moderation transaction to finish."""
        async with self._lock:
            yield self.conn

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
