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
        self._timeout_seconds = timeout_ms / 1_000
        self._transport = transport

    async def search(self, address: str) -> list[AddressSuggestion]:
        query = normalize_pasto_address(address)
        if len(query) < 3:
            return []
        if contains_foreign_city_reference(query):
            return []
        if not re.search(r"\bpasto\b", query, flags=re.IGNORECASE):
            query = f"{query}, Pasto, Nariño, Colombia"

        params = {
            "key": self._api_key,
            "countrySet": "CO",
            "topLeft": f"{MAX_LATITUDE},{MIN_LONGITUDE}",
            "btmRight": f"{MIN_LATITUDE},{MAX_LONGITUDE}",
            "limit": str(MAX_ADDRESS_RESULTS),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{TOMTOM_GEOCODE_URL}/{quote(query, safe='')}.json",
                    params=params,
                )
        except httpx.TimeoutException as error:
            raise AddressGeocoderError(
                "La búsqueda de dirección excedió el tiempo límite.", 504
            ) from error
        except httpx.HTTPError as error:
            raise AddressGeocoderError(
                "No se pudo conectar con el servicio de direcciones."
            ) from error

        if response.status_code == 429:
            raise AddressGeocoderError(
                "TomTom alcanzó su límite de búsquedas; inténtalo luego.", 429
            )
        if not response.is_success:
            raise AddressGeocoderError(
                f"El servicio de direcciones respondió HTTP {response.status_code}."
            )

        try:
            payload: object = response.json()
        except ValueError as error:
            raise AddressGeocoderError(
                "El servicio de direcciones devolvió una respuesta inválida."
            ) from error
        return self._parse_suggestions(payload)

    @staticmethod
    def _parse_suggestions(payload: object) -> list[AddressSuggestion]:
        if not isinstance(payload, Mapping):
            raise AddressGeocoderError(
                "El servicio de direcciones devolvió una respuesta inválida."
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise AddressGeocoderError(
                "El servicio de direcciones devolvió una respuesta inválida."
            )

        suggestions: list[AddressSuggestion] = []
        seen: set[tuple[float, float]] = set()
        for item in results[:100]:
            if not isinstance(item, Mapping):
                continue
            position = item.get("position")
            address_data = item.get("address")
            if not isinstance(position, Mapping) or not isinstance(address_data, Mapping):
                continue
            if isinstance(position.get("lat"), bool) or isinstance(position.get("lon"), bool):
                continue
            try:
                lat = float(position["lat"])
                lng = float(position["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(lat) or not math.isfinite(lng):
                continue
            if not is_within_pasto_service_area(lat, lng):
                continue
            if str(address_data.get("countryCode", "")).upper() != "CO":
                continue
            municipality = str(address_data.get("municipality", "")).strip().casefold()
            if municipality != "pasto":
                continue
            label = address_data.get("freeformAddress")
            if not isinstance(label, str) or not label.strip():
                continue
            key = (round(lat, 6), round(lng, 6))
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                AddressSuggestion(
                    address=normalize_pasto_address(label), lat=lat, lng=lng,
                    precision={
                        "Point Address": "address", "Cross Street": "intersection",
                    }.get(str(item.get("type")), "approximate"),
                )
            )
        return suggestions


def get_address_geocoder() -> AddressGeocoder | None:
    settings = get_settings()
    if settings.tomtom_api_key is None:
        return None
    api_key = settings.tomtom_api_key.get_secret_value().strip()
    if not api_key:
        return None
    return AddressGeocoder(api_key=api_key)


def create_address_geocoder(settings: Settings) -> AddressGeocoder | None:
    if settings.tomtom_api_key is None:
        return None
    key = settings.tomtom_api_key.get_secret_value().strip()
    return AddressGeocoder(api_key=key) if key else None
