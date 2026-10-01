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
            )
            if (
                len(proposed) <= 160
                and interpretation.is_in_pasto and interpretation.city.casefold() == "pasto"
                and interpretation.confidence >= 0.75 and numbers_match and directions_match
                and not contains_foreign_city_reference(proposed)
            ):
                candidates = await geocoder.search(_street_number_query(proposed))
                normalized, source = proposed, "ai+tomtom"
            else:
                warning = "La interpretación no es suficientemente fiable; completa la dirección."
        except AddressAiError:
            warning = "La IA no respondió; conserva el texto y completa la dirección o usa el mapa."
    if not candidates and warning is None:
        warning = "No hay coincidencias verificadas; completa la dirección o selecciona en el mapa."
    full_number = re.search(r"#\s*\d+\w*\s*(?:sur|norte)?\s*[- ]\s*\d+", normalized, re.I)
    if not full_number:
        candidates = [
            replace(item, precision="approximate") if item.precision == "address" else item
            for item in candidates
        ]
    if candidates and any(item.precision != "address" for item in candidates):
        warning = "Ubicación aproximada o intersección: confirma el punto antes de guardar."
    return {
        "candidates": [item.as_dict() for item in candidates], "source": source,
        "normalizedAddress": normalized, "warning": warning,
    }


def _street_number_query(address: str) -> str:
    # TomTom entiende mejor la numeración colombiana cuando se explicita el guion de placa.
    return re.sub(
        r"(#\s*\d+[a-z]?\s*(?:(?:sur|norte)\s*)?)\s+(\d+[a-z]?)(?=\s*(?:,|$))",
        r"\1-\2", address, flags=re.I,
    )
