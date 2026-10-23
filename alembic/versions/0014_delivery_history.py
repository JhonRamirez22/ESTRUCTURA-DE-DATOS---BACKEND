"""Shipment history (delivery_events) plus creation, delivery and scheduled timestamps."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_delivery_history"
down_revision = "0013_order_priority"
branch_labels = None
depends_on = None

_POINTS = "delivery_points"
_EVENTS = "delivery_events"
_SCHEDULE_INDEX = "delivery_points_scheduled_for_idx"


def upgrade() -> None:
    # A fresh database already has everything (the initial migration builds tables from the
    # models); an existing one receives each piece here. Every step is idempotent and additive.
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns(_POINTS)}
    if "created_at" not in columns:
        # Existing rows get the migration time: their real creation time was never recorded.
        op.add_column(
            _POINTS,
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if "delivered_at" not in columns:
        op.add_column(_POINTS, sa.Column("delivered_at", sa.DateTime(), nullable=True))
    if "scheduled_for" not in columns:
        op.add_column(_POINTS, sa.Column("scheduled_for", sa.DateTime(), nullable=True))
    if _SCHEDULE_INDEX not in {ix["name"] for ix in inspector.get_indexes(_POINTS)}:
        op.create_index(_SCHEDULE_INDEX, _POINTS, ["scheduled_for"])
