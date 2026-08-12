from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """当前项目的运行与外部服务配置。"""

    app_name: str = "travel-agent"
    app_env: str = "dev"
    debug: bool = False

    amap_key: str | None = None
    amap_api_key: str | None = None
    amap_base_url: str = "https://restapi.amap.com"
    amap_timeout_seconds: float = 10.0

    redis_url: str | None = None
    redis_ttl_seconds: int = 86400

    llm_provider: str = "mock"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "qwen-plus"
    llm_temperature: float = 0.2
    llm_timeout: float = 60.0
    enable_mock_llm: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
