"""Repository queries and persistence operations for appointments, proposals, and challenges."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import (
    Appointment,
    BookingProposal,
    ConsentChallenge,
)


async def get_proposal_by_id(
    session: AsyncSession,
    proposal_id: UUID,
) -> BookingProposal | None:
    """Retrieve a booking proposal by ID without locking."""
    res = await session.execute(select(BookingProposal).where(BookingProposal.id == proposal_id))
    return res.scalar_one_or_none()


async def get_proposal_by_id_for_update(
    session: AsyncSession,
    proposal_id: UUID,
) -> BookingProposal | None:
    """Retrieve a booking proposal by ID with a Level 1 row-level lock (FOR UPDATE)."""
    res = await session.execute(
        select(BookingProposal).where(BookingProposal.id == proposal_id).with_for_update()
    )
    return res.scalar_one_or_none()


async def get_proposal_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> BookingProposal | None:
    """Retrieve a booking proposal by unique idempotency key."""
    res = await session.execute(
        select(BookingProposal).where(BookingProposal.idempotency_key == idempotency_key)
    )
    return res.scalar_one_or_none()


async def get_appointment_by_id(
    session: AsyncSession,
    appointment_id: int,
) -> Appointment | None:
    """Retrieve an appointment by ID without locking."""
    res = await session.execute(select(Appointment).where(Appointment.id == appointment_id))
    return res.scalar_one_or_none()


async def get_appointment_by_id_for_update(
    session: AsyncSession,
    appointment_id: int,
) -> Appointment | None:
    """Retrieve an appointment by ID with a Level 2 row-level lock (FOR UPDATE)."""
    res = await session.execute(
        select(Appointment).where(Appointment.id == appointment_id).with_for_update()
    )
    return res.scalar_one_or_none()


async def get_confirmed_appointments_by_phone_suffix(
    session: AsyncSession,
    clinic_id: int,
    phone_suffix: str,
) -> list[Appointment]:
    """Retrieve all confirmed appointments for a clinic whose caller_phone ends with the given suffix."""
    res = await session.execute(
        select(Appointment).where(
            Appointment.clinic_id == clinic_id,
            Appointment.status == "confirmed",
            Appointment.caller_phone.endswith(phone_suffix),
        )
    )
    return list(res.scalars().all())


async def get_active_consent_challenge(
    session: AsyncSession,
    session_id: str,
) -> ConsentChallenge | None:
    """Retrieve the current pending consent challenge for a voice session."""
    res = await session.execute(
        select(ConsentChallenge).where(
            ConsentChallenge.session_id == session_id,
            ConsentChallenge.status == "pending",
        )
    )
    return res.scalar_one_or_none()
