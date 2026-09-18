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
