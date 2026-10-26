"""Siembra un día de demostración SIMULADO: pedidos por zonas y tráfico por hora.

Todo lo creado lleva el prefijo SIM- en la referencia del pedido, para distinguirlo de
datos reales. Sin --apply solo muestra qué haría.

Uso:  python scripts/seed_simulated_day.py            # simulación
      python scripts/seed_simulated_day.py --apply    # escribe en la base de datos
"""

from __future__ import annotations

import argparse
import asyncio
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models import DeliveryPoint, DeliveryPointStatus, TrafficObservation

PREFIX = "SIM-"
# Tres zonas de Pasto donde se agrupan los pedidos del día simulado.
ZONES: dict[str, tuple[float, float]] = {
    "Centro": (1.2136, -77.2811),
    "Las Lunas": (1.2172, -77.2744),
    "Pandiaco": (1.2260, -77.2870),
}
ORDERS_PER_ZONE = 4
# Picos de tráfico: 7–8 h (salida) y 17–19 h (regreso); el resto fluye.
PEAK_HOURS = {7: 1.9, 8: 1.7, 17: 2.1, 18: 2.4, 19: 1.8}


def simulated_orders() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    counter = 1
    for zone, (lat, lng) in ZONES.items():
        for index in range(ORDERS_PER_ZONE):
            # Desplazamiento pequeño y determinista: unos 80–250 m alrededor del centro.
            angle = 2 * math.pi * index / ORDERS_PER_ZONE
            radius = 0.0009 + 0.0003 * index
            rows.append(
                {
                    "order_id": f"{PREFIX}{counter:03d}",
                    "address": f"{zone} · punto simulado {index + 1}",
                    "lat": round(lat + radius * math.sin(angle), 6),
                    "lng": round(lng + radius * math.cos(angle), 6),
                }
            )
            counter += 1
    return rows


def simulated_traffic(now: datetime) -> list[TrafficObservation]:
    """Un día completo de mediciones sobre un tramo ficticio entre dos zonas."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[TrafficObservation] = []
    for hour in range(24):
        multiplier = PEAK_HOURS.get(hour, 1.05)
        level = "MUY_ALTO" if multiplier >= 2.0 else "ALTO" if multiplier >= 1.5 else "FLUIDO"
        observed = day + timedelta(hours=hour, minutes=5)
