"""FastAPI application factory."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.api.system import router as system_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.db import models  # noqa: F401  (register tables)
from app.db.init_db import seed_users
from app.db.session import SessionLocal
from app.internal.router import router as internal_router
from app.workers.scheduler import Scheduler


def create_app() -> FastAPI:
    settings = get_settings()
    scheduler = Scheduler(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging(settings.log_level, settings.log_dir)
        settings.ensure_dirs()
        logger = structlog.get_logger("startup")

        if settings.app_env.lower() != "test":
            # Alembic owns the schema. Startup only seeds the role accounts.
            with SessionLocal() as session:
                created = seed_users(session)
            logger.info(
                "startup_complete",
                env=settings.app_env,
                users_created=created,
                dry_run=settings.email_dry_run,
            )
        scheduler.start()
        yield
        scheduler.stop()
        logger.info("shutdown_complete")

    app = FastAPI(
        title="HR AI Agent System",
        version="0.1.0",
        description=(
            "Backend for the three-agent recruitment workflow. "
            "No candidate email is sent without HR approval."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            settings.frontend_origin,
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8001",
            "http://localhost:8001",
        ],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = request_id
        return response

    register_exception_handlers(app)
    app.include_router(system_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    # Internal Kestra routes are intentionally hidden from the public schema.
    app.include_router(internal_router, include_in_schema=False)

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/dashboard", StaticFiles(directory=str(frontend_dist), html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        async def root_redirect():
            return RedirectResponse(url="/dashboard/")

    return app


app = create_app()
