from __future__ import annotations

import json

import httpx
import pytest

from app.services.route_order_advisor import HttpRouteOrderAdvisor, RouteOrderAdvisorError

MATRIX = [[0.0, 5.0, 9.0], [5.0, 0.0, 4.0], [9.0, 4.0, 0.0]]


def _advisor(handler: httpx.MockTransport) -> HttpRouteOrderAdvisor:
    return HttpRouteOrderAdvisor(
        api_url="https://api.example/chat/completions",
        api_key="test-key",
        model="test-model",
        timeout_ms=1000,
        transport=handler,
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_advisor_sends_only_aliases_and_minutes_and_parses_the_order() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _reply('{"order":["stop-0","stop-2","stop-1"]}')

    order = await _advisor(httpx.MockTransport(handler)).suggest_order(MATRIX, [0, 1, 2])
    assert order == [0, 2, 1]
    user_content = seen["body"]["messages"][1]["content"]  # type: ignore[index]
    assert "stop-0" in user_content and "lat" not in user_content and "address" not in user_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ['{"order":["stop-0","stop-1"]}', '{"order":["stop-0","stop-1","stop-1"]}', "no es json"],
)
async def test_advisor_rejects_orders_that_do_not_cover_every_stop(content: str) -> None:
    transport = httpx.MockTransport(lambda _request: _reply(content))
    with pytest.raises(RouteOrderAdvisorError):
        await _advisor(transport).suggest_order(MATRIX, [0, 1, 2])


@pytest.mark.asyncio
async def test_advisor_reports_provider_errors_without_leaking_details() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429, text="secret detail"))
    with pytest.raises(RouteOrderAdvisorError) as error:
        await _advisor(transport).suggest_order(MATRIX, [0, 1, 2])
    assert "secret detail" not in str(error.value)


@pytest.mark.asyncio
async def test_advisor_retries_once_on_a_transient_error_and_records_the_incident() -> None:
    from app.services import optimization_feed

    optimization_feed.reset()
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503)
        return _reply('{"order":["stop-0","stop-1","stop-2"]}')

    order = await _advisor(httpx.MockTransport(handler)).suggest_order(MATRIX, [0, 1, 2])
    assert order == [0, 1, 2] and calls["count"] == 2
    assert optimization_feed.recent_incidents(5)[0]["kind"] == "orden:http-503"
    optimization_feed.reset()


@pytest.mark.asyncio
async def test_advisor_does_not_retry_a_client_error() -> None:
    from app.services import optimization_feed

    optimization_feed.reset()
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400)

    with pytest.raises(RouteOrderAdvisorError):
        await _advisor(httpx.MockTransport(handler)).suggest_order(MATRIX, [0, 1, 2])
    assert calls["count"] == 1
    optimization_feed.reset()
