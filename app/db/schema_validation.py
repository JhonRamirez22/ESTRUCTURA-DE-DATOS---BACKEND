"""Utilidades de reflexión de schema para adopción segura de bases existentes."""

from __future__ import annotations

from sqlalchemy.engine.reflection import Inspector

UniqueSignature = tuple[str | None, tuple[str, ...]]


def actual_unique_signatures(inspector: Inspector, table_name: str) -> set[UniqueSignature]:
    """Normaliza UNIQUE constraints e índices únicos a la misma representación."""
    signatures = {
        (
            constraint.get("name"),
            tuple(column for column in constraint.get("column_names") or () if column is not None),
        )
        for constraint in inspector.get_unique_constraints(table_name)
    }
    signatures.update(
        (
            index.get("name"),
            tuple(column for column in index.get("column_names") or () if column is not None),
        )
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    return signatures
