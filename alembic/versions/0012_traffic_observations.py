"""Guarda mediciones de TomTom por tramo y hora para detectar congestión repetida."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_traffic_observations"
down_revision = "0011_order_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tabla nueva: no reescribe tablas existentes. Si el baseline ya la creó, no se repite.
    if "traffic_observations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "traffic_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "route_id", sa.Uuid(),
            sa.ForeignKey("routes.id", ondelete="SET NULL", onupdate="CASCADE"),
        ),
        sa.Column("from_stop", sa.String(32), nullable=False),
        sa.Column("to_stop", sa.String(32), nullable=False),
        sa.Column("multiplier", sa.Float()),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("road_closure", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("point_lng", sa.Float(), nullable=False),
        sa.Column("point_lat", sa.Float(), nullable=False),
        sa.Column("hour_of_day", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "traffic_observations_observed_hour_idx",
        "traffic_observations",
        ["observed_at", "hour_of_day"],
    )


def downgrade() -> None:
    op.drop_index("traffic_observations_observed_hour_idx", table_name="traffic_observations")
    op.drop_table("traffic_observations")
