"""FastAPI application factory (ADR-003: single service).

Wires app-scoped singletons onto ``app.state`` in a lifespan, mounts routers under the API
prefix, and configures CORS for local dev. Migrations are NOT applied here — the request/
boot path only checks and warns (ADR-011).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .db import Database
from .migration_check import warn_if_behind_head
from .providers.registry import build_registry
from .routers import auth, health
from .services.auth_service import AuthService
from .services.rate_limit import RateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings

    db = Database(settings)
    await db.connect()
    app.state.db = db

    app.state.registry = build_registry(settings)
    app.state.auth_service = AuthService(db, settings)
    app.state.login_rate_limiter = RateLimiter(
        max_events=settings.login_rate_limit_per_min, window_seconds=60.0
    )

    await warn_if_behind_head(db)

    try:
        yield
    finally:
        await db.disconnect()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,  # cookies cross-origin in dev (web:5173 -> api:8000)
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # All endpoints live under /api/v1 (Caddy proxies /api -> FastAPI). 03-api.md.
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)

    return app


app = create_app()
