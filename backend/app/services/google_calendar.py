"""Reconcile committed appointment records to the owner's primary Google Calendar."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import and_, or_, select

from app.core.config import get_settings
from app.db.models.booking import Appointment
from app.db.models.google_calendar import GoogleCalendarConnection, GoogleCalendarEvent
from app.db.session import get_session_factory

logger = logging.getLogger("voxora.google_calendar")
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def event_payload(appointment: Appointment, event_id: str) -> dict[str, Any]:
    # Include the information needed to identify the booking, but no contact details.
    return {"id": event_id, "summary": f"{appointment.service_name_snapshot} — {appointment.caller_name}",
            "start": {"dateTime": appointment.start_at.isoformat()},
            "end": {"dateTime": appointment.end_at.isoformat()}}


async def sync_event(client: httpx.AsyncClient, token: str, appointment: Appointment,
                     event_id: str, exists: bool) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    if appointment.status == "cancelled":
        # A create might have succeeded just before the process lost its DB transaction.
        response = await client.delete(f"{EVENTS_URL}/{event_id}", headers=headers)
        if response.status_code not in (404, 410):
            response.raise_for_status()
        return
    payload = event_payload(appointment, event_id)
    if exists:
        response = await client.patch(f"{EVENTS_URL}/{event_id}", headers=headers, json=payload)
        if response.status_code not in (404, 410):
            response.raise_for_status()
            return
    response = await client.post(EVENTS_URL, headers=headers, json=payload)
    if response.status_code == 409:
        # A previous create may have succeeded before the database transaction committed.
        response = await client.patch(f"{EVENTS_URL}/{event_id}", headers=headers, json=payload)
    response.raise_for_status()


async def reconcile_calendar() -> None:
    cfg = get_settings()
    async with get_session_factory()() as db:
        # Only one process may update the calendar or disconnect/reconnect at a time.
        connection = (await db.execute(select(GoogleCalendarConnection).with_for_update(skip_locked=True))).scalars().first()
        if connection is None:
            return
        rows = (await db.execute(
            select(Appointment, GoogleCalendarEvent)
            .outerjoin(GoogleCalendarEvent, GoogleCalendarEvent.appointment_id == Appointment.id)
            .where(or_(
                and_(GoogleCalendarEvent.appointment_id.is_(None), or_(
                    and_(Appointment.status == "confirmed", Appointment.end_at > datetime.now(UTC)),
                    Appointment.updated_at >= connection.connected_at,
                )),
                and_(GoogleCalendarEvent.appointment_id.is_not(None), GoogleCalendarEvent.synced_version != Appointment.version),
            )).order_by(Appointment.id)
        )).all()
        if not rows:
            return
        try:
            if not cfg.google_calendar_encryption_key:
                raise ValueError("Calendar encryption key is missing")
            refresh_token = Fernet(cfg.google_calendar_encryption_key.encode()).decrypt(connection.encrypted_refresh_token.encode()).decode()
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post("https://oauth2.googleapis.com/token", data={
                    "client_id": cfg.google_calendar_client_id, "client_secret": cfg.google_calendar_client_secret,
                    "refresh_token": refresh_token, "grant_type": "refresh_token",
                })
                response.raise_for_status()
                token = response.json()["access_token"]
                had_error = False
                for appointment, mapping in rows:
                    event_id = mapping.event_id if mapping else f"{connection.event_prefix}{appointment.id:x}"
                    try:
                        await sync_event(client, token, appointment, event_id, mapping is not None)
                    except httpx.HTTPError:
                        had_error = True
                        logger.warning("Google Calendar event sync failed for appointment %s", appointment.id)
                        continue
                    if mapping:
                        mapping.synced_version = appointment.version
                    else:
                        db.add(GoogleCalendarEvent(appointment_id=appointment.id, owner_id=connection.owner_id,
                                                   event_id=event_id, synced_version=appointment.version))
                connection.last_error = "Some appointments could not be synced" if had_error else None
        except Exception:
            # Never log credentials, Google response bodies, or appointment details.
            logger.warning("Google Calendar synchronization failed; will retry")
            connection.last_error = "Calendar synchronization failed"
        await db.commit()


async def calendar_sync_loop() -> None:
    while True:
        try:
            await reconcile_calendar()
        except Exception:
            logger.exception("Google Calendar synchronization could not run")
        await asyncio.sleep(30)
