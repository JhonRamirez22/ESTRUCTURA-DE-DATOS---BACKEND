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
