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
                server_default=text("false"),
            ),
        )

    outbox_columns = {
        column["name"] for column in inspect(bind).get_columns("customer_notification_outbox")
    }
    channel_type = ENUM(
        "EMAIL",
        "SMS",
        name="customer_notification_channel",
        create_type=False,
    )
    if bind.dialect.name == "postgresql":
        channel_type.create(bind, checkfirst=True)
    if "channel" not in outbox_columns:
        op.add_column(
            "customer_notification_outbox",
            Column("channel", channel_type, nullable=False, server_default="EMAIL"),
        )
    if "recipient_phone" not in outbox_columns:
        op.add_column(
            "customer_notification_outbox",
            Column("recipient_phone", String(20), nullable=True),
        )
def downgrade() -> None:
    # Evita borrar destinatarios y consentimientos sin un proceso explícito de retención.
    pass
