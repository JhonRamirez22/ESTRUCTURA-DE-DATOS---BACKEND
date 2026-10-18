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
            database,
            app_username,
        )
        grant_schema = await connection.fetchval(
            "SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', $1)", app_username
        )
        if not isinstance(grant_database, str) or not isinstance(grant_schema, str):
            raise RuntimeError("No se pudieron preparar los permisos de la aplicación")
        await connection.execute(grant_database)
        await connection.execute(grant_schema)
    finally:
        await connection.close()

    _LOGGER.info("Se verificó el rol PostgreSQL de la aplicación y sus permisos mínimos.")


def _required(value: str | None, name: str) -> str:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        raise ValueError(f"{name} debe configurarse para inicializar el rol de PostgreSQL")
    return normalized


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} debe configurarse para inicializar el rol de PostgreSQL")
    secret = value.get_secret_value().strip()
    if not secret:
        raise ValueError(f"{name} debe configurarse para inicializar el rol de PostgreSQL")
    return secret


def _validate_identifier(value: str, name: str) -> None:
    if not _ROLE_PATTERN.fullmatch(value):
        raise ValueError(f"{name} debe usar solo letras, números y guion bajo")


def _ssl_argument(settings: Settings) -> bool | str | ssl.SSLContext:
    mode = settings.database_ssl_mode
    if mode == "disable":
        return False
    if mode == "require":
        return "require"
    if not settings.database_ssl_root_cert:
        raise ValueError("DATABASE_SSL_ROOT_CERT es requerido al verificar el certificado de RDS")

    context = ssl.create_default_context(cafile=settings.database_ssl_root_cert)
    context.check_hostname = mode == "verify-full"
    return context


async def _main() -> None:
    await bootstrap_database_role(get_settings())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
