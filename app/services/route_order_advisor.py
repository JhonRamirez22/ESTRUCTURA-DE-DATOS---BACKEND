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
        self._api_url = api_url
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_ms / 1000
        self._transport = transport

    async def suggest_order(
        self, durations_minutes: Sequence[Sequence[float | None]], current_order: Sequence[int]
    ) -> list[int]:
        size = len(durations_minutes)
        payload = {
            "stops": [f"stop-{index}" for index in range(size)],
            "durationsMinutes": [
                [
                    round(value, 1) if value is not None and math.isfinite(value) else None
                    for value in row
                ]
                for row in durations_minutes
            ],
            "currentOrder": [f"stop-{index}" for index in current_order],
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            "temperature": 0.1,
            "max_tokens": 600,
            "stream": False,
        }
        response = await self._post_with_one_retry(body)
        return _parse_order(response, size)


    async def _post_with_one_retry(self, body: dict[str, object]) -> httpx.Response:
        last_error: RouteOrderAdvisorError | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds, transport=self._transport
                ) as client:
                    response = await client.post(
                        self._api_url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=body,
                    )
            except httpx.HTTPError as error:
                record_incident("groq", "orden:red")
                last_error = RouteOrderAdvisorError("No se pudo consultar a la IA de orden.")
