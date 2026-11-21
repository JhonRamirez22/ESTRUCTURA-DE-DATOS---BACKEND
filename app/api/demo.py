"""Herramientas de demostración: solo existen con DEMO_MODE encendido."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.services.demo_data import refresh_demo_gps
from app.settings import get_settings

router = APIRouter(prefix="/demo", tags=["demo"])
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}


@router.get("/estado")
async def demo_status(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
) -> JSONResponse:
    return JSONResponse({"enabled": get_settings().demo_mode}, headers=_NO_STORE)


@router.post("/renovar-gps")
async def renew_demo_gps(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if not get_settings().demo_mode:
        return JSONResponse({"error": "El modo demo no está activo."}, status_code=404,
                            headers=_NO_STORE)
    try:
        renewed = await refresh_demo_gps(session, now=datetime.now(UTC).replace(tzinfo=None))
    except SQLAlchemyError:
        await session.rollback()
        return JSONResponse({"error": "No se pudo renovar el GPS demo."}, status_code=500,
                            headers=_NO_STORE)
    return JSONResponse({"renewed": renewed}, headers=_NO_STORE)
