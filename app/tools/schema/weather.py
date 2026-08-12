from __future__ import annotations

from pydantic import BaseModel


class WeatherDay(BaseModel):
    date: str
    weather: str = ""
    temperature_range: str | None = None


class WeatherResult(BaseModel):
    city: str
    start_date: str
    end_date: str
    daily: list[WeatherDay]
    source: str | None = None
    error: str | None = None
