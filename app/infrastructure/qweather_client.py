from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.infrastructure.conversions import retry_call, safe_int
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
        self._client = httpx.Client(timeout=self.config.timeout_seconds)  # 复用连接，避免每次请求重新握手

    def is_enabled(self) -> bool:
        return bool(self.config.api_key and self.config.geo_api_key and self.config.host)

    def geo_lookup(self, city: str) -> dict[str, Any] | None:
        """和风城市搜索（geo/v2/city/lookup）"""
        if not self.config.geo_api_key or not self.config.host:
            return None
        try:
            url = f"https://{self.config.host}/geo/v2/city/lookup"
            headers = {"X-QW-Api-Key": self.config.geo_api_key}
            response = retry_call(lambda: self._client.get(url, params={"location": city}, headers=headers))
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None
        if str(data.get("code")) != "200":
            return None
        locations = data.get("location") or []
        if not locations:
            return None
        item = locations[0]
        return {
            "location_id": item.get("id"),
            "lng": safe_int(item.get("lon")),
            "lat": safe_int(item.get("lat")),
        }

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.config.api_key or not self.config.host:
            raise RuntimeError("QWeather API key or host is not configured")
        url = f"https://{self.config.host}{path}"
        headers = {"X-QW-Api-Key": self.config.api_key}
        response = retry_call(lambda: self._client.get(url, params=params, headers=headers))
        response.raise_for_status()
        return response.json()

    def get_daily_forecast(self, *, location: str, days: int) -> list[dict[str, Any]]:
        """每日预报：按所需天数自动选择 /v7/weather/{days}d 端点"""
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
                "temp_max": safe_int(item.get("tempMax")),
                "temp_min": safe_int(item.get("tempMin")),
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


qweather_client = QWeatherClient(QWeatherClientConfig.from_env())
