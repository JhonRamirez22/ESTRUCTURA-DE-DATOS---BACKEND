"""Solicitudes de envío de clientes: entran en espera y despacho las acepta o rechaza.

La creación es pública, así que se protege por capas: origen obligatorio, contrato
estricto sin datos personales, zona de servicio de Pasto, límites compartidos y un tope
de solicitudes en espera. Una solicitud nunca entra a asignación: solo los pedidos en
PENDING son elegibles, y aceptar es la única vía para llegar a ese estado.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import _configured_secret, _validate_origin, require_dispatcher_session
from app.core.auth import SessionClaims
from app.core.location.pasto_area import (
    contains_foreign_city_reference,
    is_within_pasto_service_area,
    normalize_pasto_address,
)
from app.core.location.proximity import haversine_m
from app.core.structures.lru_cache import LruCache
from app.db.session import get_session
from app.models import DeliveryEventType, DeliveryPoint, DeliveryPointStatus
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.schemas.orders import OrderRequestCreate
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_geocoder import (
    AddressGeocoder,
    AddressGeocoderError,
    get_address_geocoder,
)
from app.services.address_resolver import resolve_address
from app.services.client_identity import client_key
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.delivery_events import (
    CUSTOMER_ACTOR,
    DeliveryEventRecorder,
    EventActor,
)
from app.settings import get_settings

router = APIRouter(prefix="/solicitudes", tags=["solicitudes"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}
MAX_REQUEST_BODY_BYTES = 2_048
# Tope de la bandeja: por encima, se rechazan nuevas solicitudes hasta que despacho resuelva.
MAX_WAITING_REQUESTS = 25
# Por cliente (cabecera de reenvío) y para todo el servicio, compartidos entre réplicas.
_CLIENT_LIMITER = CustomerChatRateLimiter(max_requests=3, window_seconds=600)
_SERVICE_LIMITER = CustomerChatRateLimiter(max_requests=20, window_seconds=600)
# Búsquedas de dirección: más holgadas que el envío, pero acotan el gasto en TomTom y Groq.
_LOOKUP_CLIENT_LIMITER = CustomerChatRateLimiter(max_requests=15, window_seconds=600)
_LOOKUP_SERVICE_LIMITER = CustomerChatRateLimiter(max_requests=120, window_seconds=600)
# Respuestas recientes por texto normalizado: repetir una búsqueda no vuelve a los proveedores.
_LOOKUP_CACHE: LruCache[str, dict[str, object]] = LruCache(256)

