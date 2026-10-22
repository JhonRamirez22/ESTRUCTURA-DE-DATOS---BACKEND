"""Permite que la outbox guarde solo el destino correspondiente al canal."""

from __future__ import annotations

from sqlalchemy import String

from alembic import op

revision = "0004_sms_recipient_nullable"
down_revision = "0003_customer_sms_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("customer_notification_outbox") as batch_op:
            batch_op.alter_column(
                "recipient_email",
                existing_type=String(320),
                existing_nullable=False,
                nullable=True,
            )
    else:
        op.alter_column(
            "customer_notification_outbox",
            "recipient_email",
            existing_type=String(320),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    # No se vuelve NOT NULL porque los avisos SMS usan recipient_phone.
    pass
