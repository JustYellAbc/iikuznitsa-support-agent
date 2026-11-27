import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent import knowledge
from app.api import routes_manager, routes_public, routes_v1
from app.config import get_settings
from app.core.errors import install_error_handlers
from app.core.llm import close_http_client
from app.core.security import RateLimitMiddleware
from app.db.database import close_db, init_db
from app.services import turn_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    s.quotes_dir.mkdir(parents=True, exist_ok=True)
    await init_db(s.db_path)
    try:
        yield
    finally:
        await turn_service.wait_idle()
        await close_http_client()
        await close_db()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Softline support agent API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    origins = s.cors_origin_list
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["Retry-After"],
    )
    install_error_handlers(app)
    app.include_router(routes_public.router)
    app.include_router(knowledge.router)
    app.include_router(routes_v1.router)
    app.include_router(routes_manager.router)
    return app


app = create_app()
