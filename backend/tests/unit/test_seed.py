"""Unit tests for BrightSmile seed routine.

Verifies:
1. Seed idempotency (skips cleanly when fully seeded).
2. Incomplete seed detection (raises error if partial records exist).
3. Transactional atomicity (rolls back on failure).
4. Zero owner account insertion (deferred to Phase 7).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.clinic import BusinessProfile
from app.db.models.owner import OwnerUser
from app.seed.brightsmile import IncompleteSeedError, seed_brightsmile


class FakeTransactionContext:
    """Async context manager replicating `async with session.begin():` behavior."""

    def __init__(self, session: AsyncMock) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        tb: object,
    ) -> None:
        if exc_val is not None:
            await self.session.rollback()


@pytest.mark.asyncio
async def test_seed_idempotency_skip() -> None:
    mock_session = AsyncMock()
    mock_session.in_transaction.return_value = False
    mock_session.begin = MagicMock(side_effect=lambda: FakeTransactionContext(mock_session))

    mock_profile = BusinessProfile(id=1, slug="brightsmile", name="BrightSmile Dental Clinic")

    # Mock profile lookup
    mock_profile_res = MagicMock()
    mock_profile_res.scalar_one_or_none.return_value = mock_profile

    # Mock count queries for child records: 7 hours, 4 services, 3 closures, 2 policies, 3 faqs
    mock_counts = [MagicMock(scalar_one=MagicMock(return_value=count)) for count in (7, 4, 3, 2, 3)]
    mock_session.execute.side_effect = [mock_profile_res] + mock_counts

    seeded = await seed_brightsmile(mock_session)
    assert seeded is False
    mock_session.add.assert_not_called()
    mock_session.add_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counts", "missing_entity"),
    [
        ((3, 4, 3, 2, 3), "hours=3/7"),
        ((7, 2, 3, 2, 3), "services=2/4"),
        ((7, 4, 1, 2, 3), "closures=1/3"),
        ((7, 4, 3, 1, 3), "policies=1/2"),
        ((7, 4, 3, 2, 1), "faqs=1/3"),
    ],
)
async def test_seed_incomplete_detection_for_all_entities(
    counts: tuple[int, int, int, int, int],
    missing_entity: str,
) -> None:
    """Verify that incomplete seed detection catches missing records across all child tables."""
    mock_session = AsyncMock()
    mock_session.in_transaction.return_value = False
    mock_session.begin = MagicMock(side_effect=lambda: FakeTransactionContext(mock_session))

    mock_profile = BusinessProfile(id=1, slug="brightsmile", name="BrightSmile Dental Clinic")
    mock_profile_res = MagicMock()
    mock_profile_res.scalar_one_or_none.return_value = mock_profile

    mock_counts = [MagicMock(scalar_one=MagicMock(return_value=c)) for c in counts]
    mock_session.execute.side_effect = [mock_profile_res] + mock_counts

    with pytest.raises(IncompleteSeedError, match=missing_entity):
        await seed_brightsmile(mock_session)


@pytest.mark.asyncio
async def test_seed_participates_in_existing_transaction() -> None:
    """Regression test: seed_brightsmile does not call session.begin() if in transaction."""
    mock_session = AsyncMock()
    mock_session.in_transaction.return_value = True
    mock_session.begin = MagicMock()
    mock_session.add = MagicMock()
    mock_session.add_all = MagicMock()

    mock_profile_res = MagicMock()
    mock_profile_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_profile_res

    # When flush is called, assign an ID to profile
    async def fake_flush() -> None:
        if mock_session.add.call_args:
            added_obj = mock_session.add.call_args[0][0]
            if isinstance(added_obj, BusinessProfile):
                added_obj.id = 10

    mock_session.flush.side_effect = fake_flush

    seeded = await seed_brightsmile(mock_session)
    assert seeded is True
    # Crucial assertion: session.begin() was NOT called because transaction was already active
    mock_session.begin.assert_not_called()
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_seed_inserts_canonical_records_when_new() -> None:
    mock_session = AsyncMock()
    mock_session.in_transaction = MagicMock(return_value=False)
    mock_session.add = MagicMock()
    mock_session.add_all = MagicMock()
    mock_session.begin = MagicMock(side_effect=lambda: FakeTransactionContext(mock_session))

    # Profile does not exist
    mock_profile_res = MagicMock()
    mock_profile_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_profile_res

    # When flush is called, assign an ID to profile
    async def fake_flush() -> None:
        if mock_session.add.call_args:
            added_obj = mock_session.add.call_args[0][0]
            if isinstance(added_obj, BusinessProfile):
                added_obj.id = 10

    mock_session.flush.side_effect = fake_flush

    seeded = await seed_brightsmile(mock_session)
    assert seeded is True

    # BusinessProfile added
    mock_session.add.assert_called_once()
    added_profile = mock_session.add.call_args[0][0]
    assert isinstance(added_profile, BusinessProfile)
    assert added_profile.slug == "brightsmile"
    assert added_profile.phone is None  # Deliberate safety deviation verified!

    # 5 child collections added: hours, closures, services, policies, faqs
    assert mock_session.add_all.call_count == 5

    # Crucial check: OwnerUser was NEVER added
    for call in mock_session.add_all.call_args_list:
        for item in call[0][0]:
            assert not isinstance(item, OwnerUser)


@pytest.mark.asyncio
async def test_seed_transactional_rollback() -> None:
    mock_session = AsyncMock()
    mock_session.in_transaction = MagicMock(return_value=False)
    mock_session.add = MagicMock()
    mock_session.add_all = MagicMock()
    mock_session.begin = MagicMock(side_effect=lambda: FakeTransactionContext(mock_session))

    # Profile does not exist
    mock_profile_res = MagicMock()
    mock_profile_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_profile_res

    # Simulate database error on flush
    mock_session.flush.side_effect = RuntimeError("PostgreSQL foreign key violation")

    with pytest.raises(RuntimeError, match="PostgreSQL foreign key violation"):
        await seed_brightsmile(mock_session)

    mock_session.rollback.assert_awaited_once()
