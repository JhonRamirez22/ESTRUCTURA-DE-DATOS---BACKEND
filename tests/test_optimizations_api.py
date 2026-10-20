from __future__ import annotations

import time
from uuid import UUID

from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.services import optimization_feed
from app.services.optimization_feed import recent_events, record_proposal, route_alias

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


def _dispatcher_cookie() -> str:
    token = create_session_token(
        "dispatcher", int(time.time()) + SESSION_MAX_AGE_SECONDS, _SECRET,
    )
    return f"{session_cookie_name('dispatcher')}={token}"


def test_route_alias_hides_the_real_route_id() -> None:
    route_id = UUID("11111111-2222-4333-8444-555555555555")
    alias = route_alias(route_id)
    assert alias.startswith("R-") and len(alias) == 6
    assert str(route_id) not in alias


def test_proposals_are_recorded_without_addresses_and_cache_hits_only_count() -> None:
    optimization_feed.reset()
    before = optimization_feed.stats().cache_hits
    route_id = UUID("11111111-2222-4333-8444-555555555555")
    body = {
        "changed": True,
        "improvementMinutes": 2.5,
        "liveTraffic": {"usedMeasuredSegments": 4},
        "trafficAi": {"applied": True},
        "orderedDeliveryPointIds": ["secret-point-id"],
    }
    record_proposal(route_id, body, cached=False)
    record_proposal(route_id, body, cached=True)
    events = recent_events(5)
    assert events[0]["kind"] == "propuesta"
    assert events[0]["routeAlias"] == route_alias(route_id)
    assert "secret-point-id" not in str(events)
    assert optimization_feed.stats().cache_hits == before + 1


def test_history_keeps_the_newest_events_and_incidents_are_a_bounded_fifo() -> None:
    optimization_feed.reset()
    route_id = UUID("11111111-2222-4333-8444-555555555555")
    for _ in range(optimization_feed.MAX_EVENTS + 5):
        record_proposal(route_id, {"changed": False}, cached=False)
    assert len(optimization_feed._EVENTS) == optimization_feed.MAX_EVENTS
    assert len(recent_events(1000)) == optimization_feed.MAX_EVENTS

    for index in range(optimization_feed.MAX_INCIDENTS + 3):
        optimization_feed.record_incident("groq", f"fallo-{index}")
    incidents = optimization_feed.recent_incidents(100)
    assert len(incidents) == optimization_feed.MAX_INCIDENTS
    assert incidents[0]["kind"] == f"fallo-{optimization_feed.MAX_INCIDENTS + 2}"
    assert incidents[-1]["kind"] == "fallo-3"
    optimization_feed.reset()
