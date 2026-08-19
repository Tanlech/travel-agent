from __future__ import annotations

from pydantic import BaseModel, Field


class WeatherInput(BaseModel):
    """天气查询输入：查询某城市某日期范围的逐日天气预报"""
    city: str = Field(description="目的地城市，必填")
    start_time: str = Field(description="行程开始日期，格式 YYYY-MM-DD，如 2026-08-23")
    end_time: str = Field(description="行程结束日期，格式 YYYY-MM-DD，须不早于开始日期")


class WeatherDay(BaseModel):
    date: str
    weather_day: str = ""
    temperature_range: str | None = None
    wind: str | None = None
    humidity: str | None = None
    precip: str | None = None


class WeatherResult(BaseModel):
    city: str
    start_date: str
    end_date: str
    daily: list[WeatherDay]
    error: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None
    missing_dates: list[str] = Field(default_factory=list)
