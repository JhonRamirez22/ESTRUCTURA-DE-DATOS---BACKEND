"""Health check público que no filtra detalles del driver o la URL DB."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _configured_secret
from app.db.session import get_session
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()
_HEALTH_HEADERS = {"Cache-Control": "no-store, max-age=0"}


@router.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    if _configured_secret(get_settings()) is None:
        # Sin AUTH_SECRET válido nadie puede iniciar sesión: el servicio no está sano.
        return Response(
            content='{"status":"unavailable","checks":{"database":"unknown","auth":"missing"}}',
            status_code=503,
            media_type="application/json",
            headers=_HEALTH_HEADERS,
        )
    try:
        await session.execute(text("SELECT 1"))
    except Exception as error:
        # No se incluye el mensaje del driver porque puede revelar host o credenciales.
        logger.warning(
            "La comprobación de disponibilidad de PostgreSQL falló",
            extra={"error_type": type(error).__name__},
        )
        return Response(
            content='{"status":"unavailable","checks":{"database":"unavailable","auth":"ok"}}',
            status_code=503,
            media_type="application/json",
            headers=_HEALTH_HEADERS,
        )

    return Response(
        content='{"status":"ok","checks":{"database":"ok","auth":"ok"}}',
        media_type="application/json",
        headers=_HEALTH_HEADERS,
    )
