from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/infrastructure/ → 上两级），.env 按绝对路径定位，
# 避免在 PyCharm/子目录下运行测试时因 CWD 不同而找不到 .env
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """当前项目的运行与外部服务配置"""

    amap_key: str | None = None
    amap_api_key: str | None = None  # 废弃别名（历史遗留）：新配置统一用 AMAP_KEY，读取时 amap_key 优先
    amap_base_url: str = "https://restapi.amap.com"
    amap_timeout_seconds: float = 10.0

    # 和风天气（天气数据源，替代高德天气）
    qweather_api_key: str | None = None  # 天气预报接口专用 key（/v7/weather/*）
    qweather_geo_api_key: str | None = None  # 城市搜索接口专用 key（/geo/v2/*）
    qweather_host: str | None = None  # 项目自定义域名，例如 mk54e6x6rw.re.qweatherapi.com
    qweather_forecast_days: int = 30  # 订阅支持的最大预报天数（端点选择上限），和风最多 30 天，按订阅级别调整
    qweather_timeout_seconds: float = 10.0

    redis_url: str | None = None
    redis_ttl_seconds: int = 86400

    # RAG 知识库（向量检索）
    embedding_model: str = "qwen3.7-text-embedding"  # 阿里云 Qwen-Embedding（1024 维），可切换 text-embedding-v3/v4
    embedding_dim: int = 1024
    embedding_batch_size: int = 16
    qdrant_url: str | None = None  # 如 http://localhost:6333
    qdrant_api_key: str | None = None
    qdrant_collection_prefix: str = "kb_"  # Qdrant 集合名前缀，避免与其他应用冲突

    llm_provider: str = "mock"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "qwen-plus"
    llm_temperature: float = 0.2
    llm_timeout: float = 60.0
    # 双开关等效：enable_mock_llm=true 或 llm_provider="mock" 任一命中即禁用 LLM（走规则路径）
    enable_mock_llm: bool = True

    # attraction_tool 自动沉淀：搜索确认的主要景点写回城市 json 并重导 Qdrant
    attraction_persist_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
