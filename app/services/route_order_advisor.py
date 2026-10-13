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
                last_error.__cause__ = error
            else:
                if response.is_success:
                    return response
                retryable = response.status_code == 429 or response.status_code >= 500
                record_incident("groq", f"orden:http-{response.status_code}")
                last_error = RouteOrderAdvisorError(
                    f"La IA de orden respondió HTTP {response.status_code}."
                )
                if not retryable:
                    break
            if attempt == 0:
                await asyncio.sleep(0.4)
        assert last_error is not None
        raise last_error


def _parse_order(response: httpx.Response, size: int) -> list[int]:
    try:
        content = str(response.json()["choices"][0]["message"]["content"]).strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        raw_order = json.loads(content)["order"]
        order = [int(str(item).removeprefix("stop-")) for item in raw_order]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RouteOrderAdvisorError("La respuesta de la IA de orden no es válida.") from error
    if sorted(order) != list(range(size)):
        raise RouteOrderAdvisorError("La IA devolvió un orden que no cubre las paradas.")
    return order


def get_route_order_advisor() -> HttpRouteOrderAdvisor | None:
    settings = get_settings()
    if not (settings.ai_allow_route_location_sharing or settings.ai_allow_location_data_sharing):
        return None
    if settings.ai_traffic_provider != "groq":
        return None
    key = settings.customer_chat_api_key
    api_url = (settings.customer_chat_api_url or "").strip()
    model = (settings.customer_chat_model or "").strip()
    api_key = key.get_secret_value().strip() if key is not None else ""
    if not api_url or not api_key or not model:
        return None
    return HttpRouteOrderAdvisor(
        api_url=api_url,
        api_key=api_key,
        model=model,
        timeout_ms=settings.ai_traffic_timeout_ms,
    )
