"""Adopta el schema existente de Prisma sin eliminar ni reemplazar datos.

Revision ID: 0001_adopt_prisma_schema
Revises:
Create Date: 2026-09-29
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.schema import ForeignKeyConstraint, Index, UniqueConstraint

import app.models  # noqa: F401 — registra todos los modelos en metadata.
from alembic import op
from app.db.base import Base
from app.db.schema_validation import actual_unique_signatures

revision = "0001_adopt_prisma_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # checkfirst crea solo objetos ausentes y conserva íntegros los ya creados por Prisma.
    Base.metadata.create_all(bind=bind, checkfirst=True)
    inspector = inspect(bind)
    missing_columns: list[str] = []
    for table in Base.metadata.sorted_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in actual_columns
        )
    if missing_columns:
        raise RuntimeError(
            "El schema PostgreSQL existente no coincide con los modelos SQLAlchemy: "
            + ", ".join(missing_columns)
        )

    missing_objects = _find_missing_constraints_and_indexes(inspector)
    if missing_objects:
        raise RuntimeError(
            "El schema PostgreSQL existente no conserva las restricciones e índices "
            "requeridos por los modelos SQLAlchemy: "
            + ", ".join(missing_objects)
        )


def downgrade() -> None:
    # El baseline puede estar adoptando datos de producción; nunca los borra al revertir.
    pass


def _find_missing_constraints_and_indexes(inspector: Inspector) -> list[str]:
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        table_name = table.name
        actual_primary_key = tuple(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
