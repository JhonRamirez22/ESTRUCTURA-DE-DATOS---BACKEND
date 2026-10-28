from __future__ import annotations

import pytest
from sqlalchemy import Text, UniqueConstraint, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import (
    Courier,
    CourierCredential,
    CourierStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Notification,
    NotificationKind,
    Route,
    RouteGeometryProvider,
    RouteStatus,
)
from app.settings import Settings


def test_models_match_prisma_text_columns_without_redundant_unique_indexes() -> None:
    for column in (
        Courier.__table__.c.name,
        Courier.__table__.c.phone,
        CourierCredential.__table__.c.access_code_hash,
        DeliveryPoint.__table__.c.address,
        DeliveryPoint.__table__.c.order_id,
        DeliveryPoint.__table__.c.tracking_token,
    ):
        assert isinstance(column.type, Text)

    tracking_token_uniques = [
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in DeliveryPoint.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == ("tracking_token",)
    ]
    assert tracking_token_uniques == [
        ("delivery_points_tracking_token_key", ("tracking_token",))
    ]


def test_settings_normalizes_prisma_database_url_for_asyncpg() -> None:
    settings = Settings(
        database_url="postgresql://user:pass@localhost:5433/rutas?schema=public"
    )
    database_url = settings.sqlalchemy_database_url
