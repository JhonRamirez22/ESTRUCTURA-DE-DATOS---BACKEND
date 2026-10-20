"""Datos sintéticos de demostración y limpieza de datos operativos.

Todo es ficticio: nombres con prefijo "Demo", pedidos DEMO-NNN, sin correos ni teléfonos de
clientes (los avisos externos quedan apagados) y coordenadas aproximadas dentro de Pasto.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.passwords import hash_password
from app.models import (
    Account,
    Courier,
    CustomerNotificationOutbox,
    DeliveryPoint,
    LocationEvent,
    Notification,
    Route,
    RouteRevision,
)
from app.models.account import Account as AccountModel
from app.models.enums import CourierStatus, DeliveryPointStatus

DEMO_NAMESPACE = uuid.UUID("7c1d2f40-5b7e-4c53-9d0e-2a4f6b8d9e10")
MIN_DEMO_PASSWORD_LENGTH = 12
DISPATCHER_EMAIL = "despacho.demo@rutaspasto.example"
COURIER_EMAIL = "repartidor.demo@rutaspasto.example"

# (nombre, teléfono ficticio, lat, lng)
DEMO_COURIERS: tuple[tuple[str, str, float, float], ...] = (
    ("Demo · Carlos M.", "3000000001", 1.2136, -77.2811),
    ("Demo · Laura P.", "3000000002", 1.2175, -77.2760),
    ("Demo · Andrés R.", "3000000003", 1.2090, -77.2850),
    ("Demo · Sofía G.", "3000000004", 1.2230, -77.2890),
)

# (dirección, lat, lng); coordenadas aproximadas, no verificadas casa por casa.
DEMO_ORDERS: tuple[tuple[str, float, float], ...] = (
    ("Carrera 25 # 17-50, Centro", 1.2142, -77.2790),
    ("Calle 18 # 22-14, Centro", 1.2158, -77.2802),
    ("Avenida Idema # 5-30, Las Lunas", 1.2105, -77.2745),
    ("Calle 20 # 34-18, Santiago", 1.2189, -77.2735),
    ("Carrera 27 # 11-40, La Colina", 1.2070, -77.2830),
    ("Transversal 4 # 12-25, Torobajo", 1.2290, -77.2905),
    ("Calle 12 # 40-10, Pandiaco", 1.2005, -77.2770),
    ("Carrera 30 # 19-60, Palermo", 1.2205, -77.2840),
    ("Calle 16 # 9-35, Chambú", 1.2118, -77.2870),
    ("Avenida Los Estudiantes # 22-12", 1.2245, -77.2800),
    ("Carrera 22 # 8-48, Lorenzo de Aguirre", 1.2060, -77.2795),
    ("Calle 21 # 28-33, El Ejido", 1.2178, -77.2825),
    ("Diagonal 16 # 30-20, Bomboná", 1.2150, -77.2858),
    ("Carrera 36 # 14-05, Maridíaz", 1.2125, -77.2710),
)

# Orden seguro de borrado: primero lo que referencia, al final los repartidores.
_OPERATIONAL_TABLES = (
    CustomerNotificationOutbox,
    Notification,
    LocationEvent,
    RouteRevision,
    DeliveryPoint,
    Route,
    Courier,
)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    couriers: int
    orders_created: int
    accounts: int


def demo_id(name: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, name)


async def seed_demo(
    session: AsyncSession,
    *,
    now: datetime,
    dispatcher_password: str | None = None,
    courier_password: str | None = None,
) -> SeedSummary:
    """Crea o refresca los datos de demo. Es idempotente: no duplica ni pisa rutas existentes."""
    for _name, _phone, lat, lng in DEMO_COURIERS:
        _require_inside_pasto(lat, lng)
    for _address, lat, lng in DEMO_ORDERS:
        _require_inside_pasto(lat, lng)

    for name, phone, lat, lng in DEMO_COURIERS:
        courier = await session.get(Courier, demo_id(f"courier:{name}"))
        if courier is None:
            session.add(
                Courier(
                    id=demo_id(f"courier:{name}"), name=name, phone=phone,
                    status=CourierStatus.AVAILABLE, current_lat=lat, current_lng=lng,
                    last_location_at=now,
                )
            )
        elif courier.status == CourierStatus.AVAILABLE:
            # Reejecutar el script renueva el GPS vencido para la demostración.
            courier.current_lat, courier.current_lng, courier.last_location_at = lat, lng, now

    created = 0
    for index, (address, lat, lng) in enumerate(DEMO_ORDERS, start=1):
        order_id = f"DEMO-{index:03d}"
        exists = await session.scalar(
            select(DeliveryPoint.id).where(DeliveryPoint.order_id == order_id)
        )
        if exists is None:
            session.add(
                DeliveryPoint(
                    id=demo_id(f"order:{order_id}"), address=address, lat=lat, lng=lng,
                    status=DeliveryPointStatus.PENDING, order_id=order_id,
                )
            )
            created += 1
    await session.flush()

    accounts = 0
    if dispatcher_password:
        accounts += await _upsert_account(
            session, DISPATCHER_EMAIL, dispatcher_password, "dispatcher", None
        )
    if courier_password:
        accounts += await _upsert_account(
            session, COURIER_EMAIL, courier_password, "courier",
            demo_id(f"courier:{DEMO_COURIERS[0][0]}"),
        )
    await session.commit()
    return SeedSummary(len(DEMO_COURIERS), created, accounts)


async def reset_operational_data(session: AsyncSession) -> dict[str, int]:
    """Borra pedidos, rutas, repartidores y eventos. Conserva cuentas, sesiones y auditoría."""
    counts: dict[str, int] = {}
    for model in _OPERATIONAL_TABLES:
        total = await session.scalar(select(func.count()).select_from(model))
        counts[model.__tablename__] = int(total or 0)
    for model in _OPERATIONAL_TABLES:
        await session.execute(delete(model))
    # Las cuentas de repartidor referencian couriers con ON DELETE CASCADE: se retiran solas.
    await session.commit()
    return counts


async def count_operational_data(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in _OPERATIONAL_TABLES:
        total = await session.scalar(select(func.count()).select_from(model))
        counts[model.__tablename__] = int(total or 0)
    return counts


async def _upsert_account(
    session: AsyncSession, email: str, password: str, role: str, courier_id: uuid.UUID | None,
) -> int:
    if len(password) < MIN_DEMO_PASSWORD_LENGTH:
        raise ValueError(f"La contraseña de {email} debe tener al menos 12 caracteres.")
    account = await session.scalar(select(Account).where(Account.email == email))
    if account is None:
        session.add(
            AccountModel(
                email=email, password_hash=hash_password(password), role=role,
                courier_id=courier_id, enabled=True,
            )
        )
    else:
        account.password_hash = hash_password(password)
        account.enabled = True
    return 1


def _require_inside_pasto(lat: float, lng: float) -> None:
    if not is_within_pasto_service_area(lat, lng):
        raise ValueError("Una coordenada de demo quedó fuera de la zona de servicio de Pasto.")
