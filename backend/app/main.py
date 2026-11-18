import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.routes_appointments import router as appointments_router
from app.api.routes_auth import router as auth_router
from app.api.routes_business import router as business_router
from app.api.routes_google_calendar import router as google_calendar_router
from app.api.routes_health import router as health_router
from app.api.routes_owner import router as owner_router
from app.api.routes_session import router as session_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import dispose_engine, get_session_factory
from app.services.google_calendar import calendar_sync_loop

logger = logging.getLogger("voxora.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging()
    logger.info(
        f"Starting Voxora Backend in '{settings.environment}' environment "
        f"(db_echo={settings.db_echo})"
    )
    logger.info(
        "FastAPI configured with effective agent dispatch name: '%s'",
        settings.effective_agent_name,
    )

    # Validate database connection on startup
    session_factory = get_session_factory(settings)
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        logger.info("Database connectivity established successfully.")
    except Exception as exc:
        logger.error(f"Failed to connect to database on startup: {exc}. Please check DATABASE_URL.")
        # In development/test or if DB is offline, continue starting
        # so the health endpoint can report degraded state.

    calendar_task = None
    if all((settings.google_calendar_client_id, settings.google_calendar_client_secret,
            settings.google_calendar_redirect_uri, settings.google_calendar_encryption_key)):
        calendar_task = asyncio.create_task(calendar_sync_loop())

    yield

    if calendar_task:
        calendar_task.cancel()
        try:
            await calendar_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down Voxora Backend...")
    await dispose_engine()
    logger.info("Engine disposed.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Voxora API",
        version="0.1.0",
        description="Voxora AI Voice Receptionist REST API",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def owner_no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(("/api/owner", "/api/auth")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(business_router)
    app.include_router(appointments_router)
    app.include_router(auth_router)
    app.include_router(google_calendar_router)
    app.include_router(owner_router)

    return app


app = create_app()
