from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.infrastructure.amap_client import amap_client
from app.infrastructure.conversions import safe_float, safe_str
from app.agent.tools.schema.meal import MealCandidate, MealInput, MealResult

# meal_tool 需求边界：给定目的地 + 饮食偏好 + 行程景点，返回真实餐饮候选，
# 供 LLM 在渲染阶段决定"哪天在哪家用餐"。工具只采集候选、不做安排。

@dataclass(slots=True)
class _RankedCandidate:
    candidate: MealCandidate
    nearest_distance_km: float | None
    near_spot_query: bool
    score: float = 0.0


class MealTool:
    """根据 Agent 提供的条件检索、过滤并排序真实餐饮候选"""

    name = "meal_tool"
    _MAX_QUERIES = 14
    _MAX_CANDIDATES_PER_QUERY = 12
    _RESULT_COUNT = 12
    _MAX_QUERY_SPOTS = 8  # 生成"XX附近美食"查询时使用的景点数（与 base/偏好共享查询额度），铺开覆盖各片区
    _MAX_GEOCODE_SPOTS = 10  # 计算距离时 geocode 的景点数

    def __init__(self, amap=None) -> None:
        # 可注入的后端（测试可替换为 fake）；默认走真实 amap_client
        self._amap = amap

    @property
    def amap(self):
        return self._amap if self._amap is not None else amap_client

    # 高德餐饮请求的兜底关键词，保证没有任何偏好词时也能抓到真实餐厅
    _BASE_FOOD_WORDS = ("美食", "餐厅", "小吃", "特色菜")
    # 高德 type 中代表"餐饮服务"的类别主干（多类别时取第一个分段判断，避免被其它类别误杀）
    _FOOD_TYPE_MARKERS = ("餐饮", "中餐厅", "火锅", "小吃", "咖啡", "茶馆", "面包甜点", "外国菜", "快餐")
    _FOOD_NAME_MARKERS = ("餐厅", "食堂", "小馆", "饭店", "火锅", "烧烤", "菜", "面馆", "小吃", "私房菜", "食府", "咖啡", "茶")
    _NOISE_NAME_WORDS = ("医院", "学校", "中学", "小学", "派出所", "机场", "公司", "超市", "药店")

    def run(self, meal_input: MealInput) -> MealResult:
        normalized = self._normalize_input(meal_input)
        queries = self._build_queries(normalized)
        spot_coords = self._spot_coords(normalized)
        ranked = self._search_candidates(normalized, queries, spot_coords)
        filtered = self._filter_and_rank(ranked, normalized)
        top_n = min(max(int(normalized.top_n or self._RESULT_COUNT), 1), self._RESULT_COUNT * 4)
        candidates = [item.candidate for item in filtered[:top_n]]
        return MealResult(
            city=normalized.destination,
            candidates=candidates,
            summary=(
                f"已根据饮食偏好和行程景点筛选出 {len(candidates)} 家真实餐饮候选。"
                if candidates
                else "暂未筛选到符合当前条件的餐饮候选。"
            ),
            debug={
                "query_count": len(queries),
                "raw_candidate_count": len(ranked),
                "filtered_candidate_count": len(filtered),
            },
        )

    def _normalize_input(self, meal_input: MealInput) -> MealInput:
        return meal_input.model_copy(
            update={
                "destination": str(meal_input.destination).strip(),
                "preferences": self._unique_strings(meal_input.preferences),
                "spots": self._unique_strings(meal_input.spots),
            },
            deep=True,
        )

    def _build_queries(self, meal_input: MealInput) -> list[tuple[str, str, str]]:
        city = meal_input.destination
        queries: list[tuple[str, str, str]] = [
            (word, city, "base") for word in self._BASE_FOOD_WORDS[:2]
        ]
        queries.extend(
            (f"{spot} 附近美食", city, "spot")
            for spot in meal_input.spots[: self._MAX_QUERY_SPOTS]
        )
        remaining = self._MAX_QUERIES - len(queries)
        pref_words = [
            pref for pref in meal_input.preferences
            if not _is_subjective_pref(pref)
        ]
        queries.extend(
            (f"{word} 美食", city, "preference") for word in pref_words[: max(remaining, 0)]
        )
        return self._unique_queries(queries)[: self._MAX_QUERIES]

    def _spot_coords(self, meal_input: MealInput) -> dict[str, tuple[float, float]]:
        if not self.amap.is_enabled():
            return {}
        coords: dict[str, tuple[float, float]] = {}
        for spot in meal_input.spots[: self._MAX_GEOCODE_SPOTS]:
            try:
                location = self.amap.geocode(address=spot, city=meal_input.destination)
            except Exception:
                location = None
            parsed = self._extract_coords(location)
            if parsed:
                coords[spot] = parsed
        return coords

    def _search_candidates(
        self,
        meal_input: MealInput,
        queries: list[tuple[str, str, str]],
        spot_coords: dict[str, tuple[float, float]],
    ) -> list[_RankedCandidate]:
        if not self.amap.is_enabled():
            return []
        indexed: dict[str, _RankedCandidate] = {}
        for keywords, city, query_type in queries:
            pois = self._search_pois_with_retry(keywords, city)
            for poi in pois[: self._MAX_CANDIDATES_PER_QUERY]:
                ranked = self._poi_to_ranked_candidate(meal_input, poi, spot_coords, query_type == "spot")
                if not ranked:
                    continue
                key = self._candidate_key(ranked.candidate)
                existing = indexed.get(key)
                if existing:
                    existing.near_spot_query = existing.near_spot_query or ranked.near_spot_query
                else:
                    indexed[key] = ranked
        return list(indexed.values())

    def _search_pois_with_retry(self, keywords: str, city: str) -> list[dict]:
        """高德对部分关键词偶发返回空，空结果时重试，降低整体空召回概率"""
        for _attempt in range(3):
            try:
                pois = self.amap.search_pois(keywords=keywords, city=city, city_limit=True) or []
            except Exception:
                pois = []
            if pois:
                return pois
        return []

    def _poi_to_ranked_candidate(
        self,
        meal_input: MealInput,
        poi: dict[str, Any],
        spot_coords: dict[str, tuple[float, float]],
        near_spot_query: bool,
    ) -> _RankedCandidate | None:
        name = str(poi.get("name") or "").strip()
        poi_type = str(poi.get("type") or "").strip()
        if not name or not self._is_food(name, poi_type):
            return None
        lng = safe_float(poi.get("lng"))
        lat = safe_float(poi.get("lat"))
        nearest = self._nearest_distance_km(spot_coords, lng, lat)
        area = poi.get("business_area") or poi.get("adname") or poi.get("cityname") or meal_input.destination
        return _RankedCandidate(
            candidate=MealCandidate(
                poi_id=safe_str(poi.get("poi_id")),
                name=name,
                area=safe_str(area),
                address=safe_str(poi.get("address")),
                lng=lng,
                lat=lat,
                rating=safe_str(poi.get("rating")),
                type_name=_type_label(poi_type),
                distance_to_spots_km=nearest,
            ),
            nearest_distance_km=nearest,
            near_spot_query=near_spot_query,
        )

    def _filter_and_rank(self, candidates: list[_RankedCandidate], meal_input: MealInput) -> list[_RankedCandidate]:
        for item in candidates:
            item.score = self._score_candidate(meal_input, item)
        return sorted(candidates, key=lambda item: (item.score, item.candidate.name))

    def _score_candidate(self, meal_input: MealInput, item: _RankedCandidate) -> float:
        candidate = item.candidate
        text = f"{candidate.name} {candidate.area or ''} {candidate.type_name or ''}".lower()
        score = 0.0
        # 评分越高越好（缺失评分不罚分）
        rating = safe_float(candidate.rating)
        if rating is not None:
            score -= max(0.0, (rating - 4.0)) * 4  # 4.0 → 0，4.5 → -2，4.8 → -3.2
        # 餐饮类型/名称含"餐厅/菜/火锅"等明确实体词，给基础加分
        if candidate.type_name:
            score -= 6
        # 偏好词命中餐饮类型或名称
        for preference in meal_input.preferences:
            pl = preference.lower()
            if pl in text:
                score -= 3
        # 靠近行程景点（来自"XX附近美食"查询）
        if item.near_spot_query:
            score -= 4
        # 距最近景点距离
        if item.nearest_distance_km is not None:
            if item.nearest_distance_km <= 2:
                score -= 6
            elif item.nearest_distance_km <= 5:
                score -= 2
            elif item.nearest_distance_km > 15:
                score += 8
        return score

    def _is_food(self, name: str, poi_type: str) -> bool:
        if any(word in name for word in self._NOISE_NAME_WORDS):
            return False
        # 高德 type 可能是多类别（用 | 或 ; 分隔），主类别是餐饮服务时直接视为餐饮
        main_type = str(poi_type).split("|")[0].split(";")[0].strip().lower()
        if any(marker in main_type for marker in self._FOOD_TYPE_MARKERS):
            return True
        return any(word in name for word in self._FOOD_NAME_MARKERS)

    def _nearest_distance_km(
        self,
        spot_coords: dict[str, tuple[float, float]],
        lng: float | None,
        lat: float | None,
    ) -> float | None:
        if lng is None or lat is None or not spot_coords:
            return None
        distances = [self._haversine_km(lng, lat, spot_lng, spot_lat) for spot_lng, spot_lat in spot_coords.values()]
        return round(min(distances), 1)

    def _haversine_km(self, lng1: float, lat1: float, lng2: float, lat2: float) -> float:
        radius = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlng = math.radians(lng2 - lng1)
        value = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
        return 2 * radius * math.asin(math.sqrt(value))

    def _candidate_key(self, candidate: MealCandidate) -> str:
        return candidate.poi_id or f"{candidate.name.strip().lower()}|{(candidate.address or '').strip().lower()}"

    def _extract_coords(self, item: dict[str, Any] | None) -> tuple[float, float] | None:
        if not item:
            return None
        lng = safe_float(item.get("lng"))
        lat = safe_float(item.get("lat"))
        return (lng, lat) if lng is not None and lat is not None else None

    def _unique_queries(self, queries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        unique: list[tuple[str, str, str]] = []
        seen: dict[tuple[str, str], int] = {}
        for keywords, city, query_type in queries:
            key = (str(keywords).strip(), str(city).strip())
            if not key[0] or not key[1]:
                continue
            if key not in seen:
                seen[key] = len(unique)
                unique.append((key[0], key[1], query_type))
            elif query_type == "spot":
                # 同一查询词先以 base/preference 出现、后以 spot 出现时，升级为 spot（靠近景点）
                unique[seen[key]] = (key[0], key[1], "spot")
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


def _is_subjective_pref(pref: str) -> bool:
    return any(
        word in pref
        for word in ("划算", "味道好", "好吃", "人气", "干净", "实惠", "性价比", "评分高", "便宜")
    )


def _type_label(poi_type: str) -> str | None:
    """从高德多类别 type 中提炼可读的餐饮类别（取第一个含类别标记的分段）"""
    first = str(poi_type).split("|")[0].strip()
    primary = first.split(";")[-1].strip() if ";" in first else first
    return safe_str(primary) or None


meal_tool = MealTool()