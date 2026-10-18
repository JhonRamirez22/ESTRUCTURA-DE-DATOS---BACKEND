from __future__ import annotations

from app.core.location.pasto_area import (
    contains_foreign_city_reference,
    is_within_pasto_service_area,
    normalize_pasto_address,
)


def test_service_area_includes_its_boundaries_and_rejects_other_cities() -> None:
    assert is_within_pasto_service_area(1.16, -77.36)
    assert is_within_pasto_service_area(1.30, -77.20)
    assert not is_within_pasto_service_area(1.31, -77.28)
    assert not is_within_pasto_service_area(1.21, -77.19)


def test_address_normalization_expands_common_abbreviations_without_geocoding() -> None:
    assert normalize_pasto_address("  cra. 25   num 4 sur - 65 ") == "Carrera 25 # 4 sur-65"
    assert normalize_pasto_address("Cll 18 # 09") == "Calle 18 # 09"
    assert contains_foreign_city_reference("Pasto, Colombia") is False
    assert contains_foreign_city_reference("Entregar en Popayán") is True
