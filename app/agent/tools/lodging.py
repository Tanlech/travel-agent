from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.infrastructure.amap_client import amap_client
from app.infrastructure.conversions import safe_float, safe_str
from app.agent.tools.schema.lodging import LodgingCandidate, LodgingInput, LodgingResult

# lodging_tool 的需求边界：给定目的地 + 偏好 + 行程景点，返回 top N 住宿候选，LLM 自主选择锚点

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
    _MAX_QUERY_SPOTS = 3  # 生成"XX附近酒店"查询时使用的景点数（与 base/grade/pref 共享 6 条查询额度）
    _MAX_GEOCODE_SPOTS = 4  # 计算距离时 geocode 的景点数（应 >= _MAX_QUERY_SPOTS）
    _MAX_DISTANCE_KM = 40  # 距最近景点超过该距离的候选直接过滤

    def __init__(self, amap=None) -> None:
        # 可注入的后端（测试可替换为 fake）；默认走真实 amap_client
        self._amap = amap

    @property
    def amap(self):
        return self._amap if self._amap is not None else amap_client

    _GRADE_HINTS = ("三星", "四星", "五星", "经济", "连锁", "舒适", "豪华", "高档", "快捷", "商务", "民宿", "旅馆", "青年旅舍", "青旅")
    # 用户档次偏好词 → 高德 keytag 候选（避免"星"子串误匹配所有星级）
    _GRADE_KEYTAG_MAP = {
        "三星": ("三星级酒店", "三星"),
        "四星": ("四星级酒店", "四星"),
        "五星": ("五星级酒店", "豪华型"),
        "经济": ("经济型", "快捷酒店"),
        "连锁": ("连锁",),
        "舒适": ("舒适型",),
        "豪华": ("豪华型", "五星级酒店"),
        "高档": ("高档型", "豪华型"),
        "快捷": ("快捷酒店", "经济型"),
        "商务": ("商务酒店",),
        "民宿": ("民宿",),
        "旅馆": ("旅馆",),
        "青年旅舍": ("青年旅舍",),
        "青旅": ("青年旅舍",),
    }
    _SUBJECTIVE_PREFS = ("交通方便", "干净", "安静", "温馨", "实惠", "性价比", "位置好", "适合作为全程锚点", "市中心", "商圈", "近地铁", "地铁站", "核心区")
    _LODGING_WORDS = ("酒店", "饭店", "宾馆", "客栈", "旅舍", "民宿", "hotel", "inn", "hostel", "residence")
    _STRONG_LODGING_WORDS = ("酒店", "饭店", "宾馆", "hotel")
    _NON_LODGING_WORDS = (
        "餐饮", "小吃", "火锅", "咖啡", "茶馆", "景点", "风景", "博物馆", "公园", "图书馆",
        "购物", "商场", "美食", "快餐", "面馆", "故居", "纪念馆", "湿地", "广场", "景区",
    )
    _NOISE_NAME_WORDS = ("医院", "学校", "中学", "小学", "派出所", "机场", "公司", "超市")

    def run(self, lodging_input: LodgingInput) -> LodgingResult:
        normalized = self._normalize_input(lodging_input)
        queries = self._build_queries(normalized)
        spot_coords = self._spot_coords(normalized)
        ranked = self._search_candidates(normalized, queries, spot_coords)
        filtered = self._filter_and_rank(ranked, normalized)
        top_n = min(max(int(normalized.top_n or self._RESULT_COUNT), 1), self._RESULT_COUNT * 4)
        candidates = [item.candidate for item in filtered[:top_n]]
        return LodgingResult(
            city=normalized.destination,
            candidates=candidates,
            summary=(
                f"已根据偏好和行程景点筛选出 {len(candidates)} 家住宿候选。"
                if candidates
                else "暂未筛选到符合当前条件的住宿候选。"
            ),
            debug={
                "query_count": len(queries),
                "raw_candidate_count": len(ranked),
                "filtered_candidate_count": len(filtered),
                "no_rating_count": sum(1 for c in candidates if not c.rating),
                "no_keytag_count": sum(1 for c in candidates if not c.keytag),
            },
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
        queries.extend((f"{spot} 附近酒店", city, "spot") for spot in lodging_input.spots[: self._MAX_QUERY_SPOTS])
        remaining = self._MAX_QUERIES - len(queries)
        # 档次词优先（用户硬性偏好），超出额度时先保证档次词
        grade_words = self._grade_words(lodging_input)[:remaining]
        queries.extend((word, city, "grade") for word in grade_words)
        remaining = self._MAX_QUERIES - len(queries)
        pref_words = [
            pref for pref in lodging_input.preferences
            if not any(hint in pref for hint in self._GRADE_HINTS)
            and not any(word in pref for word in self._SUBJECTIVE_PREFS)
            and f"{pref}酒店" not in grade_words
            and pref not in grade_words
        ]
        queries.extend((f"{word} 酒店", city, "preference") for word in pref_words[:remaining])
        return self._unique_queries(queries)[: self._MAX_QUERIES]

    def _grade_words(self, lodging_input: LodgingInput) -> list[str]:
        return self._unique_strings(
            [pref if "酒店" in pref else f"{pref}酒店" for pref in lodging_input.preferences if any(hint in pref for hint in self._GRADE_HINTS)]
        )

    def _spot_coords(self, lodging_input: LodgingInput) -> dict[str, tuple[float, float]]:
        if not self.amap.is_enabled():
            return {}
        coords: dict[str, tuple[float, float]] = {}
        for spot in lodging_input.spots[: self._MAX_GEOCODE_SPOTS]:
            try:
                location = self.amap.geocode(address=spot, city=lodging_input.destination)
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
        if not self.amap.is_enabled():
            return []
        indexed: dict[str, _RankedCandidate] = {}
        for keywords, city, query_type in queries:
            pois = self._search_pois_with_retry(keywords, city)
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
        # 兜底：档次关键词（如"四星酒店"）偶发全空时，从 base 结果放宽 keytag 过滤补充，
        # 避免返回空候选（候选仍带 keytag，LLM 可自行判断档次匹配度）
        if not indexed:
            for keywords, city, query_type in queries:
                if query_type != "base":
                    continue
                pois = self._search_pois_with_retry(keywords, city)
                for poi in pois[: self._MAX_CANDIDATES_PER_QUERY]:
                    ranked = self._poi_to_ranked_candidate(
                        lodging_input, poi, spot_coords, False, enforce_keytag=False
                    )
                    if not ranked:
                        continue
                    key = self._candidate_key(ranked.candidate)
                    if key not in indexed:
                        indexed[key] = ranked
        return list(indexed.values())

    def _search_pois_with_retry(self, keywords: str, city: str) -> list[dict]:
        """高德对部分关键词（如"四星酒店"）偶发返回空，空结果时重试（约 1/3 失败率，3 次后 ~1/27）"""
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
        lodging_input: LodgingInput,
        poi: dict[str, Any],
        spot_coords: dict[str, tuple[float, float]],
        near_spot_query: bool,
        enforce_keytag: bool = True,
    ) -> _RankedCandidate | None:
        name = str(poi.get("name") or "").strip()
        poi_type = str(poi.get("type") or "").strip()
        if not name or self._is_non_lodging(name, poi_type):
            return None
        lng = safe_float(poi.get("lng"))
        lat = safe_float(poi.get("lat"))
        nearest, average = self._distances_to_spots(spot_coords, lng, lat)
        area = poi.get("business_area") or poi.get("adname") or poi.get("cityname") or lodging_input.destination
        keytag = safe_str(poi.get("keytag"))
        if enforce_keytag and not self._keytag_matches_preferences(keytag, lodging_input.preferences):
            return None
        return _RankedCandidate(
            candidate=LodgingCandidate(
                poi_id=safe_str(poi.get("poi_id")),
                name=name,
                area=safe_str(area),
                address=safe_str(poi.get("address")),
                tel=safe_str(poi.get("tel")),
                rating=safe_str(poi.get("rating")),
                keytag=keytag,
                distance_to_spots_km=nearest,
            ),
            lng=lng,
            lat=lat,
            nearest_distance_km=nearest,
            average_distance_km=average,
            near_spot_query=near_spot_query,
        )

    def _keytag_matches_preferences(self, keytag: str | None, preferences: list[str]) -> bool:
        """档次偏好（含档次词）与高德 keytag 匹配，基于精确映射表。

        有档次偏好但 keytag 缺失时保留（信息不全不误杀）；
        keytag 存在时，任一偏好词映射的候选 keytag 命中即通过。
        """
        grade_prefs = [pref for pref in preferences if any(hint in pref for hint in self._GRADE_HINTS)]
        if not grade_prefs or not keytag:
            return True
        tag_text = keytag.strip().lower()
        for pref in grade_prefs:
            for keyword, candidates in self._GRADE_KEYTAG_MAP.items():
                if keyword in pref:
                    if any(candidate in tag_text for candidate in candidates):
                        return True
        return False

    def _filter_and_rank(self, candidates: list[_RankedCandidate], lodging_input: LodgingInput) -> list[_RankedCandidate]:
        accepted: list[_RankedCandidate] = []
        for item in candidates:
            candidate = item.candidate
            if self._matches_avoid_keywords(candidate, lodging_input.avoid_keywords):
                continue
            if item.nearest_distance_km is not None and item.nearest_distance_km > self._MAX_DISTANCE_KM:
                continue
            if not self._has_minimum_information(item):
                continue
            item.score = self._score_candidate(lodging_input, item)
            accepted.append(item)
        return sorted(accepted, key=lambda item: (item.score, item.candidate.name))

    def _matches_avoid_keywords(self, candidate: LodgingCandidate, avoid_keywords: list[str]) -> bool:
        """规避关键词命中名称或 keytag（如 avoid='民宿' 时 keytag='民宿' 的候选也排除）"""
        if not avoid_keywords:
            return False
        name_text = candidate.name.lower()
        keytag_text = (candidate.keytag or "").lower()
        return any(
            keyword.lower() in name_text or (keytag_text and keyword.lower() in keytag_text)
            for keyword in avoid_keywords
            if keyword
        )

    def _has_minimum_information(self, item: _RankedCandidate) -> bool:
        has_location = item.lng is not None and item.lat is not None
        has_identity = bool(item.candidate.poi_id or item.candidate.address)
        return has_location and has_identity

    def _score_candidate(self, lodging_input: LodgingInput, item: _RankedCandidate) -> float:
        candidate = item.candidate
        text = f"{candidate.name} {candidate.area or ''} {candidate.address or ''}".lower()
        score = 0.0
        # 评分越高越好（缺失评分不加分也不罚分）
        rating = safe_float(candidate.rating)
        if rating is not None:
            score -= max(0.0, (rating - 4.0)) * 4  # 4.0 → 0，4.5 → -2，4.8 → -3.2
        # 名称是明确住宿实体
        if any(word in text for word in self._STRONG_LODGING_WORDS):
            score -= 8
        # 档次偏好命中（keytag 已在前置过滤，此处给额外加分）
        if candidate.keytag and self._keytag_matches_preferences(candidate.keytag, lodging_input.preferences):
            score -= 3
        # 来自"XX附近酒店"查询，说明靠近行程景点，给较强优先
        if item.near_spot_query:
            score -= 4
        # 距最近景点距离
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
            # 偏好词命中名称/区域视为命中（keytag 档次命中已在上方加分，避免重复计数）
            if preference.lower() in text:
                score -= 2
        return score

    def _is_non_lodging(self, name: str, poi_type: str) -> bool:
        text = f"{name} {poi_type}".lower()
        if any(word in name for word in self._NOISE_NAME_WORDS):
            return True
        # 高德 type 可能是多类别（用 | 或 ; 分隔），主类别是住宿服务时直接视为住宿，
        # 避免"住宿服务;宾馆酒店;四星级宾馆|餐饮服务;..."被"餐饮"等排除词误杀
        main_type = str(poi_type).split("|")[0].split(";")[0].strip().lower()
        if "住宿" in main_type:
            return False
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


lodging_tool = LodgingTool()
