"""FastAPI read-only routes for inspecting clinic configuration.

Why read-only endpoints in Phase 3?
In our phased roadmap:
- The clinic data model, migrations, seed data, and public inspection routes are built in Phase 3.
- All write/mutation endpoints (editing clinic profile, updating hours, changing prices)
  are strictly deferred to Phase 7, when owner authentication, session cookies, and CSRF
  protections are implemented.
- Exposing mutation endpoints in Phase 3 before authentication would introduce
  unauthenticated public write access, violating our security architecture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.repositories.clinic_repo import (
    get_active_faqs_by_clinic_id,
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_policies_by_clinic_id,
    get_profile_by_slug,
    get_upcoming_closures_by_clinic_id,
)
from app.schemas.clinic import (
    BusinessClosureResponse,
    BusinessHoursResponse,
    ClinicPolicyResponse,
    ClinicProfileResponse,
    FAQResponse,
    ServiceResponse,
)
from app.schemas.dto import ClinicProfileDTO
from app.voice.context import resolve_clinic_timezone

router = APIRouter(prefix="/api/clinic", tags=["clinic"])


async def _resolve_clinic(
    session: AsyncSession,
    slug: str,
) -> ClinicProfileDTO:
    """Helper to resolve a clinic profile by slug or raise HTTP 404."""
    profile = await get_profile_by_slug(session, slug=slug)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clinic profile with slug '{slug}' not found.",
        )
    return profile


@router.get("", response_model=ClinicProfileResponse)
async def get_clinic_profile(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    session: AsyncSession = Depends(get_db),
) -> ClinicProfileResponse:
    """Return clinic identity, address, and timezone configuration."""
    profile = await _resolve_clinic(session, slug)
    return ClinicProfileResponse.model_validate(profile)


@router.get("/hours", response_model=list[BusinessHoursResponse])
async def get_clinic_hours(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    session: AsyncSession = Depends(get_db),
) -> list[BusinessHoursResponse]:
    """Return regular weekly operating hours for all 7 days."""
    profile = await _resolve_clinic(session, slug)
    hours = await get_hours_by_clinic_id(session, profile.id)
    return [BusinessHoursResponse.model_validate(h) for h in hours]


@router.get("/closures", response_model=list[BusinessClosureResponse])
async def get_clinic_closures(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    days_ahead: int = Query(default=30, ge=1, le=365, description="Lookahead window in days"),
    session: AsyncSession = Depends(get_db),
) -> list[BusinessClosureResponse]:
    """Return scheduled one-off closures and holidays for the clinic."""
    profile = await _resolve_clinic(session, slug)
    clinic_tz = resolve_clinic_timezone(profile.timezone)
    local_today = datetime.now(UTC).astimezone(clinic_tz).date()
    until_date = local_today + timedelta(days=days_ahead)

    closures = await get_upcoming_closures_by_clinic_id(
        session, profile.id, since_date=local_today, until_date=until_date
    )
    return [BusinessClosureResponse.model_validate(c) for c in closures]


@router.get("/services", response_model=list[ServiceResponse])
async def get_clinic_services(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    session: AsyncSession = Depends(get_db),
) -> list[ServiceResponse]:
    """Return active dental services with durations, buffers, and prices."""
    profile = await _resolve_clinic(session, slug)
    services = await get_active_services_by_clinic_id(session, profile.id)
    return [ServiceResponse.model_validate(s) for s in services]


@router.get("/policies", response_model=list[ClinicPolicyResponse])
async def get_clinic_policies(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    session: AsyncSession = Depends(get_db),
) -> list[ClinicPolicyResponse]:
    """Return conversational policies (e.g. cancellation terms)."""
    profile = await _resolve_clinic(session, slug)
    policies = await get_policies_by_clinic_id(session, profile.id)
    return [ClinicPolicyResponse.model_validate(p) for p in policies]


@router.get("/faqs", response_model=list[FAQResponse])
async def get_clinic_faqs(
    slug: str = Query(default="brightsmile", description="Unique clinic slug"),
    session: AsyncSession = Depends(get_db),
) -> list[FAQResponse]:
    """Return active Frequently Asked Questions."""
    profile = await _resolve_clinic(session, slug)
    faqs = await get_active_faqs_by_clinic_id(session, profile.id)
    return [FAQResponse.model_validate(f) for f in faqs]
