from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.infrastructure.config.settings import settings


@dataclass(slots=True)
class AmapClientConfig:
    """高德地图客户端配置。"""

    api_key: str | None = None
    base_url: str = "https://restapi.amap.com"
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "AmapClientConfig":
        return cls(
            api_key=getattr(settings, "amap_key", None) or getattr(settings, "amap_api_key", None),
            base_url=settings.amap_base_url,
            timeout_seconds=settings.amap_timeout_seconds,
        )


class AmapClient:
    """高德地图 API 客户端。"""

    def __init__(self, config: AmapClientConfig | None = None) -> None:
        self.config = config or AmapClientConfig.from_env()

    def is_enabled(self) -> bool:
        return bool(self.config.api_key)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.api_key:
            raise RuntimeError("Amap API key is not configured")

        request_params = dict(params or {})
        request_params.setdefault("key", self.config.api_key)
        url = f"{self.config.base_url}{path}"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.get(url, params=request_params)
            response.raise_for_status()
            return response.json()

    def geocode(self, *, address: str, city: str | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"address": address}
        if city:
            params["city"] = city
        data = self.get_json("/v3/geocode/geo", params)
        geocodes = data.get("geocodes") or []
        if not geocodes:
            return None
        item = geocodes[0]
        lng, lat = self._split_location(item.get("location"))
        return {
            "lng": lng,
            "lat": lat,
            "adcode": item.get("adcode"),
            "province": item.get("province"),
            "city": self._normalize_city_name(item.get("city")),
            "district": item.get("district"),
            "formatted_address": item.get("formatted_address"),
        }

    def search_pois(self, *, keywords: str, city: str | None = None, city_limit: bool = True) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"keywords": keywords, "offset": 10, "page": 1, "extensions": "all"}
        if city:
            params["city"] = city
            params["citylimit"] = "true" if city_limit else "false"
        data = self.get_json("/v5/place/text", params)
        pois = data.get("pois") or []
        normalized: list[dict[str, Any]] = []
        for item in pois:
            lng, lat = self._split_location(item.get("location"))
            normalized.append(
                {
                    "poi_id": item.get("id"),
                    "name": item.get("name"),
                    "lng": lng,
                    "lat": lat,
                    "adcode": item.get("adcode"),
                    "address": item.get("address"),
                    "type": item.get("type"),
                    "cityname": item.get("cityname"),
                    "pname": item.get("pname"),
                    "adname": item.get("adname"),
                }
            )
        return normalized

    def get_poi_by_id(self, *, poi_id: str) -> dict[str, Any] | None:
        data = self.get_json("/v5/place/detail", {"id": poi_id})
        pois = data.get("pois") or []
        if not pois:
            return None
        item = pois[0]
        lng, lat = self._split_location(item.get("location"))
        return {
            "poi_id": item.get("id"),
            "name": item.get("name"),
            "lng": lng,
            "lat": lat,
            "adcode": item.get("adcode"),
            "address": item.get("address"),
            "type": item.get("type"),
            "cityname": item.get("cityname"),
            "pname": item.get("pname"),
            "adname": item.get("adname"),
        }

    def measure_distance(self, *, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        data = self.get_json(
            "/v3/distance",
            {
                "origins": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "type": 1,
            },
        )
        results = data.get("results") or []
        if not results:
            return None
        item = results[0]
        return {
            "distance_meters": self._safe_float(item.get("distance")),
            "duration_seconds": self._safe_float(item.get("duration")),
        }

    def plan_transit(self, *, origin: tuple[float, float], destination: tuple[float, float], city: str) -> dict[str, Any] | None:
        data = self.get_json(
            "/v3/direction/transit/integrated",
            {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "city": city,
                "strategy": 0,
                "extensions": "all",
            },
        )
        route = data.get("route") or {}
        transits = route.get("transits") or []
        if not transits:
            return None
        best = transits[0]
        return {
            "mode": "transit",
            "distance_meters": self._safe_float(best.get("distance")),
            "duration_seconds": self._safe_float(best.get("duration")),
            "price": self._safe_float(best.get("cost")),
            "walking_distance": self._safe_float(best.get("walking_distance")),
            "transits": transits,
            "best": best,
            "raw": data,
        }

    def plan_driving(self, *, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        data = self.get_json(
            "/v3/direction/driving",
            {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "extensions": "all",
                "strategy": 0,
            },
        )
        route = data.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return None
        best = paths[0]
        return {
            "mode": "driving",
            "distance_meters": self._safe_float(best.get("distance")),
            "duration_seconds": self._safe_float(best.get("duration")),
            "traffic_lights": self._safe_int(best.get("traffic_lights")),
            "raw": data,
        }

    def plan_walking(self, *, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        data = self.get_json(
            "/v3/direction/walking",
            {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
            },
        )
        route = data.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return None
        best = paths[0]
        return {
            "mode": "walking",
            "distance_meters": self._safe_float(best.get("distance")),
            "duration_seconds": self._safe_float(best.get("duration")),
            "steps": best.get("steps") or [],
            "raw": data,
        }

    def plan_bicycling(self, *, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        data = self.get_json(
            "/v4/direction/bicycling",
            {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
            },
        )
        if int(data.get("errcode") or 0) != 0:
            return None
        data_node = data.get("data") or {}
        paths = data_node.get("paths") or []
        if not paths:
            return None
        best = paths[0]
        return {
            "mode": "bicycling",
            "distance_meters": self._safe_float(best.get("distance")),
            "duration_seconds": self._safe_float(best.get("duration")),
            "steps": best.get("steps") or [],
            "raw": data,
        }

    def get_weather_forecast(self, *, city: str, extensions: str = "all") -> dict[str, Any] | None:
        data = self.get_json(
            "/v3/weather/weatherInfo",
            {
                "city": city,
                "extensions": extensions,
                "output": "JSON",
            },
        )
        if str(data.get("status")) != "1" or str(data.get("infocode")) != "10000":
            return None

        forecasts = data.get("forecasts") or []
        if not forecasts:
            return None
        forecast = forecasts[0] or {}
        casts = forecast.get("casts") or []
        if not casts:
            return None

        return {
            "city": forecast.get("city") or str(city),
            "adcode": forecast.get("adcode"),
            "province": forecast.get("province"),
            "reporttime": forecast.get("reporttime"),
            "casts": [
                {
                    "date": item.get("date"),
                    "week": item.get("week"),
                    "dayweather": item.get("dayweather"),
                    "nightweather": item.get("nightweather"),
                    "daytemp": self._safe_int(item.get("daytemp")),
                    "nighttemp": self._safe_int(item.get("nighttemp")),
                    "daywind": item.get("daywind"),
                    "nightwind": item.get("nightwind"),
                    "daypower": item.get("daypower"),
                    "nightpower": item.get("nightpower"),
                }
                for item in casts
                if item.get("date")
            ],
        }

    def _split_location(self, raw: str | None) -> tuple[float | None, float | None]:
        if not raw or "," not in raw:
            return None, None
        lng_text, lat_text = raw.split(",", 1)
        return self._safe_float(lng_text), self._safe_float(lat_text)

    def _safe_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: Any) -> int | None:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _normalize_city_name(self, value: Any) -> str | None:
        if isinstance(value, list):
            return str(value[0]) if value else None
        if value in {None, ""}:
            return None
        return str(value)


amap_client = AmapClient(AmapClientConfig.from_env())
