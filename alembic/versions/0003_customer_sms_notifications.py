"""Agrega consentimiento SMS y destinatarios por canal sin perder la outbox existente."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, String, inspect, text
from sqlalchemy.dialects.postgresql import ENUM

from alembic import op

revision = "0003_customer_sms_notifications"
down_revision = "0002_customer_email_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    delivery_columns = {column["name"] for column in inspector.get_columns("delivery_points")}
    if "customer_phone" not in delivery_columns:
        op.add_column("delivery_points", Column("customer_phone", String(20), nullable=True))
    if "sms_notifications_enabled" not in delivery_columns:
        op.add_column(
            "delivery_points",
            Column(
                "sms_notifications_enabled",
                Boolean,
                nullable=False,
