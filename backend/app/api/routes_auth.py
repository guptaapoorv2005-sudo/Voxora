"""Owner authentication with revocable, rotating refresh credentials."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.owner import OwnerLoginAttempt, OwnerSession, OwnerUser
from app.db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["owner authentication"])
password_hash = PasswordHash.recommended()
DUMMY_HASH = password_hash.hash("not-a-real-owner-password")
ACCESS_SECONDS = 900
REFRESH_SECONDS = 7 * 24 * 3600


def signing_key() -> str:
    key = get_settings().owner_jwt_secret
    if not key or len(key.encode()) < 32:
        raise HTTPException(503, "Owner authentication is not configured")
    return key


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or origin not in get_settings().allowed_origins:
        raise HTTPException(403, "Invalid request origin")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cookies(response: Response, session: OwnerSession, token: str) -> None:
    settings = get_settings()
    secure = settings.environment.lower() != "development"
    now = datetime.now(UTC)
    access = jwt.encode(
        {
            "sub": str(session.owner_id),
            "sid": session.id,
            "iat": now,
            "exp": now + timedelta(seconds=ACCESS_SECONDS),
            "typ": "access",
        },
        signing_key(),
        algorithm="HS256",
    )
    for name, value, age in (
        ("voxora_access", access, ACCESS_SECONDS),
        ("voxora_refresh", f"{session.id}.{token}", REFRESH_SECONDS),
    ):
        response.set_cookie(
            name, value, max_age=age, httponly=True, secure=secure, samesite="strict", path="/"
        )
    response.headers["Cache-Control"] = "no-store"


def clear_cookies(response: Response) -> None:
    for name in ("voxora_access", "voxora_refresh"):
        response.delete_cookie(
            name,
            path="/",
            secure=get_settings().environment.lower() != "development",
            httponly=True,
            samesite="strict",
        )
    response.headers["Cache-Control"] = "no-store"


async def require_owner(request: Request, db: AsyncSession = Depends(get_db)) -> OwnerUser:
    token = request.cookies.get("voxora_access")
    try:
        payload = jwt.decode(
            token or "",
            signing_key(),
            algorithms=["HS256"],
            options={"require": ["sub", "sid", "exp", "iat", "typ"]},
        )
        if payload["typ"] != "access" or not isinstance(payload["sid"], str):
            raise ValueError("invalid token")
        owner_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Authentication required") from None
    row = await db.get(OwnerSession, payload["sid"])
    owner = await db.get(OwnerUser, owner_id)
    if (
        row is None
        or row.owner_id != owner_id
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC)
        or owner is None
        or not owner.is_active
    ):
        raise HTTPException(401, "Authentication required")
    return owner


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login", dependencies=[Depends(check_origin)])
async def login(
    body: Login, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    signing_key()
    # A single owner account permits a global, database-backed limit shared by workers.
    now = datetime.now(UTC)
    await db.execute(text("SELECT pg_advisory_xact_lock(18273645, 7)"))
    await db.execute(
        delete(OwnerLoginAttempt).where(
            OwnerLoginAttempt.attempted_at < now - timedelta(minutes=15)
        )
    )
    count = await db.scalar(select(func.count()).select_from(OwnerLoginAttempt))
    if count is not None and count >= 10:
        await db.commit()
        raise HTTPException(429, "Too many login attempts; try again later")
    db.add(OwnerLoginAttempt(attempted_at=now))
    await db.commit()
    owner = (
        await db.execute(select(OwnerUser).where(OwnerUser.email == body.email.strip().lower()))
    ).scalar_one_or_none()
    encoded = owner.hashed_password if owner and owner.hashed_password else DUMMY_HASH
    try:
        valid = await run_in_threadpool(password_hash.verify, body.password, encoded)
    except (ValueError, TypeError, PwdlibError):
        valid = False
    if not valid or owner is None or not owner.is_active or not owner.hashed_password:
        raise HTTPException(401, "Invalid credentials")
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    row = OwnerSession(
        id=secrets.token_hex(32),
        owner_id=owner.id,
        refresh_token_hash=digest(token),
        created_at=now,
        expires_at=now + timedelta(seconds=REFRESH_SECONDS),
    )
    db.add(row)
    await db.commit()
    cookies(response, row, token)
    return {"ok": True}


@router.post("/refresh", dependencies=[Depends(check_origin)])
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    signing_key()
    sid, sep, token = request.cookies.get("voxora_refresh", "").partition(".")
    if not sep or len(sid) != 64 or not token or len(token) > 256:
        raise HTTPException(401, "Authentication required")
    row = (
        await db.execute(select(OwnerSession).where(OwnerSession.id == sid).with_for_update())
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= now
        or not secrets.compare_digest(row.refresh_token_hash, digest(token))
    ):
        raise HTTPException(401, "Authentication required")
    owner = await db.get(OwnerUser, row.owner_id)
    if owner is None or not owner.is_active:
        raise HTTPException(401, "Authentication required")
    new_token = secrets.token_urlsafe(32)
    row.refresh_token_hash = digest(new_token)
    # Fixed absolute expiry prevents indefinite extension of a stolen session.
    await db.commit()
    cookies(response, row, new_token)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(check_origin)])
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    sid, sep, token = request.cookies.get("voxora_refresh", "").partition(".")
    if sep and len(sid) == 64 and 0 < len(token) <= 256:
        row = (
            await db.execute(select(OwnerSession).where(OwnerSession.id == sid).with_for_update())
        ).scalar_one_or_none()
        if row and secrets.compare_digest(row.refresh_token_hash, digest(token)):
            row.revoked_at = datetime.now(UTC)
            await db.commit()
    clear_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(owner: OwnerUser = Depends(require_owner)) -> dict[str, str]:
    return {"email": owner.email}
