"""Agrega el estado terminal para avisos que agotaron sus reintentos."""

from __future__ import annotations

from alembic import op

revision = "0005_customer_notify_deadletter"
down_revision = "0004_sms_recipient_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE customer_notification_status "
            "ADD VALUE IF NOT EXISTS 'FAILED'"
        )


def downgrade() -> None:
    # PostgreSQL no permite retirar un valor de enum de forma segura e in-place.
    pass
