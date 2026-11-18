import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set dummy defaults for tests if not provided, so AppSettings can load without failing
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://dummy:dummy@localhost:5432/dummy"
if "LIVEKIT_URL" not in os.environ:
    os.environ["LIVEKIT_URL"] = "wss://dummy.livekit.cloud"
if "LIVEKIT_API_KEY" not in os.environ:
    os.environ["LIVEKIT_API_KEY"] = "dummy-key"
if "LIVEKIT_API_SECRET" not in os.environ:
    os.environ["LIVEKIT_API_SECRET"] = "dummy-secret"

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Provides an AsyncClient bound to the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
