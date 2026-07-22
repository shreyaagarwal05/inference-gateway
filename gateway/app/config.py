"""Environment-backed application settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    redis_url: str = "redis://redis-stack:6379"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = None
    tenants_config_path: str = "/app/config/tenants.yaml"
    gateway_port: int = 8000
    embedding_model_name: str = "all-MiniLM-L6-v2"
    thread_pool_workers: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
