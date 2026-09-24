"""Application settings, loaded from the repo-root ``.env`` (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://sentinel:sentinel@localhost:5432/incident_sentinel"
    redis_url: str = "redis://localhost:6379/0"

    groq_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    llm_provider: str = "ollama"  # groq | ollama -- ollama by default for local dev

    environment: str = "development"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, value: str) -> str:
        """Normalise a hosted provider's URL to the driver actually installed.

        Render (and Heroku, and most managed Postgres) hand out a bare
        ``postgres://`` URL. SQLAlchemy 2.0 rejects that scheme outright, and
        plain ``postgresql://`` reaches for psycopg2, which is not in
        requirements -- this project uses psycopg 3. Rewriting the scheme here
        means the deploy config can pass the provider's string through
        untouched.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
