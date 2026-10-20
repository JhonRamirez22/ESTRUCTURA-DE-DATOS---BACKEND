"""Agrega el estado REQUESTED para solicitudes de clientes pendientes de aprobación."""

from __future__ import annotations

from alembic import op

revision = "0011_order_requests"
down_revision = "0010_revocable_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Solo añade un valor al tipo: no reescribe filas ni bloquea la tabla.
        op.execute("ALTER TYPE delivery_point_status ADD VALUE IF NOT EXISTS 'REQUESTED'")


def downgrade() -> None:
    # PostgreSQL no retira valores de un enum in-place. Para que una versión anterior de la
    # API pueda leer la tabla, se descartan las solicitudes aún no aceptadas: nunca
    # entraron a la operación ni tienen ruta.
    op.execute(
        "DELETE FROM delivery_points WHERE status::text = 'REQUESTED' AND route_id IS NULL"
    )
