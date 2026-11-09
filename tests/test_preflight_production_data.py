from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.preflight_production_data import (
    DuplicateOrderIdGroupsError,
    assert_no_duplicate_order_ids,
    get_safe_database_error_code,
)


def test_safe_error_code_never_reads_secrets_from_message() -> None:
    error = RuntimeError("postgresql://user:secret@private-host")
    error.code = "08006"  # type: ignore[attr-defined]

    assert get_safe_database_error_code(error) == "08006"
    assert get_safe_database_error_code(
        RuntimeError("P1001 postgresql://private-host")
    ) is None


@pytest.mark.asyncio
async def test_preflight_accepts_unique_order_ids(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preflight.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE delivery_points (order_id TEXT NOT NULL)")
            )
            await connection.execute(
                text("INSERT INTO delivery_points (order_id) VALUES ('A'), ('B')")
            )
            await assert_no_duplicate_order_ids(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preflight_blocks_duplicate_order_ids_without_listing_them(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'duplicates.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE delivery_points (order_id TEXT NOT NULL)")
            )
            await connection.execute(
                text("INSERT INTO delivery_points (order_id) VALUES ('private-id'), ('private-id')")
            )
            with pytest.raises(DuplicateOrderIdGroupsError) as error:
                await assert_no_duplicate_order_ids(connection)
    finally:
        await engine.dispose()

    assert error.value.duplicate_groups == 1
    assert "private-id" not in str(error.value)
