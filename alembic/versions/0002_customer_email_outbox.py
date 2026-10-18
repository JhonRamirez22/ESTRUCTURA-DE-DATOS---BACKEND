"""Agrega consentimiento de correo y una outbox idempotente para clientes.

Revision ID: 0002_customer_email_outbox
Revises: 0001_adopt_prisma_schema
Create Date: 2026-10-03
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM

from alembic import op

revision = "0002_customer_email_outbox"
down_revision = "0001_adopt_prisma_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "delivery_points" not in inspector.get_table_names():
        raise RuntimeError("No existe delivery_points; aplica primero el baseline de Alembic.")

    existing_columns = {column["name"] for column in inspector.get_columns("delivery_points")}
    if "customer_email" not in existing_columns:
        op.add_column("delivery_points", Column("customer_email", String(320), nullable=True))
    if "email_notifications_enabled" not in existing_columns:
        op.add_column(
            "delivery_points",
            Column(
                "email_notifications_enabled",
                Boolean,
                nullable=False,
                server_default=text("false"),
            ),
        )

    if "customer_notification_outbox" not in inspect(bind).get_table_names():
        # Los tipos se crean explícitamente con checkfirst; desactivar el DDL
        # automático evita que create_table intente crearlos una segunda vez.
        event_type = ENUM(
            "ORDER_ASSIGNED",
            "COURIER_NEAR",
            name="customer_notification_event",
            create_type=False,
        )
        status_type = ENUM(
            "PENDING",
            "PROCESSING",
            "SENT",
            "CANCELLED",
            name="customer_notification_status",
            create_type=False,
        )
        if bind.dialect.name == "postgresql":
            event_type.create(bind, checkfirst=True)
            status_type.create(bind, checkfirst=True)
        op.create_table(
            "customer_notification_outbox",
            Column("id", Uuid(as_uuid=True), primary_key=True, nullable=False),
            Column(
                "delivery_point_id",
                Uuid(as_uuid=True),
                nullable=False,
            ),
            Column("event_key", String(220), nullable=False),
            Column("event_type", event_type, nullable=False),
            Column("recipient_email", String(320), nullable=False),
            Column("subject", String(180), nullable=False),
            Column("text_body", Text, nullable=False),
            Column("status", status_type, nullable=False, server_default="PENDING"),
            Column("attempt_count", Integer, nullable=False, server_default="0"),
            Column(
                "available_at",
                DateTime(timezone=False),
                nullable=False,
                server_default=text("CURRENT_TIMESTAMP"),
            ),
            Column("lease_expires_at", DateTime(timezone=False), nullable=True),
            Column(
                "created_at",
                DateTime(timezone=False),
                nullable=False,
                server_default=text("CURRENT_TIMESTAMP"),
            ),
            Column("sent_at", DateTime(timezone=False), nullable=True),
            Column("last_error_code", String(100), nullable=True),
            ForeignKeyConstraint(
                ["delivery_point_id"],
                ["delivery_points.id"],
                name="customer_notification_outbox_delivery_point_id_fkey",
                ondelete="CASCADE",
                onupdate="CASCADE",
            ),
            UniqueConstraint("event_key", name="customer_notification_outbox_event_key_key"),
        )

    existing_indexes = {
        index["name"] for index in inspect(bind).get_indexes("customer_notification_outbox")
    }
    if "customer_notification_outbox_status_available_idx" not in existing_indexes:
        op.create_index(
            "customer_notification_outbox_status_available_idx",
            "customer_notification_outbox",
            ["status", "available_at"],
        )
    if "customer_notification_outbox_delivery_point_idx" not in existing_indexes:
        op.create_index(
            "customer_notification_outbox_delivery_point_idx",
            "customer_notification_outbox",
            ["delivery_point_id"],
        )


def downgrade() -> None:
    # No se borran consentimientos, destinatarios ni eventos para aparentar un rollback seguro.
    pass
