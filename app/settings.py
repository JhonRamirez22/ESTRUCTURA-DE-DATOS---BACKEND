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
    database_ssl_mode: Literal["disable", "require", "verify-ca", "verify-full"] = "disable"
    database_ssl_root_cert: str | None = None
    auth_secret: SecretStr | None = None
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
    ai_allow_location_data_sharing: bool = False
    ai_allow_route_location_sharing: bool = False
    # Guarda cada medición de TomTom por tramo y hora (tabla traffic_observations). Apagado.
    traffic_history_enabled: bool = False
    customer_chat_api_url: str | None = "https://api.groq.com/openai/v1/chat/completions"
    customer_chat_api_key: SecretStr | None = None
    customer_chat_model: str | None = "openai/gpt-oss-20b"
    customer_chat_timeout_ms: int = 5_000
    notification_smtp_host: str | None = None
    notification_smtp_port: int = 587
    notification_smtp_username: str | None = None
    notification_smtp_password: SecretStr | None = None
    notification_from_email: str | None = None
    notification_smtp_timeout_ms: int = Field(default=5_000, gt=0, le=60_000)
    # El poller infinito solo se habilita en un proceso persistente, nunca por defecto.
    # El alias /api heredado queda apagado: la API se publica solo bajo /api/v1.
    legacy_api_alias_enabled: bool = False
    # Peticiones por minuto y cuenta; frena inundaciones desde un cliente manipulado.
    account_read_limit_per_minute: int = Field(default=300, ge=10, le=10_000)
    account_write_limit_per_minute: int = Field(default=90, ge=5, le=10_000)
    # Avisos al cliente por correo y SMS: apagados en el producto actual.
    customer_notifications_enabled: bool = False
    customer_notification_worker_enabled: bool = False
    customer_notification_poll_seconds: int = Field(default=30, ge=5, le=3_600)
    notification_sms_twilio_account_sid: str | None = None
    notification_sms_twilio_auth_token: SecretStr | None = None
    notification_sms_twilio_from_phone: str | None = None
    notification_sms_timeout_ms: int = Field(default=5_000, gt=0, le=60_000)
    next_public_map_default_lat: float = 1.2136
    next_public_map_default_lng: float = -77.2811

    @field_validator(
        "next_public_map_default_lat",
        "next_public_map_default_lng",
        mode="before",
    )
    @classmethod
    def use_map_default_when_coordinate_is_empty(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if value != "":
            return value
        if info.field_name == "next_public_map_default_lat":
            return 1.2136
        return -77.2811

    @property
    def sqlalchemy_database_url(self) -> URL:
        database_url = (
            self.database_url.get_secret_value().strip() if self.database_url is not None else ""
        )
        if database_url:
            url = make_url(database_url)
        else:
            required = {
                "DATABASE_HOST": self.database_host,
                "DATABASE_NAME": self.database_name,
                "DATABASE_USERNAME": self.database_username,
                "DATABASE_PASSWORD": (
                    self.database_password.get_secret_value()
                    if self.database_password is not None
                    else None
                ),
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "Configura DATABASE_URL o las variables separadas de conexión: "
                    + ", ".join(missing)
                )

            url = URL.create(
                drivername="postgresql+asyncpg",
                username=self.database_username,
                password=(
                    self.database_password.get_secret_value()
                    if self.database_password is not None
                    else None
                ),
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
            )
        query = dict(url.query)
        query.pop("schema", None)  # Prisma's schema query parameter is not an asyncpg option.
        # asyncpg recibe TLS como `ssl` (un SSLContext), no como `sslmode` de libpq.
        query.pop("sslmode", None)
        query.pop("sslrootcert", None)
        driver_name = url.drivername
        if driver_name in {"postgres", "postgresql"}:
            driver_name = "postgresql+asyncpg"
        elif driver_name == "sqlite":
            driver_name = "sqlite+aiosqlite"
        return url.set(drivername=driver_name, query=query)

    @property
    def sqlalchemy_connect_args(self) -> dict[str, object]:
        database_url = (
            self.database_url.get_secret_value().strip() if self.database_url is not None else ""
        )
        url_query = make_url(database_url).query if database_url else {}
        ssl_mode = str(url_query.get("sslmode", self.database_ssl_mode))
        root_cert = str(url_query.get("sslrootcert", self.database_ssl_root_cert or ""))

        if ssl_mode == "disable":
            return {}
        if ssl_mode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("DATABASE_SSL_MODE debe ser un modo SSL válido para PostgreSQL")
        if ssl_mode in {"verify-ca", "verify-full"} and not root_cert:
            raise ValueError(
                "DATABASE_SSL_ROOT_CERT es requerido al verificar el certificado de RDS"
            )

        if ssl_mode == "require":
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        else:
            ssl_context = ssl.create_default_context(cafile=root_cert)
            if ssl_mode == "verify-ca":
                ssl_context.check_hostname = False

        return {"ssl": ssl_context}

    @property
    def is_development(self) -> bool:
        return self.node_env == "development"

    def startup_problems(self) -> list[str]:
        """Errores de configuración que deben impedir el arranque, no solo avisar en el log."""
        problems: list[str] = []
        if self.node_env == "production":
            secret = self.auth_secret.get_secret_value().strip() if self.auth_secret else ""
            if len(secret.encode("utf-8")) < 32:
                problems.append("AUTH_SECRET debe tener al menos 32 bytes en producción.")
        elif self.is_development and not self._database_is_local():
            problems.append("NODE_ENV=development solo se permite con una base de datos local.")
        return problems

    def _database_is_local(self) -> bool:
        raw_url = self.database_url.get_secret_value().strip() if self.database_url else ""
        if raw_url:
            url = make_url(raw_url)
            if url.drivername.startswith("sqlite"):
                return True
            host = url.host
        else:
            host = self.database_host
        return host in {None, "", "localhost", "127.0.0.1", "::1"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
