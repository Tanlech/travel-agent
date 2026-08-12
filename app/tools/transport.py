from __future__ import annotations

from app.infrastructure.amap.client import amap_client
from app.tools.schema.transport import TransportResult, TaxiRoute, TransitOptionSummary, TransitRoute, TransitRouteStep, WalkRoute


class TransportTool:
    name = "transport_tool"
    _DEFAULT_DISTANCE = 999999
    _DEFAULT_TRANSFER = 999
    _DEFAULT_DURATION = 999999
    _DEFAULT_COST = 999999

    def run(
        self,
        *,
        city: str,
        from_name: str | None = None,
        to_name: str | None = None,
        waypoints: list[str] | None = None,
    ) -> TransportResult:
        if waypoints:
            return TransportResult(
                city=city,
                from_name=from_name,
                to_name=to_name,
                error="当前 transport_tool 仅支持单段路线，不支持 waypoints",
                source="amap_transport",
            )

        return self._query_one(city, from_name, to_name)

    def _query_one(self, city: str, from_name: str | None, to_name: str | None) -> TransportResult:
        origin = self._resolve_poi(city, from_name)
        destination = self._resolve_poi(city, to_name)
        if not origin or not destination:
            return TransportResult(
                city=city,
                from_name=from_name,
                to_name=to_name,
                error="起点或终点未能解析",
                source="amap_transport",
            )

        route_data = self._fetch_routes(city, origin, destination)
        result = TransportResult(city=city, from_name=from_name, to_name=to_name, source="amap_transport")
        result.walk = self._build_walk_route(route_data.get("walk"))
        result.taxi = self._build_taxi_route(route_data.get("taxi"))
        result.transit = self._build_transit_route(route_data.get("transit"))

        if not result.walk and not result.taxi and not result.transit:
            result.error = "未能获取打车或公交路线"
        return result

    def _fetch_routes(self, city: str, origin: dict, destination: dict) -> dict[str, dict | None]:
        origin_coords = self._coords(origin)
        destination_coords = self._coords(destination)
        return {
            "walk": amap_client.plan_walking(origin=origin_coords, destination=destination_coords),
            "taxi": amap_client.plan_driving(origin=origin_coords, destination=destination_coords),
            "transit": amap_client.plan_transit(origin=origin_coords, destination=destination_coords, city=city),
        }

    def _build_walk_route(self, walk: dict | None) -> WalkRoute | None:
        if not walk:
            return None
        return WalkRoute(
            distance_meters=self._safe_int(walk.get("distance_meters")),
            duration_minutes=self._seconds_to_minutes(self._safe_int(walk.get("duration_seconds"))),
        )

    def _build_taxi_route(self, taxi: dict | None) -> TaxiRoute | None:
        if not taxi:
            return None
        distance_meters = self._safe_int(taxi.get("distance_meters"))
        return TaxiRoute(
            cost=self._safe_float(taxi.get("cost")) or self._estimate_taxi_cost(distance_meters),
            distance_meters=distance_meters,
            duration_minutes=self._seconds_to_minutes(self._safe_int(taxi.get("duration_seconds"))),
        )

    def _build_transit_route(self, transit: dict | None) -> TransitRoute | None:
        if not transit:
            return None

        option_summaries: list[TransitOptionSummary] = []
        for item in transit.get("transits") or []:
            summary = self._summarize_transit_option(item)
            if summary:
                option_summaries.append(summary)

        return TransitRoute(best_option=self._choose_best_transit_option(option_summaries))

    def _choose_best_transit_option(self, options: list[TransitOptionSummary]) -> TransitOptionSummary | None:
        if not options:
            return None
        # 优先级：耗时最短，其次步行更少，再次换乘更少，最后花费最少。
        return min(
            options,
            key=lambda item: (
                item.duration_minutes if item.duration_minutes is not None else self._DEFAULT_DURATION,
                item.walking_distance_meters if item.walking_distance_meters is not None else self._DEFAULT_DISTANCE,
                item.transfer_count if item.transfer_count is not None else self._DEFAULT_TRANSFER,
                item.cost if item.cost is not None else self._DEFAULT_COST,
            ),
        )

    def _summarize_transit_option(self, transit_option: dict | None) -> TransitOptionSummary | None:
        if not isinstance(transit_option, dict):
            return None

        steps = self._build_transit_steps(transit_option.get("segments") or [])
        return TransitOptionSummary(
            cost=self._safe_float(transit_option.get("cost")),
            duration_minutes=self._seconds_to_minutes(self._safe_int(transit_option.get("duration"))),
            distance_meters=self._safe_int(transit_option.get("distance")),
            walking_distance_meters=self._safe_int(transit_option.get("walking_distance")),
            transfer_count=self._count_transit_rides(steps),
            steps=steps,
        )

    def _build_transit_steps(self, segments: list[object]) -> list[TransitRouteStep]:
        # 每个 segment 先输出步行接驳，再输出乘坐线路，保持原始顺序。
        steps: list[TransitRouteStep] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            walk_step = self._summarize_transit_walk(segment)
            if walk_step:
                steps.append(walk_step)
            ride_step = self._summarize_transit_leg(segment)
            if ride_step:
                steps.append(ride_step)
        return steps

    def _count_transit_rides(self, steps: list[TransitRouteStep]) -> int | None:
        ride_count = sum(1 for step in steps if step.mode in {"metro", "bus"})
        return ride_count - 1 if ride_count else None

    def _summarize_transit_walk(self, segment: dict) -> TransitRouteStep | None:
        # 步行段只保留可用于排序和展示的最小字段集。
        walking = segment.get("walking")
        if not isinstance(walking, dict):
            return None
        return TransitRouteStep(
            mode="walk",
            distance_meters=self._safe_int(walking.get("distance")),
            duration_minutes=self._seconds_to_minutes(self._safe_int(walking.get("duration"))),
        )

    def _summarize_transit_leg(self, segment: dict) -> TransitRouteStep | None:
        # 乘坐段统一归一到 metro / bus，避免暴露高德的细分线路类型。
        busline = self._extract_busline(segment)
        if not busline:
            return None

        departure, arrival = self._extract_stops(busline)
        return TransitRouteStep(
            mode=self._normalize_transit_mode(busline.get("type")),
            name=busline.get("name"),
            distance_meters=self._safe_int(busline.get("distance")),
            duration_minutes=self._seconds_to_minutes(self._safe_int(busline.get("duration"))),
            from_station=departure.get("name"),
            to_station=arrival.get("name"),
        )

    def _extract_busline(self, segment: dict) -> dict | None:
        bus = segment.get("bus")
        if not isinstance(bus, dict):
            return None

        buslines = bus.get("buslines")
        if not isinstance(buslines, list) or not buslines:
            return None

        busline = buslines[0]
        return busline if isinstance(busline, dict) else None

    def _extract_stops(self, busline: dict) -> tuple[dict, dict]:
        departure = busline.get("departure_stop") if isinstance(busline.get("departure_stop"), dict) else {}
        arrival = busline.get("arrival_stop") if isinstance(busline.get("arrival_stop"), dict) else {}
        return departure, arrival

    def _resolve_poi(self, city: str, name: str | None) -> dict | None:
        if not name:
            return None
        pois = amap_client.search_pois(keywords=name, city=city, city_limit=True)
        if pois:
            return pois[0]
        geocoded = amap_client.geocode(address=name, city=city)
        if geocoded:
            return {"name": name, "lng": geocoded.get("lng"), "lat": geocoded.get("lat")}
        return None

    def _coords(self, poi: dict) -> tuple[float, float]:
        return poi["lng"], poi["lat"]

    def _normalize_transit_mode(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if "地铁" in value:
            return "metro"
        if any(keyword in value for keyword in ("公交", "无轨电车", "电车", "汽车")):
            return "bus"
        return "bus" if value else None

    def _safe_int(self, value) -> int | None:
        try:
            return int(float(value)) if value is not None else None
        except Exception:
            return None

    def _seconds_to_minutes(self, seconds: int | None) -> int | None:
        if seconds is None:
            return None
        return max((seconds + 59) // 60, 1)

    def _estimate_taxi_cost(self, distance_meters: int | None) -> float | None:
        if distance_meters is None:
            return None
        return round(max(10.0, 14.0 + distance_meters / 1000.0 * 2.8), 1)

    def _safe_float(self, value) -> float | None:
        try:
            return float(value) if value is not None else None
        except Exception:
            return None


transport_tool = TransportTool()
