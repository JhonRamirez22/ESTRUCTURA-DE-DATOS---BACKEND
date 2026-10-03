"""Registro en memoria de las optimizaciones recientes para el panel de despacho.

Vive por proceso: al reiniciar se pierde el historial. Solo guarda alias cortos de ruta
y contadores; nunca direcciones, coordenadas ni datos personales.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.core.structures.doubly_linked_list import DoublyLinkedList
from app.core.structures.queue import Queue

EventKind = Literal["propuesta", "aplicada"]
MAX_EVENTS = 200
MAX_INCIDENTS = 50
# Lista doble: los eventos nuevos entran al frente y los más viejos salen por el final en O(1).
_EVENTS: DoublyLinkedList[dict[str, object]] = DoublyLinkedList()
# Cola FIFO acotada de incidencias de proveedores externos (Groq, TomTom).
_INCIDENTS: Queue[dict[str, object]] = Queue()
_COUNTERS: dict[str, int] = {
    "proposals": 0, "cacheHits": 0, "applied": 0, "trafficSegments": 0, "incidents": 0,
}
_SAVED_MINUTES: list[float] = []
_LAST_SUMMARY_AT: list[float] = []
SUMMARY_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class FeedStats:
    proposals: int
    cache_hits: int
    applied: int
    traffic_segments: int
    saved_minutes: float


def route_alias(route_id: UUID) -> str:
    return "R-" + hashlib.sha256(str(route_id).encode("utf-8")).hexdigest()[:4].upper()


def record_proposal(route_id: UUID, body: dict[str, object], *, cached: bool) -> None:
    if cached:
        _COUNTERS["cacheHits"] += 1
        return
    _COUNTERS["proposals"] += 1
    segments = _traffic_segments(body)
    _COUNTERS["trafficSegments"] += segments
    improvement = body.get("improvementMinutes")
    improvement_minutes = float(improvement) if isinstance(improvement, (int, float)) else None
    if improvement_minutes is not None and improvement_minutes > 0 and body.get("changed") is True:
        _SAVED_MINUTES.append(improvement_minutes)
    _push_event(
        {
            "at": _now(),
            "kind": "propuesta",
            "routeAlias": route_alias(route_id),
            "changed": body.get("changed") is True,
            "improvementMinutes": improvement_minutes,
            "trafficSegments": segments,
            "aiApplied": _ai_applied(body),
            "aiOrderAccepted": _ai_order(body, "accepted") is True,
            "aiOrderSavingMinutes": _ai_order(body, "savingMinutes") or 0.0,
        }
    )


def record_applied(route_id: UUID, body: dict[str, object]) -> None:
    _COUNTERS["applied"] += 1
    _push_event(
        {
            "at": _now(),
            "kind": "aplicada",
            "routeAlias": route_alias(route_id),
            "changed": True,
            "improvementMinutes": None,
            "trafficSegments": _traffic_segments(body),
            "aiApplied": _ai_applied(body),
            "aiOrderAccepted": False,
            "aiOrderSavingMinutes": 0.0,
        }
    )


def recent_events(limit: int) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for event in _EVENTS:
        if len(events) >= limit:
            break
        events.append(event)
    return events


def record_incident(provider: str, kind: str) -> None:
    """Guarda que un proveedor falló o se degradó; descarta lo más antiguo al llenarse."""
    _COUNTERS["incidents"] = _COUNTERS.get("incidents", 0) + 1
    _INCIDENTS.enqueue({"at": _now(), "provider": provider, "kind": kind})
    while len(_INCIDENTS) > MAX_INCIDENTS:
        _INCIDENTS.dequeue()


def recent_incidents(limit: int) -> list[dict[str, object]]:
    snapshot: list[dict[str, object]] = []
    for _ in range(len(_INCIDENTS)):
        item = _INCIDENTS.dequeue()
        if item is not None:
            snapshot.append(item)
            _INCIDENTS.enqueue(item)
    return list(reversed(snapshot))[:limit]


