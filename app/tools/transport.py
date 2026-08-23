from __future__ import annotations

from app.infrastructure.amap_client import amap_client
from app.tools.schema.transport import TransportInput, TransportResult, TaxiRoute, TransitOptionSummary, TransitRouteStep, WalkRoute

# transport_tool 的需求边界：给定城市 + 起点/终点 + 可选途经点，返回每段的步行/打车/公交方案，供 LLM 判断通行可行性


class TransportTool:
    """根据 Agent 提供的条件查询同城单段或多段交通路线（步行/打车/公交）"""

    name = "transport_tool"
    _DEFAULT_DISTANCE = 999999
    _DEFAULT_TRANSFER = 999
    _DEFAULT_DURATION = 999999
    _DEFAULT_COST = 999999
    _MAX_SEGMENTS = 5  # 单次查询最多拆分的路段数，避免 waypoints 过多导致 API 调用爆炸

    def run(self, input_data: TransportInput) -> list[TransportResult]:
        """查询单段或多段路线，每段返回独立 TransportResult（walk/taxi/transit 三选一或三选多）

        多地点时按顺序连接：from_name → waypoint[0] → ... → waypoint[n-1] → to_name
        """
        city = input_data.city
        if not amap_client.is_enabled():
            return [
                TransportResult(
                    city=city,
                    from_name=input_data.from_name,
                    to_name=input_data.to_name,
                    error="高德地图未配置（AMAP_KEY）",
                    source="amap_transport",
                )
            ]

        points = self._build_route_points(input_data.from_name, input_data.to_name, input_data.waypoints)
        if len(points) < 2:
            return [
                TransportResult(
                    city=city,
                    from_name=input_data.from_name,
                    to_name=input_data.to_name,
                    error="至少需要起点和终点两个地点",
                    source="amap_transport",
                )
            ]
        if len(points) - 1 > self._MAX_SEGMENTS:
            points = points[: self._MAX_SEGMENTS + 1]
            truncate_note = f"途经点过多，已截断，仅查询前 {self._MAX_SEGMENTS} 段"
        else:
            truncate_note = None

        results: list[TransportResult] = []
        for index in range(len(points) - 1):
            result = self._query_one(city, points[index], points[index + 1])
            if truncate_note:
                result.note = truncate_note
            results.append(result)
        return results

    def _build_route_points(self, from_name: str | None, to_name: str | None, waypoints: list[str]) -> list[str]:
        """组装有序地点列表，过滤空值与相邻重复"""
        points: list[str] = []
        for raw in [from_name] + list(waypoints or []) + [to_name]:
            name = str(raw or "").strip()
            if not name or (points and points[-1] == name):
                continue
            points.append(name)
        return points

    def _query_one(self, city: str, from_name: str | None, to_name: str | None) -> TransportResult:
        origin = self._resolve_poi(city, from_name)
        destination = self._resolve_poi(city, to_name)
        if not origin:
            return TransportResult(
                city=city,
                from_name=from_name,
                to_name=to_name,
                error=f"起点未能解析（{from_name}）",
                source="amap_transport",
            )
        if not destination:
            return TransportResult(
                city=city,
                from_name=from_name,
                to_name=to_name,
                error=f"终点未能解析（{to_name}）",
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
        if origin_coords is None or destination_coords is None:
            return {"walk": None, "taxi": None, "transit": None}
        return {
            "walk": self._safe_fetch(lambda: amap_client.plan_walking(origin=origin_coords, destination=destination_coords)),
            "taxi": self._safe_fetch(lambda: amap_client.plan_driving(origin=origin_coords, destination=destination_coords)),
            "transit": self._safe_fetch(lambda: amap_client.plan_transit(origin=origin_coords, destination=destination_coords, city=city)),
        }

    def _safe_fetch(self, callable) -> dict | None:
        """单个 API 独立降级：失败只影响该方式，不拖累其他方式"""
        try:
            return callable()
        except Exception:
            return None

    def _build_walk_route(self, walk: dict | None) -> WalkRoute | None:
        if not walk:
            return None
        return WalkRoute(
            distance_meters=safe_int(walk.get("distance_meters")),
            duration_minutes=self._seconds_to_minutes(safe_int(walk.get("duration_seconds"))),
        )

    def _build_taxi_route(self, taxi: dict | None) -> TaxiRoute | None:
        if not taxi:
            return None
        distance_meters = safe_int(taxi.get("distance_meters"))
        return TaxiRoute(
            cost=safe_float(taxi.get("cost")) or self._estimate_taxi_cost(distance_meters),
            distance_meters=distance_meters,
            duration_minutes=self._seconds_to_minutes(safe_int(taxi.get("duration_seconds"))),
        )

    def _build_transit_route(self, transit: dict | None) -> TransitOptionSummary | None:
        if not transit:
            return None

        option_summaries: list[TransitOptionSummary] = []
        for item in transit.get("transits") or []:
            summary = self._summarize_transit_option(item)
            if summary:
                option_summaries.append(summary)

        return self._choose_best_transit_option(option_summaries)

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
            cost=safe_float(transit_option.get("cost")),
            duration_minutes=self._seconds_to_minutes(safe_int(transit_option.get("duration"))),
            distance_meters=safe_int(transit_option.get("distance")),
            walking_distance_meters=safe_int(transit_option.get("walking_distance")),
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
        return max(ride_count - 1, 0) if ride_count else None

    def _summarize_transit_walk(self, segment: dict) -> TransitRouteStep | None:
        # 步行段只保留可用于排序和展示的最小字段集
        walking = segment.get("walking")
        if not isinstance(walking, dict):
            return None
        return TransitRouteStep(
            mode="walk",
            distance_meters=safe_int(walking.get("distance")),
            duration_minutes=self._seconds_to_minutes(safe_int(walking.get("duration"))),
        )

    def _summarize_transit_leg(self, segment: dict) -> TransitRouteStep | None:
        # 乘坐段统一归一到 metro / bus，避免暴露高德的细分线路类型
        busline = self._extract_busline(segment)
        if not busline:
            return None

        departure, arrival = self._extract_stops(busline)
        return TransitRouteStep(
            mode=self._normalize_transit_mode(busline.get("type")),
            name=busline.get("name"),
            distance_meters=safe_int(busline.get("distance")),
            duration_minutes=self._seconds_to_minutes(safe_int(busline.get("duration"))),
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
        try:
            pois = amap_client.search_pois(keywords=name, city=city, city_limit=True)
        except Exception:
            return None
        if pois:
            return pois[0]
        try:
            geocoded = amap_client.geocode(address=name, city=city)
        except Exception:
            return None
        if geocoded:
            return {"name": name, "lng": geocoded.get("lng"), "lat": geocoded.get("lat")}
        return None

    def _coords(self, poi: dict) -> tuple[float, float] | None:
        lng = poi.get("lng")
        lat = poi.get("lat")
        if lng is None or lat is None:
            return None
        return lng, lat

    def _normalize_transit_mode(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if "地铁" in value:
            return "metro"
        if any(keyword in value for keyword in ("公交", "无轨电车", "电车", "汽车")):
            return "bus"
        return "bus" if value else None

    def _seconds_to_minutes(self, seconds: int | None) -> int | None:
        if seconds is None:
            return None
        minutes = round(seconds / 60)
        # 0 秒的真实换乘（如站内换乘）应保持 0，仅当确实不足 1 分钟但大于 0 时才记为 1
        return max(minutes, 1) if minutes == 0 and seconds > 0 else minutes

    def _estimate_taxi_cost(self, distance_meters: int | None) -> float | None:
        """打车费估算 fallback：高德 plan_driving 通常返回 route.taxi_cost（真实预估），
        仅当缺失时按全国一价粗略估算（起步 14 元 + 2.8 元/km），不代表真实计费。"""
        if distance_meters is None:
            return None
        return round(max(10.0, 14.0 + distance_meters / 1000.0 * 2.8), 1)


transport_tool = TransportTool()
