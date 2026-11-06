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
    assert database_url.drivername == "postgresql+asyncpg"
    assert database_url.password == "pass"
    assert database_url.database == "rutas"
    assert "schema" not in database_url.query


def test_settings_redacts_database_url_password_from_repr() -> None:
    password = "do-not-leak-this-password"
    settings = Settings(database_url=f"postgresql://user:{password}@localhost:5433/rutas")

    assert password not in repr(settings)
    assert "**********" in str(settings.database_url)


@pytest.mark.asyncio
async def test_sqlalchemy_schema_persists_existing_entities_and_constraints() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            courier = Courier(
                name="Repartidor de prueba",
                phone="3000000000",
                status=CourierStatus.AVAILABLE,
            )
            route = Route(
                courier=courier,
                status=RouteStatus.PLANNED,
                estimated_duration_minutes=12,
                estimated_distance_meters=2_400,
                geometry={"type": "LineString", "coordinates": [[-77.28, 1.21], [-77.27, 1.22]]},
                geometry_provider=RouteGeometryProvider.OSRM,
            )
            point = DeliveryPoint(
                address="Carrera 25 # 4 Sur-65",
                lat=1.2136,
                lng=-77.2811,
                status=DeliveryPointStatus.PENDING,
                order_id="TEST-ORDER-1",
                route=route,
                sequence_index=0,
                time_window={"start": "09:00", "end": "11:00"},
            )
            courier.credential = CourierCredential(access_code_hash="test-hash")
            courier.location_events.append(LocationEvent(lat=1.2136, lng=-77.2811))
            notification = Notification(
                kind=NotificationKind.ROUTE_CREATED,
