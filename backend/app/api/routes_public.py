import asyncio
import sqlite3

from fastapi import APIRouter

from app.config import get_settings
from app.contracts.schemas import Health
from app.core.llm import today_spend_rub
from app.db import repository
from app.services.catalog_snapshot import read_snapshot

router = APIRouter()


@router.get("/health", response_model=Health, tags=["T05 расширения"])
async def health() -> Health:
    """Публичная проверка работоспособности без входа. offers_count — SKU рабочего снимка каталога."""
    try:
        snapshot = await asyncio.to_thread(read_snapshot, get_settings().catalog_index_path)
    except sqlite3.Error:
        snapshot = None  # битый индекс каталога не валит проверку стенда; детали в /api/v1/manager/catalog
    return Health(
        status="ok",
        offers_count=snapshot["offers_count"] if snapshot else await repository.count_offers(),
        kb_count=await repository.count_kb_articles(),
        today_spend_rub=round(await today_spend_rub(), 4),
    )
