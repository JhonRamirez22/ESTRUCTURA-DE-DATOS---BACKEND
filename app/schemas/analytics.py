"""Contrato camelCase del resumen de eficiencia para el dashboard TypeScript."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FleetMetricsResponse(BaseModel):
    route_count: int = Field(serialization_alias="routeCount")
    completed_route_count: int = Field(serialization_alias="completedRouteCount")
    estimated_duration_minutes: float = Field(serialization_alias="estimatedDurationMinutes")
    estimated_distance_meters: float = Field(serialization_alias="estimatedDistanceMeters")
    time_saved_minutes: float = Field(serialization_alias="timeSavedMinutes")
    distance_saved_meters: float = Field(serialization_alias="distanceSavedMeters")
    routes_with_baseline: int = Field(serialization_alias="routesWithBaseline")


class AnalyticsResponse(BaseModel):
    metrics: FleetMetricsResponse
