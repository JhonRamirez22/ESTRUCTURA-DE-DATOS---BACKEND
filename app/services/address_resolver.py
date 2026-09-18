"""La IA interpreta texto; solo la geocodificación verificada aporta coordenadas."""

from __future__ import annotations

import re
from dataclasses import replace

from app.core.location.pasto_area import contains_foreign_city_reference, normalize_pasto_address
from app.services.address_advisor import AddressAiAdvisor, AddressAiError, AddressAiRequest
from app.services.address_geocoder import AddressGeocoder


async def resolve_address(
    text: str, geocoder: AddressGeocoder, advisor: AddressAiAdvisor | None,
) -> dict[str, object]:
    normalized = normalize_pasto_address(text)
    if contains_foreign_city_reference(normalized):
        return {
            "candidates": [], "source": "local", "normalizedAddress": normalized,
            "warning": "Solo se aceptan direcciones dentro de Pasto, Nariño, Colombia.",
        }
    candidates = await geocoder.search(_street_number_query(normalized))
    source = "tomtom"
    warning: str | None = None
    if not candidates and advisor is not None:
        try:
            interpretation = await advisor.normalize_address(
                AddressAiRequest(raw_address=normalized)
            )
            proposed = normalize_pasto_address(interpretation.normalized_address)
            # La interpretación no puede inventar números ni quitar el Sur de una dirección.
            numbers_match = re.findall(r"\d+", proposed) == re.findall(r"\d+", normalized)
            directions_match = all(
                not re.search(rf"\b{direction}\b", normalized, re.I)
                or re.search(rf"\b{direction}\b", proposed, re.I)
                for direction in ("sur", "norte", "este", "oeste")
