"""FastAPI HTTP endpoints for availability calculation, proposals, and confirmation."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.api.routes_auth import check_origin, require_owner
from app.db.models.booking import Appointment
from app.db.session import get_db
from app.repositories.clinic_repo import (
    get_hours_by_clinic_id,
    get_profile_by_id,
    get_service_by_id,
    get_upcoming_closures_by_clinic_id,
)
from app.schemas.booking import (
    AppointmentResponse,
    AvailabilityResponse,
    ConfirmBookingRequest,
    ProposalResponse,
    ProposeBookingRequest,
    SlotResponse,
)
from app.schemas.dto import ConsentAuthorization, ServiceDTO
from app.services.availability import compute_available_slots
from app.services.booking import BookingService
from app.services.booking_errors import (
    AppointmentNotFoundError,
    BookingDomainError,
    CancellationWindowError,
    IdempotencyConflictError,
    IdempotencyRaceError,
    InvalidAppointmentStatusError,
    InvalidConfirmationTurnError,
    InvalidConsentError,
    InvalidInputError,
    ProposalAlreadyUsedError,
    ProposalExpiredError,
    ProposalNotFoundError,
    SessionMismatchError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
    VerificationRequiredError,
)
from app.voice.context import resolve_clinic_timezone

router = APIRouter(prefix="/api", tags=["booking"])
booking_service = BookingService()


def _handle_domain_error(exc: Exception) -> None:
    """Map typed booking domain exceptions to appropriate HTTP status codes."""
    if isinstance(exc, (ProposalNotFoundError, AppointmentNotFoundError)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    elif isinstance(exc, SessionMismatchError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    elif isinstance(exc, (InvalidConsentError, InvalidConfirmationTurnError)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    elif isinstance(exc, ProposalExpiredError):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    elif isinstance(
        exc,
        (
            SlotUnavailableError,
            ProposalAlreadyUsedError,
            IdempotencyConflictError,
            StaleAppointmentVersionError,
            IdempotencyRaceError,
            InvalidAppointmentStatusError,
        ),
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    elif isinstance(exc, VerificationRequiredError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    elif isinstance(exc, (CancellationWindowError, InvalidInputError, ValueError)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    elif isinstance(exc, BookingDomainError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


@router.get("/availability", response_model=AvailabilityResponse)
async def get_availability(
    clinic_id: int = Query(default=1, description="Clinic identifier"),
    service_id: int = Query(..., description="Service identifier"),
    from_date: date = Query(..., description="Query start date"),
    to_date: date = Query(..., description="Query end date (inclusive)"),
    session: AsyncSession = Depends(get_db),
) -> AvailabilityResponse:
    """Compute available appointment slots for a service across a date range."""
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_date must be greater than or equal to from_date.",
        )
    if (to_date - from_date).days > 30:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 30 days.",
        )

    profile = await get_profile_by_id(session, clinic_id=clinic_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clinic with ID {clinic_id} not found.",
        )

    clinic_tz = resolve_clinic_timezone(profile.timezone)
    service = await get_service_by_id(session, clinic_id=clinic_id, service_id=service_id)
    if service is None or not service.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active service with ID {service_id} not found for clinic {clinic_id}.",
        )

    hours = await get_hours_by_clinic_id(session, clinic_id=clinic_id)
    closures = await get_upcoming_closures_by_clinic_id(
        session, clinic_id=clinic_id, since_date=from_date, until_date=to_date
    )

    # Convert date range boundaries to UTC for querying overlapping appointments and proposals
    range_start_dt = datetime.combine(from_date, time.min, tzinfo=clinic_tz).astimezone(UTC)
    range_end_dt = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=clinic_tz).astimezone(UTC)
    now_utc = datetime.now(UTC)

    apt_res = await session.execute(
        select(Appointment).where(
            Appointment.clinic_id == clinic_id,
            Appointment.status == "confirmed",
            Appointment.start_at < range_end_dt,
            Appointment.blocked_until_at > range_start_dt,
        )
    )
    existing_appointments = apt_res.scalars().all()

    service_dto = ServiceDTO(id=service.id, name=service.name, description=service.description,
            duration_minutes=service.duration_minutes, buffer_minutes=service.buffer_minutes,
            price_inr=service.price_inr, min_lead_time_hours=service.min_lead_time_hours,
            cancellation_window_hours=service.cancellation_window_hours, sort_order=service.sort_order)
    slot_dtos = []
    window_start = from_date
    while window_start <= to_date:
        window_end = min(window_start + timedelta(days=14), to_date)
        slot_dtos.extend(compute_available_slots(
            clinic_id=clinic_id,
            service=service_dto,
            hours=tuple(hours),
            closures=closures,
            existing_appointments=existing_appointments,
            start_date=window_start,
            end_date=window_end,
            clinic_tz=clinic_tz,
            now_utc=now_utc,
        ))
        window_start = window_end + timedelta(days=1)

    slots = [
        SlotResponse(
            start_at=s.start_at,
            end_at=s.end_at,
            duration_minutes=s.duration_minutes,
        )
        for s in slot_dtos
    ]

    return AvailabilityResponse(
        clinic_id=clinic_id,
        service_id=service_id,
        from_date=from_date,
        to_date=to_date,
        slots=slots,
    )


@router.post("/proposals", response_model=ProposalResponse, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(check_origin), Depends(require_owner)])
async def propose_booking(
    req: ProposeBookingRequest,
    session: AsyncSession = Depends(get_db),
) -> ProposalResponse:
    """Create or idempotently retrieve an expiring booking proposal."""
    try:
        dto = await booking_service.propose_booking(
            session=session,
            clinic_id=req.clinic_id,
            service_id=req.service_id,
            session_id=req.session_id,
            created_turn_id=req.created_turn_id,
            caller_name=req.caller_name,
            caller_phone=req.caller_phone,
            caller_email=req.caller_email,
            start_at=req.start_at,
        )
        return ProposalResponse(
            id=dto.id,
            clinic_id=dto.clinic_id,
            service_id=dto.service_id,
            service_name=dto.service_name,
            session_id=dto.session_id,
            caller_name=dto.caller_name,
            caller_phone=dto.caller_phone,
            caller_email=dto.caller_email,
            start_at=dto.start_at,
            end_at=dto.end_at,
            duration_minutes=dto.duration_minutes,
            buffer_minutes=dto.buffer_minutes,
            state=dto.state,
            expires_at=dto.expires_at,
        )
    except Exception as exc:
        _handle_domain_error(exc)
        raise exc


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=AppointmentResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(check_origin), Depends(require_owner)],
)
async def confirm_booking(
    proposal_id: UUID,
    req: ConfirmBookingRequest,
    session: AsyncSession = Depends(get_db),
) -> AppointmentResponse:
    """Confirm a booking proposal using signed server consent."""
    settings = get_settings()
    profile = await get_profile_by_id(session, clinic_id=1)
    clinic_tz = resolve_clinic_timezone(profile.timezone if profile else "Asia/Kolkata")

    consent = ConsentAuthorization(
        session_id=req.consent.session_id,
        action_type=req.consent.action_type,
        target_id=req.consent.target_id,
        authorized_turn_id=req.consent.authorized_turn_id,
        challenge_id=req.consent.challenge_id,
        nonce=req.consent.nonce,
        created_at=req.consent.created_at,
        expires_at=req.consent.expires_at,
        ttl_seconds=req.consent.ttl_seconds,
        signature=req.consent.signature,
        expected_target_version=req.consent.expected_target_version,
    )

    try:
        dto = await booking_service.confirm_booking(
            session=session,
            proposal_id=proposal_id,
            voice_session_id=req.voice_session_id,
            consent=consent,
            secret_key=settings.session_signing_secret,
            clinic_tz=clinic_tz,
        )
        return AppointmentResponse(
            id=dto.id,
            clinic_id=dto.clinic_id,
            service_id=dto.service_id,
            service_name=dto.service_name,
            caller_name=dto.caller_name,
            caller_phone=dto.caller_phone,
            caller_email=dto.caller_email,
            start_at=dto.start_at,
            end_at=dto.end_at,
            duration_minutes=dto.duration_minutes,
            buffer_minutes=dto.buffer_minutes,
            status=dto.status,
            version=dto.version,
        )
    except Exception as exc:
        _handle_domain_error(exc)
        raise exc
