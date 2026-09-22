"""La IA (Groq) sugiere el orden de visita; el algoritmo lo verifica antes de usarlo.

Solo viaja una matriz de minutos entre paradas con alias genéricos: sin direcciones,
nombres ni coordenadas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.services.optimization_feed import record_incident
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
MAX_AI_ORDER_STOPS = 25
_SYSTEM_PROMPT = (
    "Eres un optimizador de rutas de reparto. Recibes paradas con alias stop-N y una matriz "
    "de minutos entre ellas (null = sin conexión). Devuelve solo JSON con la forma "
    '{"order":["stop-0", ...]}: todas las paradas exactamente una vez, empezando por stop-0, '
    "minimizando el tiempo total de recorrido. No agregues texto fuera del JSON."
)


class RouteOrderAdvisorError(RuntimeError):
    """La IA no respondió con un orden utilizable."""


class RouteOrderAdvisor(Protocol):
    async def suggest_order(
        self, durations_minutes: Sequence[Sequence[float | None]], current_order: Sequence[int]
    ) -> list[int]: ...


class HttpRouteOrderAdvisor:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        timeout_ms: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
