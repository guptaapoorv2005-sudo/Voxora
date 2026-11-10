import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set a dummy DATABASE_URL for tests if not provided, so Settings can load without failing
if "DATABASE_URL" not in os.environ and "TEST_DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://dummy:dummy@localhost:5432/dummy"

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Provides an AsyncClient bound to the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
