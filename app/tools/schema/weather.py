from __future__ import annotations

from pydantic import BaseModel


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
    source: str | None = None
    error: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None
