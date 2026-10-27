from __future__ import annotations

from urllib.parse import unquote

import httpx
import pytest

from app.services.address_advisor import AddressAiError, AddressAiRequest, AddressAiResponse
from app.services.address_geocoder import AddressGeocoder, AddressGeocoderError
from app.services.address_resolver import resolve_address


def candidate(*, country: str = "CO", municipality: str = "Pasto", lat: float = 1.21,
              kind: str = "Point Address") -> dict[str, object]:
    return {
        "type": kind, "position": {"lat": lat, "lon": -77.28},
        "address": {"countryCode": country, "municipality": municipality,
                    "freeformAddress": "Carrera 25 # 4 Sur-65, Pasto"},
    }


class FakeAdvisor:
    def __init__(self, normalized: str = "Carrera 25 # 4 Sur-65", *, fail: bool = False) -> None:
        self.calls: list[AddressAiRequest] = []
        self.normalized, self.fail = normalized, fail

    async def normalize_address(self, request: AddressAiRequest) -> AddressAiResponse:
        self.calls.append(request)
        assert request.coordinates is None
        if self.fail:
            raise AddressAiError("timeout")
        return AddressAiResponse(
            normalizedAddress=self.normalized, isInPasto=True, city="Pasto",
            department="Nariño", country="Colombia", confidence=0.95,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["carrera 25 num 4 sur 65", "cra 25 # 4 sur 65"])
async def test_local_abbreviations_use_tomtom_without_ai(text: str) -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(unquote(request.url.path))
        assert request.url.params["countrySet"] == "CO"
        assert request.url.params["topLeft"] == "1.3,-77.36"
        return httpx.Response(200, json={"results": [candidate()]})

    advisor = FakeAdvisor()
    result = await resolve_address(text, AddressGeocoder(
