"""Google Calendar connection and per-appointment synchronization state."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class GoogleCalendarConnection(Base):
    __tablename__ = "google_calendar_connections"

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner_users.id", ondelete="CASCADE"), primary_key=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    event_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class GoogleCalendarEvent(Base):
    __tablename__ = "google_calendar_events"

    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("google_calendar_connections.owner_id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    synced_version: Mapped[int] = mapped_column(Integer, nullable=False)
