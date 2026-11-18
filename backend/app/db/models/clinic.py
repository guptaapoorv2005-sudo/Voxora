"""SQLAlchemy ORM models for clinic configuration, hours, closures, services, policies, and FAQs.

Why define these models with SQLAlchemy ORM?
In a MERN stack application, you would define Mongoose schemas with validation rules.
In Python with SQLAlchemy 2.0, we use ``Mapped`` type annotations and ``mapped_column``
coupled with database-level ``CheckConstraint``s so that invalid business states
(such as closing before opening or negative service prices) are rejected at the
PostgreSQL database level, ensuring data integrity regardless of how records are written.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class BusinessProfile(Base):
    """Canonical business identity and regional configuration for the clinic.

    In Voxora's single-business MVP, there is exactly one canonical profile row
    identified by its unique slug (``brightsmile``).
    """

    __tablename__ = "business_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(
        String(80), unique=True, index=True, nullable=False, doc="URL-safe unique clinic key"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, doc="Display name of the clinic")
    description: Mapped[str] = mapped_column(
        Text, nullable=False, doc="High-level description of clinic services and scope"
    )
    address_line1: Mapped[str] = mapped_column(String(300), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(100), nullable=False, default="India")
    phone: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        doc=(
            "Optional contact phone. Kept nullable in seed data to avoid publishing unverified "
            "or routable phone numbers in demo environments."
        ),
    )
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    timezone: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="Asia/Kolkata",
        doc="Canonical IANA timezone identifier for all local appointment and hour calculations",
    )
    receptionist_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    hours: Mapped[list[BusinessHours]] = relationship(
        "BusinessHours", back_populates="clinic", cascade="all, delete-orphan"
    )
    closures: Mapped[list[BusinessClosure]] = relationship(
        "BusinessClosure", back_populates="clinic", cascade="all, delete-orphan"
    )
    services: Mapped[list[Service]] = relationship(
        "Service", back_populates="clinic", cascade="all, delete-orphan"
    )
    policies: Mapped[list[ClinicPolicy]] = relationship(
        "ClinicPolicy", back_populates="clinic", cascade="all, delete-orphan"
    )
    faqs: Mapped[list[FAQ]] = relationship(
        "FAQ", back_populates="clinic", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("char_length(trim(slug)) > 0", name="chk_profile_slug_not_empty"),
        CheckConstraint("char_length(trim(name)) > 0", name="chk_profile_name_not_empty"),
        CheckConstraint("char_length(trim(timezone)) > 0", name="chk_profile_timezone_not_empty"),
    )


class BusinessHours(Base):
    """Weekly operating schedule for each day of the week (0=Monday through 6=Sunday).

    Database constraints enforce that:
    1. Days are within 0-6.
    2. Closed days cannot contain opening, closing, or break times.
    3. Open days have closing time strictly greater than opening time.
    4. Optional break intervals fall strictly within operating hours.
    """

    __tablename__ = "business_hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, doc="0=Monday, 1=Tuesday, ..., 6=Sunday"
    )
    is_closed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, doc="True if the clinic is closed all day"
    )
    open_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    close_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_end: Mapped[time | None] = mapped_column(Time, nullable=True)

    clinic: Mapped[BusinessProfile] = relationship("BusinessProfile", back_populates="hours")

    __table_args__ = (
        UniqueConstraint("clinic_id", "day_of_week", name="uq_hours_clinic_day"),
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="chk_hours_day_range"),
        CheckConstraint(
            "(is_closed = true AND open_time IS NULL AND close_time IS NULL "
            "AND break_start IS NULL AND break_end IS NULL) OR "
            "(is_closed = false AND open_time IS NOT NULL AND close_time IS NOT NULL "
            "AND close_time > open_time)",
            name="chk_hours_open_close_times",
        ),
        CheckConstraint(
            "(is_closed = true) OR "
            "(break_start IS NULL AND break_end IS NULL) OR "
            "(is_closed = false AND break_start IS NOT NULL AND break_end IS NOT NULL "
            "AND break_end > break_start AND break_start >= open_time AND break_end <= close_time)",
            name="chk_hours_break_interval",
        ),
    )


class BusinessClosure(Base):
    """Specific one-off closures and public holidays.

    Phase 3 supports all-day calendar closures only (enforced by constraint).
    """

    __tablename__ = "business_closures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(
        Date, nullable=False, doc="Calendar date of closure in clinic local time"
    )
    reason: Mapped[str] = mapped_column(
        String(300), nullable=False, doc="Description such as 'Republic Day' or 'Diwali'"
    )
    all_day: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, doc="Phase 3 supports all-day closures only"
    )

    clinic: Mapped[BusinessProfile] = relationship("BusinessProfile", back_populates="closures")

    __table_args__ = (
        UniqueConstraint("clinic_id", "date", name="uq_closure_clinic_date"),
        CheckConstraint("char_length(trim(reason)) > 0", name="chk_closure_reason_not_empty"),
        CheckConstraint("all_day = true", name="chk_closure_all_day_only"),
    )


class Service(Base):
    """Dental services offered by the clinic.

    Important distinction:
    - ``cancellation_window_hours`` is a numeric rule consumed programmatically by
      the booking engine (Phase 4/5) to check whether an appointment can be cancelled.
    - ``ClinicPolicy`` (below) stores human-readable conversational copy for the receptionist.
    """

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Length of procedure in minutes"
    )
    buffer_minutes: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False, doc="Post-service sanitation buffer before next slot"
    )
    price_inr: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True, doc="Price in Indian Rupees; nullable for inquiry-only"
    )
    min_lead_time_hours: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False, doc="Minimum advance notice required to book"
    )
    cancellation_window_hours: Mapped[int] = mapped_column(
        Integer, default=24, nullable=False, doc="Minimum cancellation notice in hours"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clinic: Mapped[BusinessProfile] = relationship("BusinessProfile", back_populates="services")

    __table_args__ = (
        CheckConstraint("char_length(trim(name)) > 0", name="chk_service_name_not_empty"),
        CheckConstraint("duration_minutes > 0", name="chk_service_duration_positive"),
        CheckConstraint("buffer_minutes >= 0", name="chk_service_buffer_non_negative"),
        CheckConstraint(
            "price_inr IS NULL OR price_inr >= 0", name="chk_service_price_non_negative"
        ),
        CheckConstraint("min_lead_time_hours >= 0", name="chk_service_min_lead_non_negative"),
        CheckConstraint(
            "cancellation_window_hours >= 0", name="chk_service_cancellation_window_non_negative"
        ),
    )


class ClinicPolicy(Base):
    """Spoken clinic policies explaining cancellation terms, payment, or general boundaries."""

    __tablename__ = "clinic_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_type: Mapped[str] = mapped_column(
        String(80), nullable=False, doc="Identifier e.g. 'cancellation', 'general'"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(
        Text, nullable=False, doc="Spoken explanatory text for conversational interaction"
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clinic: Mapped[BusinessProfile] = relationship("BusinessProfile", back_populates="policies")

    __table_args__ = (
        UniqueConstraint("clinic_id", "policy_type", name="uq_policy_clinic_type"),
        CheckConstraint("char_length(trim(policy_type)) > 0", name="chk_policy_type_not_empty"),
        CheckConstraint("char_length(trim(title)) > 0", name="chk_policy_title_not_empty"),
        CheckConstraint("char_length(trim(content)) > 0", name="chk_policy_content_not_empty"),
    )


class FAQ(Base):
    """Frequently Asked Questions grounded strictly in PRD §5.4.

    Missing topics (such as parking, unconfigured payment methods, insurance)
    are intentionally omitted from FAQs so the agent states that the information
    is not on file and offers human follow-up.
    """

    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic: Mapped[str] = mapped_column(
        String(80), nullable=False, doc="Category e.g. 'location', 'hours', 'appointments'"
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clinic: Mapped[BusinessProfile] = relationship("BusinessProfile", back_populates="faqs")

    __table_args__ = (
        CheckConstraint("char_length(trim(topic)) > 0", name="chk_faq_topic_not_empty"),
        CheckConstraint("char_length(trim(question)) > 0", name="chk_faq_question_not_empty"),
        CheckConstraint("char_length(trim(answer)) > 0", name="chk_faq_answer_not_empty"),
    )
