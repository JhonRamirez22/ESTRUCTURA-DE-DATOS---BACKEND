"""Cliente opcional de IA para estimar factores de tráfico por tramo."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Protocol

import httpx

from app.core.ai.assignment_traffic import (
    AssignmentTrafficAdvice,
    AssignmentTrafficRequest,
    parse_assignment_traffic_response,
)
from app.core.ai.route_traffic import (
    RouteTrafficAdvice,
    RouteTrafficAiRequest,
    parse_route_traffic_response,
)
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
_SYSTEM_PROMPT = " ".join(
    (
        "Eres un asesor de tráfico para un optimizador de rutas en Pasto, Colombia.",
        "Recibes pares dirigidos con distancia vial y duración base de OSRM u ORS.",
        "Esos valores son la referencia real de calles; no los recalcules ni los cambies.",
        "Devuelve únicamente JSON válido con esta forma:",
        '{"adjustments":[{"fromStopId":"...","toStopId":"...",'
        '"trafficMultiplier":1.0}],"model":"..."}.',
        "El factor debe estar entre 0.5 y 3. No inventes IDs y omite pares sin evidencia.",
        "trafficObservations contiene mediciones TomTom con fecha, confianza y cierres.",
        "No alteres tramos medidos ni reabras cierres. Puedes estimar otros pares solo con",
        "evidencia de las observaciones; si no basta, omite el ajuste.",
        "No inventes tráfico en vivo.",
    )
)
_ASSIGNMENT_SYSTEM_PROMPT = " ".join(
    (
        "Eres un asesor de tráfico para asignar pedidos en Pasto, Colombia.",
        "Recibes pares anonimizados con distancia vial y duración base de OSRM/ORS.",
        "Esos valores son la referencia real de calles; no los recalcules ni los cambies.",
        "No sustituyas ni inventes esas distancias o duraciones; sugiere solo un factor",
        "de congestión prudente cuando las ubicaciones y los costos lo justifiquen.",
        "Devuelve únicamente JSON válido con esta forma:",
        '{"adjustments":[{"courierId":"...","orderId":"...",'
        '"trafficMultiplier":1.0}],"model":"..."}.',
        "El factor debe estar entre 0.5 y 3. No inventes IDs ni afirmes que es tráfico en vivo.",
    )
)
GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_TRAFFIC_MODEL = "openai/gpt-oss-20b"


class RouteTrafficAdvisor(Protocol):
    async def get_route_advice(self, request: RouteTrafficAiRequest) -> RouteTrafficAdvice: ...


class AssignmentTrafficAdvisor(Protocol):
    async def get_assignment_advice(
