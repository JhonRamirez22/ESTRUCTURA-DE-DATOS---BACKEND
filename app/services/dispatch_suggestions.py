"""Sugerencias en contexto para el despachador: el cálculo decide, la IA solo redacta.

Cada sugerencia sale de una regla determinista sobre el estado real (repartidores y pedidos
pendientes) y trae su cifra verificada y, si procede, una acción de un clic. Groq puede
reescribir el texto, pero solo se acepta si cada cifra del texto es una de las verificadas y
el nombre del repartidor sigue siendo el marcador ``{courier}``: los nombres y direcciones
nunca viajan a la IA. Si Groq falla, no está configurado o se pasó del tope, se muestra el
texto determinista, que dice lo mismo.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.location.proximity import haversine_m
from app.models.enums import CourierStatus
from app.services.location_intelligence import (
    IntelligenceUnavailable,
    _chat_json,
    verify_explanation,
)
from app.services.optimization_feed import record_incident

MAX_SUGGESTIONS = 5
MAX_ORDERS_PER_ACTION = 3
NEARBY_RADIUS_M = 3_000.0
FRESH_MINUTES = 2.0
STALE_ON_ROUTE_MINUTES = 10.0
AI_CALLS_PER_DAY = 200

_PLACEHOLDER = "{courier}"
_PHRASE_PROMPT = (
    "Redactas avisos breves para un despachador de mensajería. Recibes una lista de avisos "
    "con 'id', 'kind' y 'figures' verificadas. Devuelve solo JSON "
    '{"texts":{"<id>":"frase"}} en español, una frase corta por aviso. Si el aviso nombra al '
    "repartidor, escribe exactamente {courier} donde iría su nombre. Usa solo las cifras dadas; "
    "no inventes calles, causas ni tiempos."
)


@dataclass(frozen=True, slots=True)
class CourierView:
    ident: str
    name: str
    status: CourierStatus
    lat: float | None
    lng: float | None
    last_location_at: datetime | None


@dataclass(frozen=True, slots=True)
class PendingView:
    ident: str
    lat: float
