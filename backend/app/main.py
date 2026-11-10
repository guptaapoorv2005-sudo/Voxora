import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routes_health import router as health_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import dispose_engine, get_session_factory

logger = logging.getLogger("voxora.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging()
    logger.info(
        f"Starting Voxora Backend in '{settings.environment}' environment "
        f"(db_echo={settings.db_echo})"
    )

    # Validate database connection on startup
    session_factory = get_session_factory(settings)
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        logger.info("Database connectivity established successfully.")
    except Exception as exc:
        logger.error(
            f"Failed to connect to database on startup: {exc}. "
            "Please check DATABASE_URL."
        )
        # In development/test or if DB is offline, continue starting
        # so the health endpoint can report degraded state.

    yield

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)

    return app


app = create_app()
