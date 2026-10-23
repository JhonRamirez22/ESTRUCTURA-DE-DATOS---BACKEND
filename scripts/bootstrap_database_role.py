"""Crea el rol propietario de la aplicación sin entregar el master de RDS al servicio."""

from __future__ import annotations

import asyncio
import logging
import re
import ssl

import asyncpg  # type: ignore[import-untyped]  # asyncpg does not ship typing metadata.
from pydantic import SecretStr

from app.settings import Settings, get_settings

_LOGGER = logging.getLogger(__name__)
_ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


async def bootstrap_database_role(settings: Settings) -> None:
    """Crea o actualiza el rol de la app y limita su alcance a la base/schema propios."""
    host = _required(settings.database_host, "DATABASE_HOST")
    database = _required(settings.database_name, "DATABASE_NAME")
    admin_username = _required(settings.database_admin_username, "DATABASE_ADMIN_USERNAME")
    admin_password = _required_secret(settings.database_admin_password, "DATABASE_ADMIN_PASSWORD")
    app_username = _required(settings.database_username, "DATABASE_USERNAME")
    app_password = _required_secret(settings.database_password, "DATABASE_PASSWORD")
    _validate_identifier(admin_username, "DATABASE_ADMIN_USERNAME")
    _validate_identifier(app_username, "DATABASE_USERNAME")
    _validate_identifier(database, "DATABASE_NAME")

    connection = await asyncpg.connect(
        host=host,
        port=settings.database_port,
        database=database,
        user=admin_username,
        password=admin_password,
        ssl=_ssl_argument(settings),
    )
    try:
        role_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", app_username
        )
        role_statement = await connection.fetchval(
            "SELECT format($1, $2, $3)",
            "ALTER ROLE %I WITH LOGIN PASSWORD %L"
            if role_exists
            else "CREATE ROLE %I WITH LOGIN PASSWORD %L",
            app_username,
            app_password,
        )
        if not isinstance(role_statement, str):
            raise RuntimeError(
                "No se pudo preparar la instrucción segura para el rol de aplicación"
            )
        await connection.execute(role_statement)

        grant_database = await connection.fetchval(
            "SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', $1, $2)",
