"""Normalización determinista con asesoría externa opcional y no autoritativa."""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from app.core.location.pasto_area import (
    contains_foreign_city_reference,
    normalize_pasto_address,
)
from app.services.address_advisor import (
    AddressAiAdvisor,
    AddressAiRequest,
    AddressAiResponse,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AddressNormalizationResult:
    address: str
    source: str
    confidence: float | None = None
    warning: str | None = None

    def as_dict(self) -> dict[str, str | float | None]:
        return {
            "source": self.source,
            "confidence": self.confidence,
            "warning": self.warning,
        }


async def normalize_address(
    address: str,
    latitude: float,
    longitude: float,
    advisor: AddressAiAdvisor | None,
) -> AddressNormalizationResult:
    local_address = normalize_pasto_address(address)
    if advisor is None:
        return AddressNormalizationResult(
            address=local_address,
            source="deterministic",
            warning="IA externa desactivada o no configurada; se aplicó normalización local.",
        )

    request = AddressAiRequest(
        raw_address=local_address,
        coordinates={"lat": latitude, "lng": longitude},
    )
    try:
        response = await advisor.normalize_address(request)
    except Exception as error:
        # La dirección y las coordenadas no deben terminar en logs de proveedor.
        _LOGGER.warning(
            "No se pudo normalizar la dirección con IA",
            extra={"error_type": type(error).__name__},
        )
        return AddressNormalizationResult(
            address=local_address,
            source="deterministic",
            warning="La IA de direcciones no respondió; se conservó la normalización local.",
        )

    normalized = normalize_pasto_address(response.normalized_address)
    if (
        not _confirms_pasto(response)
        or response.confidence < 0.75
        or contains_foreign_city_reference(normalized)
    ):
        return AddressNormalizationResult(
            address=local_address,
            source="deterministic",
            confidence=response.confidence,
            warning=(
                "La IA no confirmó una dirección de Pasto con suficiente confianza; "
                "se conservó la normalización local."
            ),
        )

    return AddressNormalizationResult(
        address=normalized,
        source="external-ai",
        confidence=response.confidence,
        warning=response.reason,
    )


def _confirms_pasto(response: AddressAiResponse) -> bool:
    is_in_pasto = response.is_in_pasto
    city = _normalize_location_token(response.city)
    department = _normalize_location_token(response.department)
    country = _normalize_location_token(response.country)
    return is_in_pasto and city == "pasto" and department == "narino" and country == "colombia"


def _normalize_location_token(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return without_accents.strip().lower()
