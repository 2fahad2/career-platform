"""Application settings, loaded from the environment.

Secrets come from an untracked .env (never committed — §15.15). Staging and
production load DISTINCT files with DISTINCT credentials; the two environments
share nothing.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["staging", "production"] = Field(default="staging", alias="CAREER_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Postgres — application role (NOT a superuser; RLS applies to it).
    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(default="career", alias="DB_NAME")
    db_user: str = Field(default="career_app", alias="DB_USER")
    db_password: str = Field(default="", alias="DB_PASSWORD")

    # Postgres — owner/migration role (runs Alembic; owns tables).
    db_owner_user: str = Field(default="career_owner", alias="DB_OWNER_USER")
    db_owner_password: str = Field(default="", alias="DB_OWNER_PASSWORD")

    # Redis
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")

    # Object storage (filesystem-backed StorageAdapter for now).
    storage_root: str = Field(default="./data", alias="STORAGE_ROOT")

    # Anthropic — the only LLM provider (locked). Unused until C7.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # SearchAPI.io — Google Jobs discovery source (C6, deviation D13).
    searchapi_api_key: str = Field(default="", alias="SEARCHAPI_API_KEY")

    # Salla — billing/webhooks (C3). Filled when the Partner App is created.
    salla_webhook_secret: str = Field(default="", alias="SALLA_WEBHOOK_SECRET")
    salla_api_key: str = Field(default="", alias="SALLA_API_KEY")

    # WhatsApp Cloud API (C4). Filled when the Meta app + WABA are set up.
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    whatsapp_verify_token: str = Field(default="", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_number_e164: str = Field(default="", alias="WHATSAPP_NUMBER_E164")
    whatsapp_waba_id: str = Field(default="", alias="WHATSAPP_WABA_ID")

    # Telegram admin channel (C4). Bot token must be rotated before live use.
    telegram_admin_bot_token: str = Field(default="", alias="TELEGRAM_ADMIN_BOT_TOKEN")
    telegram_admin_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_CHAT_ID")

    # Canary phase: the operator's own WhatsApp number (evening window nudge).
    canary_test_phone: str = Field(default="", alias="CANARY_TEST_PHONE")

    def _dsn(self, user: str, password: str) -> str:
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def app_database_url(self) -> str:
        """DSN for the application role — subject to RLS."""
        return self._dsn(self.db_user, self.db_password)

    @property
    def owner_database_url(self) -> str:
        """DSN for the owner/migration role — used by Alembic only."""
        return self._dsn(self.db_owner_user, self.db_owner_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
