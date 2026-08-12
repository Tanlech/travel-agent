from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.infrastructure.amap.client import amap_client
from app.tools.schema.weather import WeatherDay, WeatherResult


class WeatherTool:
    name = "weather_tool"
    _DATE_FORMAT = "%Y-%m-%d"

    def run(self, *, city: str, start_time: str, end_time: str) -> WeatherResult:
        start_date = self._parse_date(start_time)
        end_date = self._parse_date(end_time)
        if start_date is None or end_date is None or start_date > end_date:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], source="amap_weather", error="日期范围无效")

        weather_data = self._fetch_weather(city)
        if not weather_data:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], source="amap_weather", error="未能获取天气数据")

        daily = self._normalize_weather(weather_data, start_date=start_date, end_date=end_date)
        return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=daily, source="amap_weather")

    def _fetch_weather(self, city: str) -> dict:
        if not amap_client.is_enabled():
            return {}
        try:
            return amap_client.get_weather_forecast(city=city) or {}
        except Exception:
            return {}

    def _normalize_weather(self, weather_data: dict, *, start_date, end_date) -> list[WeatherDay]:
        casts = weather_data.get("casts") or []
        if not isinstance(casts, list):
            return []

        days = max((end_date - start_date).days + 1, 1)
        normalized: list[WeatherDay] = []
        for index, item in enumerate(casts[:days]):
            if not isinstance(item, dict):
                continue
            current_date = start_date + timedelta(days=index)
            daytemp = self._to_float(item.get("daytemp"))
            nighttemp = self._to_float(item.get("nighttemp"))
            normalized.append(
                WeatherDay(
                    date=str(current_date),
                    weather=str(item.get("dayweather") or item.get("nightweather") or ""),
                    temperature_range=self._build_temp_range(daytemp, nighttemp),
                )
            )
        return normalized

    def _parse_date(self, value: str):
        try:
            return datetime.strptime(value, self._DATE_FORMAT).date()
        except Exception:
            return None

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    def _build_temp_range(self, daytemp: float | None, nighttemp: float | None) -> str | None:
        if daytemp is not None and nighttemp is not None:
            return f"{int(nighttemp)}°C–{int(daytemp)}°C"
        if daytemp is not None:
            return f"{int(daytemp)}°C"
        if nighttemp is not None:
            return f"{int(nighttemp)}°C"
        return None


weather_tool = WeatherTool()
