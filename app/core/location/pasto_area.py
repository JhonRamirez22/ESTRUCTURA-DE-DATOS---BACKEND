"""Validación rectangular MVP de la zona urbana de servicio de Pasto."""

from __future__ import annotations

import re
import unicodedata

MIN_LATITUDE = 1.16
MAX_LATITUDE = 1.30
MIN_LONGITUDE = -77.36
MAX_LONGITUDE = -77.20
_ADDRESS_SEPARATOR = " # "
_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:av|avda|avenida)\.?(?=\s|#|$)", re.IGNORECASE), "Avenida"),
    (re.compile(r"\b(?:cra|carr|cr|carrera)\.?(?=\s|#|$)", re.IGNORECASE), "Carrera"),
    (re.compile(r"\b(?:cll|cl|cal|calle)\.?(?=\s|#|$)", re.IGNORECASE), "Calle"),
    (re.compile(r"\b(?:tr|transv|transversal)\.?(?=\s|#|$)", re.IGNORECASE), "Transversal"),
    (re.compile(r"\b(?:dg|diagonal)\.?(?=\s|#|$)", re.IGNORECASE), "Diagonal"),
    (re.compile(r"\b(?:no|num|nro|numero|número)\.?(?=\s|#|$)", re.IGNORECASE), "#"),
)
_FOREIGN_CITIES = frozenset(
    {
        "bogota",
        "medellin",
        "cali",
        "popayan",
        "ipiales",
        "tumaco",
        "neiva",
        "barranquilla",
        "cartagena",
        "armenia",
        "pereira",
        "manizales",
        "ibague",
        "quito",
        "guayaquil",
        "lima",
        "caracas",
        "santiago",
        "buenos aires",
        "paris",
        "madrid",
        "london",
        "new york",
        "miami",
        "ecuador",
        "peru",
        "venezuela",
        "argentina",
        "francia",
        "espana",
        "reino unido",
        "estados unidos",
        "usa",
    }
)


def is_within_pasto_service_area(latitude: float, longitude: float) -> bool:
    return MIN_LATITUDE <= latitude <= MAX_LATITUDE and MIN_LONGITUDE <= longitude <= MAX_LONGITUDE


def normalize_pasto_address(address: str) -> str:
    """Normaliza abreviaturas usuales sin geocodificar ni mover el pin elegido."""
    normalized = " ".join(address.strip().split())
    for pattern, replacement in _ABBREVIATIONS:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"\s*#\s*", _ADDRESS_SEPARATOR, normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    return " ".join(normalized.split()).strip()


def contains_foreign_city_reference(address: str) -> bool:
    normalized = _remove_accents(address).lower()
    return any(re.search(rf"\b{re.escape(city)}\b", normalized) for city in _FOREIGN_CITIES)


def _remove_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))
