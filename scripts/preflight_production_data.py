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
            SELECT COUNT(*)
            FROM (
                SELECT order_id
                FROM delivery_points
                GROUP BY order_id
                HAVING COUNT(*) > 1
            ) AS duplicate_order_groups
            """
        )
    )
    duplicate_groups: int = result.scalar_one()
    if duplicate_groups > 0:
        raise DuplicateOrderIdGroupsError(duplicate_groups)


async def _run_preflight() -> None:
    engine = create_async_engine(get_settings().sqlalchemy_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await assert_no_duplicate_order_ids(connection)
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(_run_preflight())
    except DuplicateOrderIdGroupsError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        error_code = get_safe_database_error_code(error)
        error_label = f"{type(error).__name__}:{error_code}" if error_code else type(error).__name__
        print(
            "No se pudo validar la integridad de order_id "
            f"({error_label}); el despliegue se detuvo antes de migrar. "
            "Revisa DATABASE_URL y la conectividad.",
            file=sys.stderr,
        )
        return 1

    print("Preflight correcto: no hay grupos con order_id duplicado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
