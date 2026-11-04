"""Siembra cuentas y datos sintéticos para pruebas de carga. SOLO contra una base local aislada.

Uso: DATABASE_URL=... uv run python -m tests.load.seed_load_data [repartidores] [despachadores]
Genera correos c{n}@carga.co y d{n}@carga.co con una contraseña común (LOAD_PASSWORD) y guías
guia-carga-{n}. Aborta si la base no es local, para no tocar datos reales por error.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime

from sqlalchemy import insert

from app.core.passwords import hash_password
from app.db.session import get_session_factory
from app.models import Account, Courier, DeliveryPoint, Route
from app.models.enums import CourierStatus, DeliveryPointStatus, RouteStatus
from app.settings import get_settings

PASSWORD = os.environ.get("LOAD_PASSWORD", "Carga-Masiva-2026-Zz1")


async def main(couriers: int, dispatchers: int) -> None:
    settings = get_settings()
    host = str(settings.sqlalchemy_database_url)
    if not any(marker in host for marker in ("localhost", "127.0.0.1")):
        raise SystemExit("Solo se siembra una base local.")
    password_hash = hash_password(PASSWORD)  # una sola vez: miles de cuentas comparten el hash
    now = datetime.now()
    courier_rows, account_rows, route_rows, point_rows = [], [], [], []
    for n in range(couriers):
        courier_id, route_id = uuid.uuid4(), uuid.uuid4()
        courier_rows.append({
            "id": courier_id, "name": f"Repartidor {n}", "phone": "3000000000",
