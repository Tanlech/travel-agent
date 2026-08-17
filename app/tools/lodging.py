from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.infrastructure.amap_client import amap_client
from app.tools.schema.lodging import LodgingCandidate, LodgingInput, LodgingResult


@dataclass(slots=True)
class _RankedCandidate:
    candidate: LodgingCandidate
    lng: float | None
    lat: float | None
    nearest_distance_km: float | None
    average_distance_km: float | None
    near_spot_query: bool
    score: float = 0.0


class LodgingTool:
    """根据 Agent 提供的条件检索、过滤并排序真实住宿候选"""

    name = "lodging_tool"
    _MAX_QUERIES = 6
    _MAX_CANDIDATES_PER_QUERY = 10
    _RESULT_COUNT = 5
    _MAX_GEOCODE_SPOTS = 4
    _GRADE_HINTS = ("星", "经济型", "连锁", "舒适", "豪华", "高档", "快捷")
    _SUBJECTIVE_PREFS = ("交通方便", "干净", "安静", "温馨", "实惠", "性价比", "位置好", "适合作为全程锚点")
    _LODGING_WORDS = ("酒店", "饭店", "宾馆", "客栈", "旅舍", "民宿", "hotel", "inn", "hostel", "residence")
    _STRONG_LODGING_WORDS = ("酒店", "饭店", "宾馆", "hotel")
    _NON_LODGING_WORDS = (
        "餐饮", "小吃", "火锅", "咖啡", "茶馆", "景点", "风景", "博物馆", "公园", "图书馆",
        "购物", "商场", "美食", "快餐", "面馆", "故居", "纪念馆", "湿地", "广场", "景区",
    )
    _NOISE_NAME_WORDS = ("医院", "学校", "中学", "小学", "酒店式公寓", "派出所", "机场", "公司", "超市")

    def run(self, lodging_input: LodgingInput) -> LodgingResult:
        normalized = self._normalize_input(lodging_input)
        queries = self._build_queries(normalized)
        spot_coords = self._spot_coords(normalized)
        ranked = self._search_candidates(normalized, queries, spot_coords)
        candidates = [item.candidate for item in self._filter_and_rank(ranked, normalized)[: self._RESULT_COUNT]]
        return LodgingResult(
            city=normalized.destination,
            candidates=candidates,
            summary=(
                f"已根据预算、偏好和行程景点筛选出 {len(candidates)} 家住宿候选。"
                if candidates
                else "暂未筛选到符合当前条件的住宿候选。"
            ),
        )

    def _normalize_input(self, lodging_input: LodgingInput) -> LodgingInput:
        return lodging_input.model_copy(
            update={
                "destination": str(lodging_input.destination).strip(),
                "preferences": self._unique_strings(lodging_input.preferences),
                "avoid_keywords": self._unique_strings(lodging_input.avoid_keywords),
                "spots": self._unique_strings(lodging_input.spots),
            },
            deep=True,
        )

    def _build_queries(self, lodging_input: LodgingInput) -> list[tuple[str, str, str]]:
        city = lodging_input.destination
        queries: list[tuple[str, str, str]] = [("酒店", city, "base")]
        queries.extend((f"{spot} 附近酒店", city, "spot") for spot in lodging_input.spots[:3])
        remaining = self._MAX_QUERIES - len(queries)
        queries.extend((word, city, "grade") for word in self._grade_words(lodging_input)[:remaining])
        remaining = self._MAX_QUERIES - len(queries)
        pref_words = [
            pref for pref in lodging_input.preferences
            if not any(hint in pref for hint in self._GRADE_HINTS)
            and not any(word in pref for word in self._SUBJECTIVE_PREFS)
        ]
        queries.extend((f"{word} 酒店", city, "preference") for word in pref_words[:remaining])
        return self._unique_queries(queries)[: self._MAX_QUERIES]

    def _grade_words(self, lodging_input: LodgingInput) -> list[str]:
        words = [pref if "酒店" in pref else f"{pref}酒店" for pref in lodging_input.preferences if any(hint in pref for hint in self._GRADE_HINTS)]
        if words or not lodging_input.budget:
            return self._unique_strings(words)
        if lodging_input.budget <= 300:
            return ["经济型酒店"]
        if lodging_input.budget <= 600:
            return ["舒适型酒店"]
        if lodging_input.budget <= 1500:
            return ["四星酒店"]
        return ["五星酒店"]

    def _spot_coords(self, lodging_input: LodgingInput) -> dict[str, tuple[float, float]]:
        if not amap_client.is_enabled():
            return {}
        coords: dict[str, tuple[float, float]] = {}
        for spot in lodging_input.spots[: self._MAX_GEOCODE_SPOTS]:
            try:
                location = amap_client.geocode(address=spot, city=lodging_input.destination)
            except Exception:
                location = None
            parsed = self._extract_coords(location)
            if parsed:
                coords[spot] = parsed
        return coords

    def _search_candidates(
        self,
        lodging_input: LodgingInput,
        queries: list[tuple[str, str, str]],
        spot_coords: dict[str, tuple[float, float]],
    ) -> list[_RankedCandidate]:
        if not amap_client.is_enabled():
            return []
        indexed: dict[str, _RankedCandidate] = {}
        for keywords, city, query_type in queries:
            try:
                pois = amap_client.search_pois(keywords=keywords, city=city, city_limit=True) or []
            except Exception:
                continue
            for poi in pois[: self._MAX_CANDIDATES_PER_QUERY]:
                ranked = self._poi_to_ranked_candidate(lodging_input, poi, spot_coords, query_type == "spot")
                if not ranked:
                    continue
                key = self._candidate_key(ranked.candidate)
                existing = indexed.get(key)
                if existing:
                    existing.near_spot_query = existing.near_spot_query or ranked.near_spot_query
                else:
                    indexed[key] = ranked
        return list(indexed.values())

    def _poi_to_ranked_candidate(
        self,
        lodging_input: LodgingInput,
        poi: dict[str, Any],
        spot_coords: dict[str, tuple[float, float]],
        near_spot_query: bool,
    ) -> _RankedCandidate | None:
        name = str(poi.get("name") or "").strip()
        poi_type = str(poi.get("type") or "").strip()
        if not name or self._is_non_lodging(name, poi_type):
            return None
        lng = self._to_float(poi.get("lng"))
        lat = self._to_float(poi.get("lat"))
        nearest, average = self._distances_to_spots(spot_coords, lng, lat)
        area = poi.get("business_area") or poi.get("adname") or poi.get("cityname") or lodging_input.destination
        return _RankedCandidate(
            candidate=LodgingCandidate(
                poi_id=self._optional_string(poi.get("poi_id")),
                name=name,
                area=self._optional_string(area),
                price=self._optional_string(poi.get("price")),
                address=self._optional_string(poi.get("address")),
                tel=self._optional_string(poi.get("tel")),
            ),
            lng=lng,
            lat=lat,
            nearest_distance_km=nearest,
            average_distance_km=average,
            near_spot_query=near_spot_query,
        )

    def _filter_and_rank(self, candidates: list[_RankedCandidate], lodging_input: LodgingInput) -> list[_RankedCandidate]:
        accepted: list[_RankedCandidate] = []
        for item in candidates:
            candidate = item.candidate
            if any(keyword.lower() in candidate.name.lower() for keyword in lodging_input.avoid_keywords if keyword):
                continue
            if item.nearest_distance_km is not None and item.nearest_distance_km > 40:
                continue
            if not self._has_minimum_information(item):
                continue
            item.score = self._score_candidate(lodging_input, item)
            accepted.append(item)
        return sorted(
            accepted,
            key=lambda item: (
                item.score,
                item.candidate.name,
                item.candidate.name,
            ),
        )

    def _has_minimum_information(self, item: _RankedCandidate) -> bool:
        has_location = item.lng is not None and item.lat is not None
        has_identity = bool(item.candidate.poi_id or item.candidate.address)
        return has_location and has_identity

    def _score_candidate(self, lodging_input: LodgingInput, item: _RankedCandidate) -> float:
        candidate = item.candidate
        text = f"{candidate.name} {candidate.area or ''}".lower()
        score = 0.0
        if any(word in text for word in self._STRONG_LODGING_WORDS):
            score -= 8
        if lodging_input.budget and self._to_float(candidate.price) is None:
            score += 2
        price = self._to_float(candidate.price)
        if lodging_input.budget and price is not None:
            ratio = price / lodging_input.budget
            if ratio <= 1:
                score -= 4
            elif ratio <= 1.15:
                score += 1
            elif ratio <= 1.35:
                score += 6
            else:
                score += 14
        if item.near_spot_query:
            score -= 2
        if item.nearest_distance_km is not None:
            if item.nearest_distance_km <= 3:
                score -= 5
            elif item.nearest_distance_km <= 8:
                score -= 2
            elif item.nearest_distance_km > 25:
                score += 18
            elif item.nearest_distance_km > 15:
                score += 7
        if item.average_distance_km is not None:
            if item.average_distance_km <= 8:
                score -= 4
            elif item.average_distance_km > 25:
                score += 9
            elif item.average_distance_km > 15:
                score += 4
        for preference in lodging_input.preferences:
            if preference.lower() in text:
                score -= 2
        return score

    def _is_non_lodging(self, name: str, poi_type: str) -> bool:
        text = f"{name} {poi_type}".lower()
        if any(word in name for word in self._NOISE_NAME_WORDS):
            return True
        if any(word in text for word in self._NON_LODGING_WORDS):
            return True
        return not any(word in text for word in self._LODGING_WORDS)

    def _distances_to_spots(
        self,
        coords: dict[str, tuple[float, float]],
        lng: float | None,
        lat: float | None,
    ) -> tuple[float | None, float | None]:
        if lng is None or lat is None or not coords:
            return None, None
        distances = [self._haversine_km(lng, lat, spot_lng, spot_lat) for spot_lng, spot_lat in coords.values()]
        return round(min(distances), 1), round(sum(distances) / len(distances), 1)

    def _haversine_km(self, lng1: float, lat1: float, lng2: float, lat2: float) -> float:
        radius = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlng = math.radians(lng2 - lng1)
        value = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
        return 2 * radius * math.asin(math.sqrt(value))

    def _candidate_key(self, candidate: LodgingCandidate) -> str:
        return candidate.poi_id or f"{candidate.name.strip().lower()}|{(candidate.address or '').strip().lower()}"

    def _extract_coords(self, item: dict[str, Any] | None) -> tuple[float, float] | None:
        if not item:
            return None
        lng = self._to_float(item.get("lng"))
        lat = self._to_float(item.get("lat"))
        return (lng, lat) if lng is not None and lat is not None else None

    def _unique_queries(self, queries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        unique: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for keywords, city, query_type in queries:
            key = (str(keywords).strip(), str(city).strip())
            if key[0] and key[1] and key not in seen:
                seen.add(key)
                unique.append((key[0], key[1], query_type))
        return unique

    def _unique_strings(self, items: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)
        return unique

    def _optional_string(self, value: Any) -> str | None:
        if value in (None, "", [], {}):
            return None
        return str(value).strip() or None

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


lodging_tool = LodgingTool()
