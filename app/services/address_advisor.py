"""Adaptador opcional de IA para normalizar direcciones sin cambiar el pin."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.settings import Settings, get_settings

ADDRESS_AI_CONTRACT_VERSION = "rutas-pasto.address-normalization.v1"
_SYSTEM_PROMPT = " ".join(
    (
        "Eres un normalizador de direcciones de Pasto, Nariño, Colombia.",
        "Corrige abreviaturas y errores de escritura sin inventar coordenadas.",
        "Si hay coordenadas, deben estar dentro de Pasto; "
        "sin coordenadas solo interpreta el texto.",
        "Nunca inventes numeración ni elimines orientación sur/norte; pide aclaración si falta.",
        "Devuelve únicamente JSON válido, sin markdown, con esta forma:",
        '{"normalizedAddress":"...","isInPasto":true,"city":"Pasto",'
        '"department":"Nariño","country":"Colombia","confidence":0.95,"reason":"..."}.',
        "Los campos city, department y country son obligatorios y deben ser exactamente "
        "Pasto, Nariño y Colombia.",
        "No geocodifiques, no cambies las coordenadas ni conviertas direcciones de otra "
        "ciudad o país en una de Pasto.",
    )
)
_LOGGER = logging.getLogger(__name__)


class AddressAiRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_version: str = Field(
        default=ADDRESS_AI_CONTRACT_VERSION,
        serialization_alias="contractVersion",
    )
    task: str = "address_normalization"
    city: str = "Pasto, Nariño, Colombia"
    raw_address: str = Field(serialization_alias="rawAddress")
    coordinates: dict[str, float] | None = None


class AddressAiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    normalized_address: str = Field(min_length=1, validation_alias="normalizedAddress")
    is_in_pasto: bool = Field(validation_alias="isInPasto")
    confidence: float = Field(ge=0, le=1)
    city: str = Field(min_length=1)
    department: str = Field(min_length=1)
    country: str = Field(min_length=1)
    reason: str | None = None


class AddressAiAdvisor(Protocol):
    async def normalize_address(self, request: AddressAiRequest) -> AddressAiResponse: ...


class AddressAiError(RuntimeError):
    """Fallo del proveedor sin incluir el cuerpo, que podría contener direcciones."""


class HttpAddressAiAdvisor:
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
        self._timeout_seconds = timeout_ms / 1000
        self._protocol = "openai-chat" if "/chat/completions" in api_url else "json"
        self._transport = transport
        if self._protocol == "openai-chat" and not model:
            raise ValueError("AI_TRAFFIC_MODEL es obligatorio para Chat Completions.")

    async def normalize_address(self, request: AddressAiRequest) -> AddressAiResponse:
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
                    json=self._request_body(request),
                )
        except httpx.TimeoutException as error:
            raise AddressAiError("La IA de direcciones excedió el tiempo límite.") from error
        except httpx.HTTPError as error:
            raise AddressAiError("No se pudo conectar con la IA de direcciones.") from error

        if response.status_code == 429:
            raise AddressAiError("La IA de direcciones alcanzó su límite de solicitudes.")
        if not response.is_success:
            raise AddressAiError(f"La IA de direcciones respondió HTTP {response.status_code}.")

        try:
            payload: object = response.json()
            if self._protocol == "openai-chat":
                payload = self._read_chat_content(payload)
            if not isinstance(payload, Mapping):
                raise ValueError("La respuesta debe ser un objeto JSON.")
            return AddressAiResponse.model_validate(payload)
        except (ValueError, TypeError, ValidationError) as error:
            raise AddressAiError(
                "La respuesta de IA no cumple el contrato de dirección."
            ) from error

    def _request_body(self, request: AddressAiRequest) -> dict[str, object]:
        if self._protocol == "openai-chat":
            return {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            request.model_dump(by_alias=True), ensure_ascii=False
                        ),
                    },
                ],
                "temperature": 0.1,
                "top_p": 1,
                "max_tokens": 1024,
                "stream": False,
            }
        return {
            "contractVersion": request.contract_version,
            "task": request.task,
            "model": self._model,
            "input": request.model_dump(by_alias=True),
        }

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


def get_address_ai_advisor() -> AddressAiAdvisor | None:
    try:
        return create_address_ai_advisor(get_settings())
    except ValueError as error:
        _LOGGER.warning(
            "La configuración de IA de direcciones no es válida; se usará normalización local",
            extra={"error_type": type(error).__name__},
        )
        return None


def create_address_ai_advisor(settings: Settings) -> AddressAiAdvisor | None:
    """El opt-in evita enviar direcciones/ubicaciones privadas por defecto."""
    if not settings.ai_allow_location_data_sharing:
        return None
    use_groq = settings.ai_traffic_provider == "groq"
    api_url = (
        (settings.customer_chat_api_url if use_groq else settings.ai_traffic_api_url) or ""
    ).strip()
    if not api_url:
        return None
    credential = settings.customer_chat_api_key if use_groq else settings.ai_traffic_api_key
    key = credential.get_secret_value().strip() if credential is not None else None
    model = (
        (settings.customer_chat_model if use_groq else settings.ai_traffic_model) or ""
    ).strip()
    return HttpAddressAiAdvisor(
        api_url=api_url,
        api_key=key or None,
        model=model,
        timeout_ms=settings.ai_traffic_timeout_ms,
    )
