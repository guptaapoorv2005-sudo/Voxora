import ipaddress
import logging
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from livekit import api as lk_api
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings, get_settings
from app.db.models.clinic import BusinessProfile
from app.db.session import get_db

logger = logging.getLogger("voxora.api.session")
router = APIRouter(prefix="/api/sessions", tags=["sessions"])

TOKEN_TTL = timedelta(seconds=300)


@router.post("/token")
async def create_session_token(
    settings: AppSettings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]  # Direct tests may omit the injected request.
) -> JSONResponse:
    """Generate a short-lived LiveKit JWT for an incoming voice session."""
    if settings.environment == "development" and request is not None:
        try:
            if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
                raise HTTPException(403, "Development sessions require a loopback connection")
        except ValueError:
            raise HTTPException(403, "Development sessions require a loopback connection") from None

    try:
        query = select(BusinessProfile.receptionist_enabled).where(BusinessProfile.slug == "brightsmile")
        enabled = await db.scalar(query)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Receptionist unavailable") from None
    if enabled is not True:
        raise HTTPException(status_code=503, detail="Receptionist unavailable")

    # Server generates unique room and participant identity
    room_name = f"voxora-{uuid4().hex[:12]}"
    identity = f"caller-{uuid4().hex[:8]}"
    dispatch_name = settings.effective_agent_name

    logger.info(
        "Issuing session token for room '%s' targeting agent dispatch '%s'",
        room_name,
        dispatch_name,
    )

    token = (
        lk_api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            lk_api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_publish_sources=["microphone"],  # Restricted to microphone only
                can_subscribe=True,
                can_publish_data=False,  # No arbitrary data publishing
                can_update_own_metadata=False,
            )
        )
        .with_room_config(
            lk_api.RoomConfiguration(agents=[lk_api.RoomAgentDispatch(agent_name=dispatch_name)])
        )
        .to_jwt()
    )

    return JSONResponse(
        content={
            "token": token,
            "livekit_url": settings.livekit_url,
            "room_name": room_name,
        },
        headers={"Cache-Control": "no-store"},
    )
