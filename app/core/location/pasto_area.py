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
