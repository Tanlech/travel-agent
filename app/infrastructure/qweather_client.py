from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.infrastructure.settings import settings


@dataclass(slots=True)
class QWeatherClientConfig:
    """和风天气客户端配置"""

    api_key: str | None = None  # 天气预报接口专用 key（/v7/weather/*）
    geo_api_key: str | None = None  # 城市搜索接口专用 key（/geo/v2/*）
    host: str | None = None  # 项目自定义域名，例如 mk54e6x6rw.re.qweatherapi.com
    timeout_seconds: float = 10.0
    forecast_days: int = 7

    @classmethod
    def from_env(cls) -> "QWeatherClientConfig":
        return cls(
            api_key=settings.qweather_api_key,
            geo_api_key=settings.qweather_geo_api_key,
            host=settings.qweather_host,
            timeout_seconds=settings.qweather_timeout_seconds,
            forecast_days=settings.qweather_forecast_days,
        )


class QWeatherClient:
    """和风天气 API 客户端（新版认证：X-QW-Api-Key 请求头）"""

    # 可用预报端点（days 必选 string，可选 3d/7d/10d/15d/30d）
    _AVAILABLE_ENDPOINTS = (3, 7, 10, 15, 30)

    def __init__(self, config: QWeatherClientConfig | None = None) -> None:
        self.config = config or QWeatherClientConfig.from_env()

    def is_enabled(self) -> bool:
        return bool(self.config.api_key and self.config.host)

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.config.api_key or not self.config.host:
            raise RuntimeError("QWeather API key or host is not configured")
        url = f"https://{self.config.host}{path}"
        headers = {"X-QW-Api-Key": self.config.api_key}
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()

    def get_daily_forecast(self, *, location: str, days: int) -> list[dict[str, Any]]:
        """每日预报：按所需天数自动选择 /v7/weather/{days}d 端点

        location 为 LocationID（如 101010100）或 "经度,纬度"（十进制，最多小数点后两位）；
        days 指"需要覆盖到今天起的第 N 天"，client 自动向上取整到
        合法端点（3d/7d/10d/15d/30d），且不超过订阅上限 QWEATHER_FORECAST_DAYS
        """
        endpoint = self._pick_endpoint(days)
        data = self._get_json(
            f"/v7/weather/{endpoint}",
            {"location": location},
        )
        if str(data.get("code")) != "200":
            return []
        daily = data.get("daily") or []
        if not isinstance(daily, list):
            return []
        return [
            {
                "date": item.get("fxDate"),
                "weather_day": item.get("textDay"),
                "temp_max": self._safe_int(item.get("tempMax")),
                "temp_min": self._safe_int(item.get("tempMin")),
                "wind_dir_day": item.get("windDirDay"),
                "wind_scale_day": item.get("windScaleDay"),
                "humidity": item.get("humidity"),
                "precip": item.get("precip"),
            }
            for item in daily
            if item.get("fxDate")
        ]

    def _pick_endpoint(self, days: int) -> str:
        """在订阅上限内，把所需天数向上取整到最近的可用预报端点"""
        max_days = max(min(int(self.config.forecast_days), 30), 1)
        needed = min(max(int(days), 1), max_days)
        for limit in self._AVAILABLE_ENDPOINTS:
            if limit > max_days:
                break
            if needed <= limit:
                return f"{limit}d"
        return f"{max_days}d"

    def _safe_int(self, value: Any) -> int | None:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


qweather_client = QWeatherClient(QWeatherClientConfig.from_env())
