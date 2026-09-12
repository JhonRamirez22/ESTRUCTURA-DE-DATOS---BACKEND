"""Resuelve texto sin exigir un pin previo; disponible solo para despacho."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _configured_secret, require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_geocoder import (
    AddressGeocoder,
    AddressGeocoderError,
    get_address_geocoder,
)
from app.services.address_resolver import resolve_address
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

router = APIRouter(prefix="/direcciones", tags=["direcciones"])
_LIMITER = CustomerChatRateLimiter(max_requests=30, window_seconds=60)


class ResolveAddressRequest(BaseModel):
