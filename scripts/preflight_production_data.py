"""Comprueba duplicados de order_id sin mostrar datos de pedidos ni credenciales."""

from __future__ import annotations

import asyncio
import re
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.settings import get_settings

_SAFE_ERROR_CODE = re.compile(r"^[A-Z0-9_]+$")


class DuplicateOrderIdGroupsError(RuntimeError):
    def __init__(self, duplicate_groups: int) -> None:
        super().__init__(
            f"El preflight encontró {duplicate_groups} grupos con order_id duplicado. "
            "No se aplicaron migraciones; corrige esos registros y vuelve a desplegar."
        )
        self.duplicate_groups = duplicate_groups


def get_safe_database_error_code(error: object) -> str | None:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        for attribute in ("sqlstate", "pgcode", "code"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value):
                return value
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return None


async def assert_no_duplicate_order_ids(connection: AsyncConnection) -> None:
    result = await connection.execute(
        text(
            """
