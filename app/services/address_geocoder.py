"""Geocodificación de direcciones restringida al área urbana de Pasto."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.location.pasto_area import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    MIN_LATITUDE,
    MIN_LONGITUDE,
    contains_foreign_city_reference,
    is_within_pasto_service_area,
    normalize_pasto_address,
)
from app.settings import Settings, get_settings

TOMTOM_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode"
ADDRESS_GEOCODER_TIMEOUT_MS = 5_000
MAX_ADDRESS_RESULTS = 5


@dataclass(frozen=True, slots=True)
class AddressSuggestion:
    address: str
    lat: float
    lng: float
    precision: str = "approximate"

    def as_dict(self) -> dict[str, str | float]:
        return {
            "address": self.address, "lat": self.lat, "lng": self.lng,
            "precision": self.precision,
        }


class AddressGeocoderError(RuntimeError):
    """Error sanitizado: nunca incluye la clave ni la dirección consultada."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AddressGeocoder:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_ms: int = ADDRESS_GEOCODER_TIMEOUT_MS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("TOMTOM_API_KEY debe configurarse para buscar direcciones.")
        if timeout_ms <= 0:
            raise ValueError("El timeout de geocodificación debe ser positivo.")
        self._api_key = api_key.strip()
