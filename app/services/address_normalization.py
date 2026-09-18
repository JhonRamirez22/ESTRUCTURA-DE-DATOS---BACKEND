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
