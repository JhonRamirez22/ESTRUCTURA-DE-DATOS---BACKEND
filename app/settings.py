"""Configuración tipada del servidor sin registrar ni exponer secretos."""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = None if os.environ.get("VERCEL") == "1" else _REPOSITORY_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # En despliegue las variables llegan desde el runtime; la imagen no contiene .env.
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: SecretStr | None = None
    database_host: str | None = None
    database_port: int = Field(default=5432, gt=0, le=65_535)
    database_name: str | None = None
    database_username: str | None = None
    database_password: SecretStr | None = None
    database_admin_username: str | None = None
    database_admin_password: SecretStr | None = None
    # Conexiones por proceso; el pool por defecto de SQLAlchemy (5 + 10) quedaba corto con carga.
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Mueve pedidos entre repartidores para acortar las rutas (ver joint_assignment).
    joint_assignment_enabled: bool = True
    # Avisos en contexto para el despachador (ver dispatch_suggestions).
    ai_suggestions_enabled: bool = True
    # Respeta ventanas horarias y prioridades de los pedidos al ordenar la ruta.
    time_windows_enabled: bool = True
    # Groq solo redacta textos con cifras y alias, sin ubicaciones, nombres ni direcciones.
    ai_text_phrasing_enabled: bool = False
    # Muestra el botón «Renovar GPS demo»; solo para entornos de demostración.
    demo_mode: bool = False
    # A scheduled shipment becomes «due soon» this many minutes before its time.
    scheduled_lead_minutes: int = 60
    # Recalcula cada 10 s las rutas activas y propone mejoras (nunca las aplica solo).
    live_optimization_enabled: bool = True
    # Peticiones en curso por proceso; por encima se responde 503 al instante, sin colas.
    max_inflight_requests: int = Field(default=200, ge=1, le=10_000)
    database_ssl_mode: Literal["disable", "require", "verify-ca", "verify-full"] = "disable"
    database_ssl_root_cert: str | None = None
    auth_secret: SecretStr | None = None
    # Secreto compartido con el proxy de Vercel: si existe, solo ese proxy puede llamar a /api.
    origin_auth_secret: SecretStr | None = None
    dispatcher_access_code: SecretStr | None = None
    # Los códigos anteriores solo se habilitan durante una transición explícita.
    auth_legacy_code_login_enabled: bool = False
    app_allowed_origins: tuple[str, ...] = (
        "https://proyecto-estructuras-de-datos.vercel.app",
    )
    # Un proceso desplegado mal configurado nunca debe recibir credenciales locales.
    node_env: str = "production"
    routing_provider: Literal["osrm", "ors"] = "osrm"
    osrm_api_url: str = "https://router.project-osrm.org"
    ors_api_key: SecretStr | None = None
    ors_api_url: str = "https://api.openrouteservice.org"
    ai_traffic_provider: Literal["custom", "groq"] = "custom"
    tomtom_api_key: SecretStr | None = None
    tomtom_traffic_timeout_ms: int = Field(default=3_500, gt=0, le=60_000)
    ai_traffic_api_url: str | None = None
    ai_traffic_api_key: SecretStr | None = None
    ai_traffic_model: str | None = None
    ai_traffic_timeout_ms: int = 5_000
