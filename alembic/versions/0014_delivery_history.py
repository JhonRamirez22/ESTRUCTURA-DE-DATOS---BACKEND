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
    if _EVENTS not in inspector.get_table_names():
        op.create_table(
            _EVENTS,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "delivery_point_id", sa.Uuid(),
                sa.ForeignKey(f"{_POINTS}.id", ondelete="SET NULL", onupdate="CASCADE"),
            ),
            sa.Column("order_ref", sa.Text(), nullable=False),
            sa.Column("event_type", sa.String(24), nullable=False),
            sa.Column("from_status", sa.String(16)),
            sa.Column("to_status", sa.String(16)),
            sa.Column("route_id", sa.Uuid()),
            sa.Column("courier_id", sa.Uuid()),
            sa.Column("actor_role", sa.String(16), nullable=False),
            sa.Column("actor_account_id", sa.Uuid()),
            sa.Column("detail", sa.String(200)),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "delivery_events_point_time_idx", _EVENTS, ["delivery_point_id", "occurred_at"]
        )
        op.create_index("delivery_events_time_idx", _EVENTS, ["occurred_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _EVENTS in inspector.get_table_names():
        op.drop_table(_EVENTS)
    if _SCHEDULE_INDEX in {ix["name"] for ix in inspector.get_indexes(_POINTS)}:
        op.drop_index(_SCHEDULE_INDEX, table_name=_POINTS)
    columns = {col["name"] for col in inspector.get_columns(_POINTS)}
    for name in ("scheduled_for", "delivered_at", "created_at"):
        if name in columns:
            op.drop_column(_POINTS, name)
