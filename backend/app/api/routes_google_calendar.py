"""Owner authorization for one-way Google Calendar synchronization."""

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes_auth import check_origin, require_owner, signing_key
from app.core.config import AppSettings, get_settings
from app.db.models.google_calendar import GoogleCalendarConnection, GoogleCalendarEvent
from app.db.models.owner import OwnerSession, OwnerUser
from app.db.session import get_db

router = APIRouter(prefix="/api/owner/google-calendar", tags=["google calendar"])
SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"


def calendar_config() -> AppSettings:
    settings = get_settings()
    if not all((settings.google_calendar_client_id, settings.google_calendar_client_secret,
                settings.google_calendar_redirect_uri, settings.google_calendar_encryption_key)):
        raise HTTPException(503, "Google Calendar is not configured")
    try:
        assert settings.google_calendar_encryption_key is not None
        Fernet(settings.google_calendar_encryption_key.encode())
    except (ValueError, TypeError):
        raise HTTPException(503, "Google Calendar encryption key is invalid") from None
    return settings


@router.get("")
async def status(owner: OwnerUser = Depends(require_owner), db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    row = await db.get(GoogleCalendarConnection, owner.id)
    return {"connected": row is not None, "needs_attention": bool(row and row.last_error)}


@router.post("/connect", dependencies=[Depends(check_origin)])
async def connect(request: Request, response: Response, owner: OwnerUser = Depends(require_owner)) -> dict[str, str]:
    cfg = calendar_config()
    access = jwt.decode(request.cookies["voxora_access"], signing_key(), algorithms=["HS256"])
    now = datetime.now(UTC)
    state = jwt.encode({"sub": str(owner.id), "sid": access["sid"], "nonce": secrets.token_urlsafe(24),
                        "iat": now, "exp": now + timedelta(minutes=10), "typ": "calendar"},
                       signing_key(), algorithm="HS256")
    response.set_cookie("voxora_calendar_state", state, max_age=600, httponly=True,
                        secure=cfg.environment.lower() != "development", samesite="lax", path="/")
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cfg.google_calendar_client_id, "redirect_uri": cfg.google_calendar_redirect_uri,
        "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "state": state,
    })}


@router.get("/callback")
async def callback(request: Request, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    cfg = calendar_config()
    state = request.query_params.get("state", "")
    cookie = request.cookies.get("voxora_calendar_state", "")
    if not state or not cookie or not secrets.compare_digest(state, cookie):
        raise HTTPException(400, "Invalid calendar authorization state")
    try:
        payload = jwt.decode(state, signing_key(), algorithms=["HS256"],
                             options={"require": ["sub", "sid", "exp", "iat", "typ"]})
        if payload["typ"] != "calendar":
            raise ValueError()
        owner_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(400, "Expired or invalid calendar authorization") from None
    session = await db.get(OwnerSession, payload["sid"])
    owner = await db.get(OwnerUser, owner_id)
    if not session or session.owner_id != owner_id or session.revoked_at or session.expires_at <= datetime.now(UTC) or not owner or not owner.is_active:
        raise HTTPException(401, "Owner session expired")
    code = request.query_params.get("code")
    if not code:
        response = RedirectResponse("/dashboard?calendar=denied", status_code=303)
    else:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                result = await client.post("https://oauth2.googleapis.com/token", data={
                    "code": code, "client_id": cfg.google_calendar_client_id,
                    "client_secret": cfg.google_calendar_client_secret,
                    "redirect_uri": cfg.google_calendar_redirect_uri, "grant_type": "authorization_code",
                })
                result.raise_for_status()
                data = result.json()
                token = data.get("refresh_token")
                if SCOPE not in data.get("scope", "").split():
                    raise ValueError("Calendar permission not granted")
            if not token:
                raise ValueError("Missing refresh token")
            assert cfg.google_calendar_encryption_key is not None
            encrypted = Fernet(cfg.google_calendar_encryption_key.encode()).encrypt(token.encode()).decode()
            existing = (await db.execute(select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.owner_id == owner_id).with_for_update())).scalar_one_or_none()
            if existing:
                await db.execute(delete(GoogleCalendarEvent).where(GoogleCalendarEvent.owner_id == owner_id))
                existing.encrypted_refresh_token = encrypted
                existing.event_prefix = secrets.token_hex(12)
                existing.connected_at = datetime.now(UTC)
                existing.last_error = None
            else:
                db.add(GoogleCalendarConnection(owner_id=owner_id, encrypted_refresh_token=encrypted,
                                                event_prefix=secrets.token_hex(12), connected_at=datetime.now(UTC)))
            await db.commit()
            response = RedirectResponse("/dashboard?calendar=connected", status_code=303)
        except (httpx.HTTPError, ValueError, InvalidToken):
            response = RedirectResponse("/dashboard?calendar=error", status_code=303)
    response.delete_cookie("voxora_calendar_state", path="/", secure=cfg.environment.lower() != "development", httponly=True, samesite="lax")
    return response


@router.post("/disconnect", dependencies=[Depends(check_origin)])
async def disconnect(owner: OwnerUser = Depends(require_owner), db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    row = (await db.execute(select(GoogleCalendarConnection).where(
        GoogleCalendarConnection.owner_id == owner.id).with_for_update())).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"connected": False}
