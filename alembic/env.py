"""Configuración Alembic async que carga el schema sin imprimir credenciales."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 — registra todos los modelos en metadata.
from alembic import context
from app.db.base import Base
from app.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = get_settings()
database_url = settings.sqlalchemy_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
