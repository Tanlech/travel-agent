from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.domain.common.dates import normalize_date
from app.infrastructure.conversions import safe_float
from app.infrastructure.qweather_client import qweather_client
from app.tools.schema.weather import WeatherDay, WeatherInput, WeatherResult

# weather_tool 的需求边界：给定城市 + 日期范围，返回逐日天气预报，供 LLM 约束户外/室内安排


class WeatherTool:
    """根据 Agent 提供的条件查询某城市某日期范围的逐日天气预报"""

    name = "weather_tool"

    def run(self, input_data: WeatherInput) -> WeatherResult:
        city = input_data.city
        start_time = input_data.start_time
        end_time = input_data.end_time
        start_date = self._parse_date(start_time)
        end_date = self._parse_date(end_time)
        if start_date is None or end_date is None or start_date > end_date:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], error="日期范围无效")

        if not qweather_client.is_enabled():
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], error="和风天气未配置（QWEATHER_API_KEY / QWEATHER_GEO_API_KEY / QWEATHER_HOST）")

        forecast, fetch_error = self._fetch_forecast(city, start_date=start_date, end_date=end_date)
        if fetch_error:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], error=fetch_error)
        if not forecast:
            return WeatherResult(city=city, start_date=start_time, end_date=end_time, daily=[], error="未能获取天气数据")

        daily, covered, missing = self._build_daily(forecast, start_date=start_date, end_date=end_date)
        if not covered:
            return WeatherResult(
                city=city,
                start_date=start_time,
                end_date=end_time,
                daily=daily,
                error=(
                    "预报窗口未覆盖行程日期"
                    f"（和风仅支持未来 {qweather_client.config.forecast_days} 天预报，"
                    "超出窗口的行程日无天气数据）"
                ),
                missing_dates=missing,
            )

        return WeatherResult(
            city=city,
            start_date=start_time,
            end_date=end_time,
            daily=daily,
            coverage_start=covered[0],
            coverage_end=covered[-1],
            missing_dates=missing,
        )

    def _fetch_forecast(self, city: str, *, start_date: date, end_date: date) -> tuple[list[dict], str | None]:
        """城市搜索 → location（LocationID 或经纬度） → 每日预报

        返回 (forecast, error)：error 区分城市解析失败与预报接口失败
        """
        geo = None
        try:
            geo = qweather_client.geo_lookup(city)
        except Exception:
            geo = None
        if not geo:
            return [], f"城市位置解析失败（{city}）"
        location = geo.get("location_id") or self._format_location(geo.get("lng"), geo.get("lat"))
        if not location:
            return [], f"城市位置解析失败（{city}）：未返回有效坐标或 LocationID"
        try:
            forecast = qweather_client.get_daily_forecast(
                location=location,
                days=self._forecast_days_needed(start_date, end_date),
            )
        except Exception:
            return [], "天气预报接口调用失败"
        return forecast, None

    def _format_location(self, lng: float | None, lat: float | None) -> str | None:
        """经纬度格式化为 "经度,纬度"（最多小数点后两位，满足和风 location 参数要求）"""
        if lng is None or lat is None:
            return None
        return f"{lng:.2f},{lat:.2f}"

    def _forecast_days_needed(self, start_date: date, end_date: date) -> int:
        """所需预报天数（从今天到行程结束），至少覆盖整个行程窗口"""
        trip_end_days = (end_date - date.today()).days + 1
        trip_span_days = (end_date - start_date).days + 1
        return max(trip_end_days, trip_span_days, 1)

    def _build_daily(self, forecast: list[dict], *, start_date: date, end_date: date) -> tuple[list[WeatherDay], list[str], list[str]]:
        """按行程日期逐天对齐；缺失的天补空占位，保证 daily 与行程天数一一对应

        返回 (daily, covered, missing)：covered 为有预报数据的日期，missing 为无预报数据的日期
        """
        by_date = {item.get("date"): item for item in forecast if item.get("date")}
        days = max((end_date - start_date).days + 1, 1)
        daily: list[WeatherDay] = []
        covered: list[str] = []
        missing: list[str] = []
        for offset in range(days):
            current_date = start_date + timedelta(days=offset)
            item = by_date.get(str(current_date))
            if not item:
                daily.append(WeatherDay(date=str(current_date)))
                missing.append(str(current_date))
                continue
            weather_day = str(item.get("weather_day") or "")
            humidity = str(item.get("humidity") or "").strip()
            precip = str(item.get("precip") or "").strip()
            daily.append(
                WeatherDay(
                    date=str(current_date),
                    temperature_range=self._build_temp_range(safe_float(item.get("temp_min")), safe_float(item.get("temp_max"))),
                    weather_day=weather_day,
                    wind=self._build_wind(item.get("wind_dir_day"), item.get("wind_scale_day")),
                    humidity=f"{humidity}%" if humidity else None,
                    precip=f"{precip}mm" if precip else None,
                )
            )
            covered.append(str(current_date))
        return daily, covered, missing

    def _parse_date(self, value: str) -> date | None:
        """日期解析：复用项目统一归一化，兼容 YYYY-MM-DD / 2026/8/17 / 8月17日 等格式"""
        text = normalize_date(value)
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
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
