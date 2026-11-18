"""SQLAlchemy ORM model for the clinic owner user account.

Phase 3 Scope and Architectural Boundary:
This model establishes the relational schema foundation for the ``owner_users`` table.
In accordance with our phase roadmap:
- The database table and Alembic migration are created here in Phase 3.
- No active owner user, fake login credentials, or usable password hashes are seeded
  during Phase 3.
- Owner account provisioning, password hashing (Argon2/bcrypt), JWT session management,
  and authentication endpoints are strictly deferred to Phase 7.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class OwnerUser(Base):
    """Clinic owner account representation.

    In Voxora's single-business architecture, exactly one owner account is permitted.
    No staff, multi-tenant, or organization membership roles are supported.
    """

    __tablename__ = "owner_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False, doc="Owner email address"
    )
    hashed_password: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "Securely hashed password (Argon2/bcrypt). Kept nullable so the table can exist "
            "in Phase 3 without inserting dummy or insecure credentials before Phase 7 auth."
        ),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("char_length(trim(email)) > 0", name="chk_owner_email_not_empty"),
        Index("uq_single_owner", text("(true)"), unique=True),
    )


class OwnerSession(Base):
    """Durable refresh session; only a digest of the bearer token is stored."""

    __tablename__ = "owner_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OwnerLoginAttempt(Base):
    __tablename__ = "owner_login_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
