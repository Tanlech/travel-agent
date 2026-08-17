from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from app.domain.common.dates import normalize_date
from app.infrastructure.qweather_client import qweather_client
from app.tools.schema.weather import WeatherDay, WeatherResult


class WeatherTool:
    name = "weather_tool"

    def run(self, *, city: str, start_time: str, end_time: str) -> WeatherResult:
        start_date = self._parse_date(start_time)
        end_date = self._parse_date(end_time)
        if start_date is None or end_date is None or start_date > end_date:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], source="qweather", error="日期范围无效")

        if not qweather_client.is_enabled():
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], source="qweather", error="和风天气未配置（QWEATHER_API_KEY / QWEATHER_GEO_API_KEY / QWEATHER_HOST）")

        forecast = self._fetch_forecast(city, end_date=end_date)
        if not forecast:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], source="qweather", error="未能获取天气数据")

        daily, covered = self._build_daily(forecast, start_date=start_date, end_date=end_date)
        if not covered:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=daily, source="qweather", error="预报窗口未覆盖行程日期（和风仅支持未来 N 天预报，超出窗口的行程日无天气数据）")

        return WeatherResult(
            city=city,
            start_date=start_time,
            end_date=end_time,
            daily=daily,
            source="qweather",
            coverage_start=covered[0],
            coverage_end=covered[-1],
        )

    def _fetch_forecast(self, city: str, *, end_date: date) -> list[dict]:
        """城市搜索 → location（LocationID 或经纬度） → 每日预报"""
        try:
            geo = self._search_city(city)
            if not geo:
                return []
            location = geo.get("location_id") or self._format_location(geo.get("lng"), geo.get("lat"))
            if not location:
                return []
            return qweather_client.get_daily_forecast(
                location=location,
                days=self._forecast_days_needed(end_date),
            )
        except Exception:
            return []

    def _format_location(self, lng: float | None, lat: float | None) -> str | None:
        """经纬度格式化为 "经度,纬度"（最多小数点后两位，满足和风 location 参数要求）"""
        if lng is None or lat is None:
            return None
        return f"{lng:.2f},{lat:.2f}"

    def _forecast_days_needed(self, end_date: date) -> int:
        """和风预报从今天起算：所需预报天数 = 行程结束日距今天的天数 + 1（端点上限由订阅配置约束）"""
        return max((end_date - date.today()).days + 1, 1)

    def _search_city(self, city: str) -> dict[str, Any] | None:
        """和风城市搜索（geo/v2/city/lookup）：只返回每天预报需要的字段

        输出：location_id（LocationID，优先用于预报）、lng/lat（经纬度备选）
        """
        host = qweather_client.config.host
        api_key = qweather_client.config.geo_api_key
        if not host or not api_key:
            return None
        try:
            with httpx.Client(timeout=qweather_client.config.timeout_seconds) as client:
                response = client.get(
                    f"https://{host}/geo/v2/city/lookup",
                    params={"location": city},
                    headers={"X-QW-Api-Key": api_key},
                )
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
            "lng": self._to_float(item.get("lon")),
            "lat": self._to_float(item.get("lat")),
        }

    def _build_daily(self, forecast: list[dict], *, start_date: date, end_date: date) -> tuple[list[WeatherDay], list[str]]:
        """按行程日期逐天对齐，真实预报日期匹配；缺失的天补空占位，保证 daily 与行程天数一一对应

        返回 (daily, covered)：covered 为实际有预报数据的日期列表（按行程日期顺序）
        """
        by_date = {item.get("date"): item for item in forecast if item.get("date")}
        days = max((end_date - start_date).days + 1, 1)
        daily: list[WeatherDay] = []
        covered: list[str] = []
        for offset in range(days):
            current_date = start_date + timedelta(days=offset)
            item = by_date.get(str(current_date))
            if not item:
                daily.append(WeatherDay(date=str(current_date)))
                continue
            weather_day = str(item.get("weather_day") or "")
            humidity = str(item.get("humidity") or "").strip()
            precip = str(item.get("precip") or "").strip()
            daily.append(
                WeatherDay(
                    date=str(current_date),
                    temperature_range=self._build_temp_range(self._to_float(item.get("temp_min")), self._to_float(item.get("temp_max"))),
                    weather_day=weather_day,
                    wind=self._build_wind(item.get("wind_dir_day"), item.get("wind_scale_day")),
                    humidity=f"{humidity}%" if humidity else None,
                    precip=f"{precip}mm" if precip else None,
                )
            )
            covered.append(str(current_date))
        return daily, covered

    def _parse_date(self, value: str):
        """日期解析：复用项目统一归一化，兼容 YYYY-MM-DD / 2026/8/17 / 8月17日 等格式"""
        text = normalize_date(value)
        if not text:
            return None
        return date.fromisoformat(text)

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    def _build_temp_range(self, temp_min: float | None, temp_max: float | None) -> str | None:
        if temp_min is not None and temp_max is not None:
            return f"{int(temp_min)}°C–{int(temp_max)}°C"
        if temp_max is not None:
            return f"{int(temp_max)}°C"
        if temp_min is not None:
            return f"{int(temp_min)}°C"
        return None

    def _build_wind(self, wind_dir: Any, wind_scale: Any) -> str | None:
        text = f"{str(wind_dir or '').strip()} {str(wind_scale or '').strip()}".strip()
        if not text:
            return None
        if wind_scale and not str(wind_scale).strip().endswith("级"):
            text += "级"
        return text


weather_tool = WeatherTool()
