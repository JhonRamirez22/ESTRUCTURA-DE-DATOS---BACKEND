"""Añade la prioridad del pedido (1 normal, 2 alta, 3 urgente) para ventanas horarias."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_order_priority"
down_revision = "0012_traffic_observations"
branch_labels = None
depends_on = None

_TABLE = "delivery_points"
_CHECK = "delivery_points_priority_range"


def _state() -> tuple[bool, bool]:
    inspector = sa.inspect(op.get_bind())
    has_column = "priority" in {col["name"] for col in inspector.get_columns(_TABLE)}
    has_check = _CHECK in {check["name"] for check in inspector.get_check_constraints(_TABLE)}
    return has_column, has_check


def upgrade() -> None:
    # Una base nueva ya trae la columna (la migración inicial crea las tablas desde los
    # modelos); una base existente la recibe aquí. Cada paso es idempotente.
    has_column, has_check = _state()
    if not has_column:
        # Valor por defecto constante: en PostgreSQL 11+ no reescribe la tabla.
        op.add_column(
            _TABLE,
            sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="1"),
        )
    if not has_check:
        op.create_check_constraint(_CHECK, _TABLE, "priority BETWEEN 1 AND 3")


def downgrade() -> None:
    has_column, has_check = _state()
    if has_check:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    if has_column:
        op.drop_column(_TABLE, "priority")
