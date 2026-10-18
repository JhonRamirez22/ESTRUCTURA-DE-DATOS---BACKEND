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
        self, request: AssignmentTrafficRequest
    ) -> AssignmentTrafficAdvice: ...


class RouteTrafficAiError(RuntimeError):
    """Error seguro que no conserva ni registra payloads con coordenadas."""


class HttpRouteTrafficAdvisor:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str | None,
        model: str | None,
        timeout_ms: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("AI_TRAFFIC_API_URL no puede estar vacío.")
        if timeout_ms <= 0:
            raise ValueError("AI_TRAFFIC_TIMEOUT_MS debe ser positivo.")
        self._api_url = api_url
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_ms / 1_000
        self._protocol = "openai-chat" if "/chat/completions" in api_url else "json"
        self._transport = transport
        if self._protocol == "openai-chat" and not model:
            raise ValueError("AI_TRAFFIC_MODEL es obligatorio para Chat Completions.")

    async def get_route_advice(self, request: RouteTrafficAiRequest) -> RouteTrafficAdvice:
        try:
            payload = await self._post(request.as_payload())
            if not isinstance(payload, Mapping):
                raise ValueError("La respuesta debe ser un objeto JSON.")
            return parse_route_traffic_response(payload)
        except (ValueError, TypeError, KeyError) as error:
            raise RouteTrafficAiError(
                "La respuesta de IA no cumple el contrato de tráfico."
            ) from error

    async def get_assignment_advice(
        self,
        request: AssignmentTrafficRequest,
    ) -> AssignmentTrafficAdvice:
        try:
            payload = await self._post(request.as_payload())
            if not isinstance(payload, Mapping):
                raise ValueError("La respuesta debe ser un objeto JSON.")
            return parse_assignment_traffic_response(payload)
        except (ValueError, TypeError, KeyError) as error:
            raise RouteTrafficAiError(
                "La respuesta de IA no cumple el contrato de asignación."
            ) from error

    def _request_body(self, request: RouteTrafficAiRequest) -> dict[str, object]:
        return self._request_body_from(request.as_payload())

    def _request_body_from(self, payload: dict[str, object]) -> dict[str, object]:
        if self._protocol == "openai-chat":
            body: dict[str, object] = {
                "model": self._model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            _ASSIGNMENT_SYSTEM_PROMPT
                            if payload.get("task") == "assignment_traffic"
                            else _SYSTEM_PROMPT
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                "temperature": 0.1,
                "top_p": 1,
                "max_tokens": 1_024,
                "stream": False,
            }
            if "api.groq.com" in self._api_url:
                body["response_format"] = {"type": "json_object"}
            return body
        return {
            "contractVersion": payload["contractVersion"],
            "task": payload["task"],
            "model": self._model,
            "input": payload,
        }

    async def _post(self, payload: dict[str, object]) -> object:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._api_url,
                    headers=headers,
                    json=self._request_body_from(payload),
                )
        except httpx.TimeoutException as error:
            raise RouteTrafficAiError("La IA de tráfico excedió el tiempo límite.") from error
        except httpx.HTTPError as error:
            raise RouteTrafficAiError("No se pudo conectar con la IA de tráfico.") from error

        if response.status_code == 429:
            raise RouteTrafficAiError("La IA de tráfico alcanzó su límite de solicitudes.")
        if not response.is_success:
            raise RouteTrafficAiError(f"La IA de tráfico respondió HTTP {response.status_code}.")
        try:
            result: object = response.json()
            return self._read_chat_content(result) if self._protocol == "openai-chat" else result
        except (ValueError, TypeError) as error:
            raise RouteTrafficAiError("La IA de tráfico devolvió JSON inválido.") from error

    @staticmethod
    def _read_chat_content(payload: object) -> object:
        if not isinstance(payload, dict):
            raise ValueError("Chat Completions devolvió una respuesta inválida.")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Chat Completions no devolvió choices.")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Chat Completions no devolvió contenido.")
        content = message["content"].strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.removeprefix("```").removesuffix("```").strip()
            if content[:4].lower() == "json":
                content = content[4:].strip()
        return json.loads(content)


def get_route_traffic_advisor() -> HttpRouteTrafficAdvisor | None:
    settings = get_settings()
    if not (settings.ai_allow_route_location_sharing or settings.ai_allow_location_data_sharing):
        return None
    api_url: str
    api_key: str | None
    model: str | None
    if settings.ai_traffic_provider == "groq":
        api_url = (settings.customer_chat_api_url or GROQ_CHAT_COMPLETIONS_URL).strip()
        api_key = (
            settings.customer_chat_api_key.get_secret_value().strip()
            if settings.customer_chat_api_key is not None
            else None
        )
        model = (settings.customer_chat_model or DEFAULT_GROQ_TRAFFIC_MODEL).strip()
    else:
        api_url = (settings.ai_traffic_api_url or "").strip()
        api_key = (
            settings.ai_traffic_api_key.get_secret_value().strip()
            if settings.ai_traffic_api_key is not None
            else None
        )
        model = (settings.ai_traffic_model or "").strip() or None
    if not api_url or not api_key or not model:
        return None
    try:
        return HttpRouteTrafficAdvisor(
            api_url=api_url,
            api_key=api_key or None,
            model=model,
            timeout_ms=settings.ai_traffic_timeout_ms,
        )
    except ValueError as error:
        _LOGGER.warning(
            "La configuración de IA de tráfico no es válida; se usará el algoritmo determinista",
            extra={"error_type": type(error).__name__},
        )
        return None
