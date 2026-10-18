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
        api_key="test-key", transport=httpx.MockTransport(respond),
    ), advisor)
    assert result["source"] == "tomtom"
    assert result["candidates"][0]["precision"] == "address"
    assert len(seen) == 1 and not advisor.calls
    assert "Sur-65" in seen[0] or "sur-65" in seen[0]


@pytest.mark.asyncio
async def test_incomplete_number_is_never_silently_exact() -> None:
    geocoder = AddressGeocoder(api_key="test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [candidate()]}),
    ))
    result = await resolve_address("carrera 25 num 3", geocoder, None)
    assert result["candidates"][0]["precision"] == "approximate"
    assert "confirma" in result["warning"]


@pytest.mark.asyncio
async def test_ai_interprets_after_miss_but_only_tomtom_supplies_coordinates() -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": [] if calls == 1 else [candidate()]})

    advisor = FakeAdvisor()
    result = await resolve_address("carera 25 num 4 sur 65", AddressGeocoder(
        api_key="test", transport=httpx.MockTransport(respond),
    ), advisor)
    assert result["source"] == "ai+tomtom"
    assert result["candidates"][0]["lat"] == 1.21
    assert len(advisor.calls) == 1 and calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("proposed", ["Carrera 99 # 4 Sur-65", "Carrera 25 # 4-65"])
async def test_ai_cannot_invent_numbers_or_drop_sur(proposed: str) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    result = await resolve_address("carera 25 num 4 sur 65", AddressGeocoder(
        api_key="test", transport=httpx.MockTransport(respond),
    ), FakeAdvisor(proposed))
    assert result["candidates"] == [] and len(calls) == 1


@pytest.mark.asyncio
async def test_foreign_provider_results_are_discarded_and_foreign_input_not_sent() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": [
            candidate(country="EC"), candidate(municipality="Quito"), candidate(lat=4.5),
        ]})

    geocoder = AddressGeocoder(api_key="test", transport=httpx.MockTransport(respond))
    assert (await resolve_address("Carrera 25 num 3", geocoder, None))["candidates"] == []
    assert (await resolve_address("Carrera 25 Quito Ecuador", geocoder, None))["candidates"] == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ai_failure_keeps_manual_alternative_without_fabricating_a_pin() -> None:
    geocoder = AddressGeocoder(api_key="test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": []}),
    ))
    result = await resolve_address("carera 25 num 3", geocoder, FakeAdvisor(fail=True))
    assert result["candidates"] == []
    assert "conserva el texto" in result["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_provider_http_error_is_explicit_and_does_not_leak_key(status: int) -> None:
    geocoder = AddressGeocoder(api_key="test-private-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text="provider detail"),
    ))
    with pytest.raises(AddressGeocoderError) as caught:
        await resolve_address("Carrera 25 # 3", geocoder, None)
    assert caught.value.status_code == (429 if status == 429 else 502)
    assert "test-private-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_provider_timeout_is_distinct() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    geocoder = AddressGeocoder(api_key="test", transport=httpx.MockTransport(timeout))
    with pytest.raises(AddressGeocoderError) as caught:
        await resolve_address("Carrera 25 # 3", geocoder, None)
    assert caught.value.status_code == 504
