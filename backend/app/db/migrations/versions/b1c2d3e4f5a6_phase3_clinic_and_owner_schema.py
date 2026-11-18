"""Phase 3 clinic configuration and owner user schema.

Revision ID: b1c2d3e4f5a6
Revises: 12ddd6604a2a
Create Date: 2026-11-13 01:53:00.000000

Creates the canonical BrightSmile clinic configuration tables:
- business_profile
- business_hours
- business_closures
- services
- clinic_policies
- faqs
- owner_users (schema foundation for Phase 7 authentication)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "12ddd6604a2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. business_profile
    op.create_table(
        "business_profile",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("address_line1", sa.String(length=300), nullable=False),
        sa.Column("address_line2", sa.String(length=300), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("postal_code", sa.String(length=20), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False, server_default="India"),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("timezone", sa.String(length=80), nullable=False, server_default="Asia/Kolkata"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("char_length(trim(slug)) > 0", name="chk_profile_slug_not_empty"),
        sa.CheckConstraint("char_length(trim(name)) > 0", name="chk_profile_name_not_empty"),
        sa.CheckConstraint(
            "char_length(trim(timezone)) > 0", name="chk_profile_timezone_not_empty"
        ),
    )
    op.create_index(op.f("ix_business_profile_slug"), "business_profile", ["slug"], unique=True)

    # 2. business_hours
    op.create_table(
        "business_hours",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.SmallInteger(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("open_time", sa.Time(), nullable=True),
        sa.Column("close_time", sa.Time(), nullable=True),
        sa.Column("break_start", sa.Time(), nullable=True),
        sa.Column("break_end", sa.Time(), nullable=True),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_hours_clinic_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "day_of_week", name="uq_hours_clinic_day"),
        sa.CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="chk_hours_day_range"),
        sa.CheckConstraint(
            "(is_closed = true AND open_time IS NULL AND close_time IS NULL "
            "AND break_start IS NULL AND break_end IS NULL) OR "
            "(is_closed = false AND open_time IS NOT NULL AND close_time IS NOT NULL "
            "AND close_time > open_time)",
            name="chk_hours_open_close_times",
        ),
        sa.CheckConstraint(
            "(is_closed = true) OR "
            "(break_start IS NULL AND break_end IS NULL) OR "
            "(is_closed = false AND break_start IS NOT NULL AND break_end IS NOT NULL "
            "AND break_end > break_start AND break_start >= open_time AND break_end <= close_time)",
            name="chk_hours_break_interval",
        ),
    )
    op.create_index(op.f("ix_business_hours_clinic_id"), "business_hours", ["clinic_id"])

    # 3. business_closures
    op.create_table(
        "business_closures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default="true"),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_closures_clinic_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "date", name="uq_closure_clinic_date"),
        sa.CheckConstraint("char_length(trim(reason)) > 0", name="chk_closure_reason_not_empty"),
        sa.CheckConstraint("all_day = true", name="chk_closure_all_day_only"),
    )
    op.create_index(op.f("ix_business_closures_clinic_id"), "business_closures", ["clinic_id"])

    # 4. services
    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("price_inr", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("min_lead_time_hours", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("cancellation_window_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_services_clinic_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("char_length(trim(name)) > 0", name="chk_service_name_not_empty"),
        sa.CheckConstraint("duration_minutes > 0", name="chk_service_duration_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="chk_service_buffer_non_negative"),
        sa.CheckConstraint(
            "price_inr IS NULL OR price_inr >= 0", name="chk_service_price_non_negative"
        ),
        sa.CheckConstraint("min_lead_time_hours >= 0", name="chk_service_min_lead_non_negative"),
        sa.CheckConstraint(
            "cancellation_window_hours >= 0",
            name="chk_service_cancellation_window_non_negative",
        ),
    )
    op.create_index(op.f("ix_services_clinic_id"), "services", ["clinic_id"])

    # 5. clinic_policies
    op.create_table(
        "clinic_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("policy_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_policies_clinic_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "policy_type", name="uq_policy_clinic_type"),
        sa.CheckConstraint("char_length(trim(policy_type)) > 0", name="chk_policy_type_not_empty"),
        sa.CheckConstraint("char_length(trim(title)) > 0", name="chk_policy_title_not_empty"),
        sa.CheckConstraint("char_length(trim(content)) > 0", name="chk_policy_content_not_empty"),
    )
    op.create_index(op.f("ix_clinic_policies_clinic_id"), "clinic_policies", ["clinic_id"])

    # 6. faqs
    op.create_table(
        "faqs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_faqs_clinic_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("char_length(trim(topic)) > 0", name="chk_faq_topic_not_empty"),
        sa.CheckConstraint("char_length(trim(question)) > 0", name="chk_faq_question_not_empty"),
        sa.CheckConstraint("char_length(trim(answer)) > 0", name="chk_faq_answer_not_empty"),
    )
    op.create_index(op.f("ix_faqs_clinic_id"), "faqs", ["clinic_id"])

    # 7. owner_users (Phase 3 schema foundation; auth in Phase 7)
    op.create_table(
        "owner_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("char_length(trim(email)) > 0", name="chk_owner_email_not_empty"),
    )
    op.create_index(op.f("ix_owner_users_email"), "owner_users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_table("owner_users")
    op.drop_table("faqs")
    op.drop_table("clinic_policies")
    op.drop_table("services")
    op.drop_table("business_closures")
    op.drop_table("business_hours")
    op.drop_table("business_profile")
