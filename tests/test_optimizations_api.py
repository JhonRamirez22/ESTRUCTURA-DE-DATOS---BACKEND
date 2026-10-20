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
    optimization_feed._EVENTS.clear()
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
