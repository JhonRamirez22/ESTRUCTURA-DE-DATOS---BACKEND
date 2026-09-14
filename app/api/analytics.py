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
