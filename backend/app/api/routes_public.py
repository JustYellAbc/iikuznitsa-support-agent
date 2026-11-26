from fastapi import APIRouter

from app.contracts.schemas import Health
from app.core.llm import today_spend_rub
from app.db import repository

router = APIRouter()


@router.get("/health", response_model=Health, tags=["T05 расширения"])
async def health() -> Health:
    """Публичная проверка работоспособности без входа."""
    return Health(
        status="ok",
        offers_count=await repository.count_offers(),
        kb_count=await repository.count_kb_articles(),
        today_spend_rub=round(await today_spend_rub(), 4),
    )
