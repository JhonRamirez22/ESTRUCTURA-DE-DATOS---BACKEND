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
        )
        expected_primary_key = tuple(column.name for column in table.primary_key.columns)
        if actual_primary_key != expected_primary_key:
            missing.append(f"{table_name}.primary_key")

        actual_unique = actual_unique_signatures(inspector, table_name)
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            expected = (constraint.name, tuple(column.name for column in constraint.columns))
            if expected not in actual_unique:
                missing.append(f"{table_name}.unique:{constraint.name or expected[1]}")

        actual_foreign_keys = {
            (
                tuple(foreign_key.get("constrained_columns") or ()),
                foreign_key.get("referred_table"),
                tuple(foreign_key.get("referred_columns") or ()),
                _normalize_action(foreign_key.get("options", {}).get("ondelete")),
                _normalize_action(foreign_key.get("options", {}).get("onupdate")),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            elements = tuple(constraint.elements)
            expected_foreign_key = (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
                _normalize_action(constraint.ondelete),
                _normalize_action(constraint.onupdate),
            )
            if expected_foreign_key not in actual_foreign_keys:
                missing.append(
                    f"{table_name}.foreign_key:{','.join(expected_foreign_key[0])}"
                )

        actual_indexes = {
            (
                index.get("name"),
                tuple(index.get("column_names") or ()),
                bool(index.get("unique")),
            )
            for index in inspector.get_indexes(table_name)
        }
        for index in table.indexes:
            if not isinstance(index, Index):
                continue
            expected_index = (
                index.name,
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            if expected_index not in actual_indexes:
                missing.append(f"{table_name}.index:{index.name}")
    return missing


def _normalize_action(value: str | None) -> str | None:
    return value.upper() if value is not None else None
