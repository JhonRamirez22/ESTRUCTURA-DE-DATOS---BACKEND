"""Registra cuándo se optimizó por última vez cada ruta."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_route_optimized_at"
down_revision = "0007_shared_chat_rate_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("routes")}
    if "optimized_at" not in columns:
        op.add_column("routes", sa.Column("optimized_at", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE routes SET optimized_at = COALESCE(optimized_at, updated_at, created_at)"
        )
    )


def downgrade() -> None:
    op.drop_column("routes", "optimized_at")
