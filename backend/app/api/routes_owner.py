"""Authenticated management of the single clinic."""

from datetime import date, time
from decimal import Decimal
from typing import Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes_auth import check_origin, require_owner
from app.db.models.booking import Appointment
from app.db.models.call import Call, CallTraceEvent
from app.db.models.clinic import (
    FAQ,
    BusinessClosure,
    BusinessHours,
    BusinessProfile,
    ClinicPolicy,
    Service,
)
from app.db.session import get_db
from app.services.schedule_validation import lock_schedule_configuration

router = APIRouter(prefix="/api/owner", tags=["owner"], dependencies=[Depends(require_owner)])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Profile(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    address_line1: str = Field(min_length=1, max_length=300)
    address_line2: str | None = Field(default=None, max_length=300)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=300)


class Hours(Input):
    day_of_week: int = Field(ge=0, le=6)
    is_closed: bool
    open_time: time | None = None
    close_time: time | None = None
    break_start: time | None = None
    break_end: time | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        values = (self.open_time, self.close_time, self.break_start, self.break_end)
        if self.is_closed:
            if any(v is not None for v in values):
                raise ValueError("Closed days cannot have hours")
        elif self.open_time is None or self.close_time is None or self.open_time >= self.close_time:
            raise ValueError("Opening time must precede closing time")
        elif (self.break_start is None) != (self.break_end is None):
            raise ValueError("Both break times are required")
        elif (
            self.break_start is not None
            and self.break_end is not None
            and not (self.open_time <= self.break_start < self.break_end <= self.close_time)
        ):
            raise ValueError("Break must be inside opening hours")
        if any(v is not None and v.tzinfo is not None for v in values):
            raise ValueError("Hours must be local times without an offset")
        return self


class Closure(Input):
    date: date
    reason: str = Field(min_length=1, max_length=300)


class ServiceInput(Input):
    id: int | None = None
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=4000)
    duration_minutes: int = Field(gt=0, le=1440)
    buffer_minutes: int = Field(ge=0, le=1440)
    price_inr: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    min_lead_time_hours: int = Field(ge=0, le=8760)
    cancellation_window_hours: int = Field(ge=0, le=8760)
    is_active: bool = True
    sort_order: int = 0


class FaqInput(Input):
    id: int | None = None
    topic: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)
    is_active: bool = True
    sort_order: int = 0


class PolicyInput(Input):
    policy_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    sort_order: int = 0


class Toggle(Input):
    enabled: bool


async def clinic(db: AsyncSession) -> BusinessProfile:
    result = await db.execute(select(BusinessProfile).where(BusinessProfile.slug == "brightsmile"))
    obj = result.scalar_one_or_none()
    if obj is None:
        raise HTTPException(404, "Clinic not found")
    return obj


async def commit_context(db: AsyncSession) -> None:
    """Persist validated clinic edits; tool responses are bounded when retrieved."""
    await db.commit()


def fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in names}


PROFILE_FIELDS = tuple(Profile.model_fields)
SERVICE_FIELDS = tuple(ServiceInput.model_fields)
FAQ_FIELDS = tuple(FaqInput.model_fields)
POLICY_FIELDS = tuple(PolicyInput.model_fields)
HOUR_FIELDS = tuple(Hours.model_fields)


@router.get("/profile")
async def get_profile(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    obj = await clinic(db)
    return {
        **fields(obj, PROFILE_FIELDS),
        "timezone": obj.timezone,
        "receptionist_enabled": obj.receptionist_enabled,
    }


@router.put("/profile", dependencies=[Depends(check_origin)])
async def update_profile(data: Profile, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    obj = await clinic(db)
    for key, value in data.model_dump().items():
        setattr(obj, key, value)
    await commit_context(db)
    return {"ok": True}


@router.put("/receptionist", dependencies=[Depends(check_origin)])
async def toggle(data: Toggle, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    obj = await clinic(db)
    obj.receptionist_enabled = data.enabled
    await db.commit()
    return {"enabled": data.enabled}


@router.get("/hours")
async def get_hours(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    obj = await clinic(db)
    rows = (
        await db.execute(
            select(BusinessHours)
            .where(BusinessHours.clinic_id == obj.id)
            .order_by(BusinessHours.day_of_week)
        )
    ).scalars()
    return [fields(row, HOUR_FIELDS) for row in rows]


@router.put("/hours", dependencies=[Depends(check_origin)])
async def update_hours(data: list[Hours], db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    if len(data) != 7 or {x.day_of_week for x in data} != set(range(7)):
        raise HTTPException(422, "Exactly one entry for each weekday is required")
    obj = await clinic(db)
    await lock_schedule_configuration(db, obj.id)
    rows = (
        (await db.execute(select(BusinessHours).where(BusinessHours.clinic_id == obj.id)))
        .scalars()
        .all()
    )
    existing = {row.day_of_week: row for row in rows}
    for item in data:
        row = existing.get(item.day_of_week)
        if row is None:
            db.add(BusinessHours(clinic_id=obj.id, **item.model_dump()))
        else:
            for key, value in item.model_dump().items():
                setattr(row, key, value)
    await commit_context(db)
    return {"ok": True}


@router.get("/closures")
async def get_closures(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    obj = await clinic(db)
    rows = (
        await db.execute(
            select(BusinessClosure)
            .where(BusinessClosure.clinic_id == obj.id)
            .order_by(BusinessClosure.date)
        )
    ).scalars()
    return [fields(row, ("date", "reason")) for row in rows]


@router.put("/closures", dependencies=[Depends(check_origin)])
async def update_closures(
    data: list[Closure], db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    if len(data) > 500 or len({x.date for x in data}) != len(data):
        raise HTTPException(422, "Closure dates must be unique and limited to 500")
    obj = await clinic(db)
    await lock_schedule_configuration(db, obj.id)
    await db.execute(delete(BusinessClosure).where(BusinessClosure.clinic_id == obj.id))
    db.add_all(
        [BusinessClosure(clinic_id=obj.id, all_day=True, **item.model_dump()) for item in data]
    )
    await commit_context(db)
    return {"ok": True}


@router.get("/{kind}")
async def get_collection(
    kind: Literal["services", "faqs", "policies", "appointments", "calls"],
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    obj = await clinic(db)
    if kind == "appointments":
        appointments = (
            await db.execute(
                select(Appointment)
                .where(Appointment.clinic_id == obj.id)
                .order_by(Appointment.start_at.desc())
                .limit(500)
            )
        ).scalars()
        return [
            fields(
                x,
                (
                    "id",
                    "service_name_snapshot",
                    "caller_name",
                    "caller_phone",
                    "start_at",
                    "end_at",
                    "status",
                ),
            )
            for x in appointments
        ]
    if kind == "calls":
        calls = (
            await db.execute(select(Call).order_by(Call.started_at.desc()).limit(500))
        ).scalars()
        return [fields(x, ("id", "started_at", "ended_at", "outcome")) for x in calls]
    model: Any
    model, names = {
        "services": (Service, SERVICE_FIELDS),
        "faqs": (FAQ, FAQ_FIELDS),
        "policies": (ClinicPolicy, POLICY_FIELDS),
    }[kind]
    rows = (
        await db.execute(
            select(model).where(model.clinic_id == obj.id).order_by(model.sort_order, model.id)
        )
    ).scalars()
    return [fields(x, names) for x in rows]


async def replace_collection(
    db: AsyncSession,
    model: Any,
    data: list[ServiceInput] | list[FaqInput],
    *,
    deactivate: bool = False,
) -> dict[str, bool]:
    obj = await clinic(db)
    if model is Service:
        await lock_schedule_configuration(db, obj.id)
    rows = (await db.execute(select(model).where(model.clinic_id == obj.id))).scalars().all()
    existing = {row.id: row for row in rows}
    ids = [item.id for item in data if item.id is not None]
    if len(ids) != len(set(ids)) or any(i not in existing for i in ids):
        raise HTTPException(422, "Invalid or duplicate identifier")
    for row in rows:
        if row.id not in ids:
            if deactivate:
                row.is_active = False
            else:
                await db.delete(row)
    for item in data:
        values = item.model_dump(exclude={"id"})
        if item.id is None:
            db.add(model(clinic_id=obj.id, **values))
        else:
            for key, value in values.items():
                setattr(existing[item.id], key, value)
    await commit_context(db)
    return {"ok": True}


@router.put("/services", dependencies=[Depends(check_origin)])
async def update_services(
    data: list[ServiceInput], db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    if len(data) > 100:
        raise HTTPException(422, "Too many services")
    return await replace_collection(db, Service, data, deactivate=True)


@router.put("/faqs", dependencies=[Depends(check_origin)])
async def update_faqs(data: list[FaqInput], db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    if len(data) > 100:
        raise HTTPException(422, "Too many FAQs")
    return await replace_collection(db, FAQ, data)


@router.put("/policies", dependencies=[Depends(check_origin)])
async def update_policies(
    data: list[PolicyInput], db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    if len(data) > 100 or len({x.policy_type for x in data}) != len(data):
        raise HTTPException(422, "Policy types must be unique and limited to 100")
    obj = await clinic(db)
    await db.execute(delete(ClinicPolicy).where(ClinicPolicy.clinic_id == obj.id))
    db.add_all([ClinicPolicy(clinic_id=obj.id, **item.model_dump()) for item in data])
    await commit_context(db)
    return {"ok": True}


@router.get("/calls/{call_id}")
async def call_detail(call_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "Call not found")
    events = (
        await db.execute(
            select(CallTraceEvent)
            .where(CallTraceEvent.call_id == call_id)
            .order_by(CallTraceEvent.sequence)
        )
    ).scalars()
    return {
        **fields(call, ("id", "started_at", "ended_at", "outcome")),
        "events": [fields(e, ("sequence", "occurred_at", "event", "details")) for e in events],
    }
