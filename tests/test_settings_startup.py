from __future__ import annotations

from app.settings import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_production_requires_a_long_session_secret() -> None:
    assert _settings(node_env="production").startup_problems()
    assert _settings(node_env="production", auth_secret="short").startup_problems()
    assert not _settings(node_env="production", auth_secret="x" * 40).startup_problems()


def test_development_is_only_allowed_with_a_local_database() -> None:
    remote = _settings(node_env="development", database_host="db.example.rds.amazonaws.com")
    assert remote.startup_problems()
    assert _settings(node_env="development", database_host="127.0.0.1").startup_problems() == []
    assert _settings(node_env="development").startup_problems() == []
    sqlite = _settings(node_env="development", database_url="sqlite:///./dev.db")
    assert sqlite.startup_problems() == []
    remote_url = _settings(
        node_env="development", database_url="postgresql://u:p@db.example.com/rutas",
    )
    assert remote_url.startup_problems()
