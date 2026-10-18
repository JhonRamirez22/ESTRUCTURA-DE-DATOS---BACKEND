"""Resumen agregado de duración, distancia y ahorro de rutas."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.core.metrics.route_metrics import RouteMetricRecord, calculate_fleet_metrics
from app.db.session import get_session
from app.repositories.route_repository import RouteRepository
from app.schemas.analytics import AnalyticsResponse, FleetMetricsResponse

router = APIRouter(prefix="/analitica", tags=["analitica"])
_LOGGER = logging.getLogger(__name__)


@router.get("", response_model=AnalyticsResponse, response_model_by_alias=True)
async def get_analytics(
    request: Request,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    raw_courier_id = request.query_params.get("courierId")
    try:
        courier_id = UUID(raw_courier_id) if raw_courier_id else None
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="El identificador del repartidor no es válido.",
        ) from error

    try:
        rows = await RouteRepository(session).list_metric_snapshots(courier_id)
        records = [
            RouteMetricRecord(
                status=row.status.value,
                baseline_duration_minutes=row.baseline_duration_minutes,
                estimated_duration_minutes=row.estimated_duration_minutes,
                baseline_distance_meters=row.baseline_distance_meters,
                estimated_distance_meters=row.estimated_distance_meters,
            )
            for row in rows
        ]
        metrics = calculate_fleet_metrics(records)
        payload = AnalyticsResponse(metrics=FleetMetricsResponse(**asdict(metrics)))
        return JSONResponse(
            content=payload.model_dump(mode="json", by_alias=True),
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    except SQLAlchemyError as error:
        _LOGGER.error(
            "No se pudieron calcular las métricas de rutas",
            extra={"error_type": type(error).__name__},
        )
        return JSONResponse(
            content={"error": "No se pudieron calcular las métricas de rutas."}, status_code=500
        )
