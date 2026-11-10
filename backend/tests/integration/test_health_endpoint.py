import os

import pytest
from httpx import AsyncClient

_target_db = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
_has_live_db = bool(
    _target_db
    and "dummy:dummy" not in _target_db
    and "user:password" not in _target_db
)


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_live_db,
    reason="No live Neon database configured (TEST_DATABASE_URL / DATABASE_URL missing or dummy)",
)
async def test_health_endpoint_integration(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert "environment" in data
