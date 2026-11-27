"""Чтение индекса каталога T06 (catalog/index.py) и метаданных снимка, которые пишет обновление фида T14.

Индекс открывается только на чтение и на каждый вызов: после атомарной подмены файла
следующий запрос уже видит новый снимок.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from app.services.price_policy import OfferSnapshot

SNAPSHOT_TABLE = """
CREATE TABLE feed_snapshot(
    feed_date TEXT NOT NULL, feed_sha256 TEXT NOT NULL, feed_name TEXT NOT NULL, feed_bytes INTEGER NOT NULL,
    offers_count INTEGER NOT NULL, listed INTEGER NOT NULL, availability_check INTEGER NOT NULL,
    on_request INTEGER NOT NULL, refreshed_at TEXT NOT NULL
)
"""
TERM_PARAM = "Срок действия"


@contextmanager
def open_index(index: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        yield db


def read_snapshot(index: Path) -> dict | None:
    """Метаданные снимка; None — индекса нет или он собран без refresh_feed."""
    if not index.is_file():
        return None
    with open_index(index) as db:
        has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feed_snapshot'").fetchone()
        row = db.execute("SELECT * FROM feed_snapshot").fetchone() if has_table else None
    return dict(row) if row else None


def read_offer(index: Path, offer_id: str) -> OfferSnapshot | None:
    with open_index(index) as db:
        row = db.execute(
            "SELECT id,name,source,available,price,currency,details FROM offers WHERE id=?", (offer_id,)
        ).fetchone()
        date = db.execute("SELECT snapshot FROM metadata").fetchone()
    if row is None:
        return None
    raw_params = json.loads(row["details"]).get("raw_params", {})
    return OfferSnapshot(
        offer_id=row["id"],
        name=row["name"],
        url=row["source"],
        available=None if row["available"] is None else bool(row["available"]),
        price=row["price"],
        currency=row["currency"] or None,
        license_terms=tuple(raw_params.get(TERM_PARAM, ())),
        snapshot_date=date["snapshot"] if date else None,
    )
