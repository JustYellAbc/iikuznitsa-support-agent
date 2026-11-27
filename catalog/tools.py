"""Read-only T31 tools. The caller supplies the index path, never the model."""

import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from catalog.index import search, source_version

from app.agent.knowledge import ARTICLE_PATH, question_key
from app.config import get_settings
from app.contracts.schemas import ToolResult
from app.db.database import get_db
from app.services.catalog_snapshot import open_index
from app.services.price_policy import UNDEFINED_TERMS, assess, snapshot_from_row


def _results(items: list[dict]) -> ToolResult:
    return ToolResult(ok=True, data=items, source_ref=items[0]["source"] if items else None)


def _public_link(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname)


def _offer(row, version: str, *, detail: bool) -> dict:
    snapshot = snapshot_from_row(row, version)
    policy = assess(snapshot)
    item = {
        "offerId": snapshot.offer_id,
        "name": snapshot.name,
        "source": snapshot.url,
        "version": version,
        "available": snapshot.available,
        "priceState": policy.price_state,
        "unitPriceKopecks": policy.unit_price_kopecks if policy.show_price else None,
        "priceCaveat": policy.price_caveat,
        "licenseTerm": policy.license_term,
    }
    params = row["params"]
    if isinstance(params, str):
        params = json.loads(params)
    unknown = UNDEFINED_TERMS | {"n/a"}

    def values(key):
        return [v.strip() for v in params.get(key, []) if v.strip().casefold() not in unknown] or None

    details = row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    item["salesNotes"] = details.get("sales_notes") or None
    item["seatLimit"] = values("К-во пользователей до")
    item["licenseType"] = values("Тип лицензии")
    if detail:
        item.update(
            groupId=row["group_id"] if "group_id" in row.keys() else None,
            edition=" / ".join(values("Версия") or []) or None,
            platform=values("Платформа"),
            delivery=values("Способ доставки"),
            buyer=values("Пользователь"),
            organizationType=values("Тип организации"),
        )
    return item


def search_catalog(
    index: Path,
    query: str,
    *,
    by_need: bool = False,
    limit: int = 3,
    license_type: str | None = None,
    detail: bool = False,
) -> ToolResult:
    """Retrieve candidates using a product/family, category or task keywords."""
    if not 1 <= limit <= 5 or len(query) > 500:
        return ToolResult(ok=False, error="invalid_arguments")
    try:
        rows = search(index, query, limit=limit, license_type=license_type, include_snapshot=True, by_need=by_need)
    except sqlite3.Error:
        return ToolResult(ok=False, error="catalog_unavailable")
    return _results([_offer(row, row["snapshot"], detail=detail) for row in rows if _public_link(row["source"])])


def get_offer(index: Path, offer_id: str) -> ToolResult:
    """Exact offer ID, including non-numeric IDs. Never substitute a different pack."""
    try:
        with open_index(index) as db:
            version = source_version(db)
            row = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    except sqlite3.Error:
        return ToolResult(ok=False, error="catalog_unavailable")
    if row is None or not _public_link(row["source"]):
        return _results([])
    return _results([_offer(row, version, detail=True)])


def recalled_offers(index: Path, sources: list[str]) -> ToolResult:
    """Resolve citations from an earlier answer locally; trust only an exact source match."""
    rows = []
    for url in dict.fromkeys(sources[:3]):
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        offer_id = slug.rsplit("-", 1)[-1]
        if not offer_id.isdecimal():
            continue
        hit = get_offer(index, offer_id)
        if hit.ok:
            rows.extend(row for row in hit.data if row["source"] == url)
    return _results(rows)


def search_store_knowledge(index: Path, query: str, *, limit: int = 3) -> ToolResult:
    """Return short articles from the indexed store help pages."""
    if not 1 <= limit <= 5 or len(query) > 500:
        return ToolResult(ok=False, error="invalid_arguments")
    try:
        rows = search(index, query, scope="knowledge", limit=limit)
    except sqlite3.Error:
        return ToolResult(ok=False, error="catalog_unavailable")
    items = []
    for row in rows:
        if _public_link(row["source"]):
            version = hashlib.sha256(row["body"].encode()).hexdigest()
            items.append({**row, "version": version})
    return _results(items)


async def search_published_knowledge(query: str) -> ToolResult:
    """Match live manager publications and their approved phrasings (T15)."""
    if len(query) > 500:
        return ToolResult(ok=False, error="invalid_arguments")
    key = question_key(query)
    if not key:
        return _results([])
    async with get_db().committed_read() as db:
        rows = await db.execute_fetchall(
            "SELECT p.id, p.title, p.body, v.question FROM kb_live_publications p "
            "LEFT JOIN kb_phrasings v ON v.patch_id=p.id ORDER BY p.id"
        )
    matches = {
        row["id"]: row
        for row in rows
        if key == question_key(row["title"]) or (row["question"] and key == question_key(row["question"]))
    }
    if not matches:
        return _results([])
    if len({row["body"] for row in matches.values()}) > 1:
        return ToolResult(ok=False, error="conflicting_publications")
    row = next(iter(matches.values()))
    source = f"{str(get_settings().public_base_url).rstrip('/')}{ARTICLE_PATH}{row['id']}"
    return _results([{"title": row["title"], "body": row["body"], "source": source, "version": row["id"]}])
