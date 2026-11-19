"""Environment-based configuration. Nothing secret is hard-coded."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field("development", alias="APP_ENV")
    app_name: str = Field("ERP Data Modelling Intelligence Platform", alias="APP_NAME")
    database_url: str = Field("sqlite:///./erpdm.db", alias="DATABASE_URL")
    db_pool_size: int = Field(5, alias="DB_POOL_SIZE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_file: str | None = Field(None, alias="LOG_FILE")

    # Bootstrap admin (only used when no admin exists yet)
    admin_username: str | None = Field(None, alias="ADMIN_USERNAME")
    admin_password: SecretStr | None = Field(None, alias="ADMIN_PASSWORD")
    admin_email: str | None = Field(None, alias="ADMIN_EMAIL")

    session_timeout_minutes: int = Field(60, alias="SESSION_TIMEOUT_MINUTES")
    max_failed_logins: int = Field(5, alias="MAX_FAILED_LOGINS")
    lockout_minutes: int = Field(15, alias="LOCKOUT_MINUTES")
    min_password_length: int = Field(10, alias="MIN_PASSWORD_LENGTH")

    # LLM
    llm_provider: str = Field("none", alias="LLM_PROVIDER")  # anthropic|openai|azure_openai|ollama|none
    llm_model: str | None = Field(None, alias="LLM_MODEL")
    llm_api_key: SecretStr | None = Field(None, alias="LLM_API_KEY")
    llm_base_url: str | None = Field(None, alias="LLM_BASE_URL")
    llm_api_version: str | None = Field(None, alias="LLM_API_VERSION")  # Azure
    llm_timeout_seconds: int = Field(60, alias="LLM_TIMEOUT_SECONDS")
    llm_max_tokens: int = Field(2000, alias="LLM_MAX_TOKENS")
    llm_temperature: float = Field(0.1, alias="LLM_TEMPERATURE")

    # Generators
    default_language_key: str = Field("E", alias="DEFAULT_LANGUAGE_KEY")
    raw_loaded_at_column: str = Field("_loaded_at", alias="RAW_LOADED_AT_COLUMN")
    raw_database: str = Field("RAW", alias="RAW_DATABASE")
    target_database: str = Field("ANALYTICS", alias="TARGET_DATABASE")
    max_upload_mb: int = Field(50, alias="MAX_UPLOAD_MB")
    seed_sample_metadata: bool = Field(True, alias="SEED_SAMPLE_METADATA")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider in ("", "none", None):
            return False
        if self.llm_provider == "ollama":
            return True
        return self.llm_api_key is not None and bool(self.llm_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
