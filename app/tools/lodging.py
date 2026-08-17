from __future__ import annotations

from typing import Any

from app.infrastructure.amap_client import amap_client
from app.tools.schema.lodging import LodgingCandidate, LodgingInput, LodgingResult, SelectedLodging


class LodgingTool:
    """酒店候选工具：纯规则（无 LLM）→ 高德 POI 搜索 → 评分排序 → top 候选 + 主推"""

    name = "lodging_tool"
    _MAX_QUERIES = 5
    _MAX_CANDIDATES_PER_QUERY = 10
    _RESULT_COUNT = 5  # 返回给 agent 的候选数量
    _GRADE_HINTS = ("星", "经济型", "连锁", "舒适", "豪华", "高档", "快捷")  # preferences 里的档位偏好词提示

    def run(self, lodging_input: LodgingInput, transport=None) -> LodgingResult:
        lodging_input = self._merge_transport_spots(lodging_input, transport)
        self._last_queries: list[dict[str, str]] = []

        candidates = self._search_candidates(lodging_input, self._build_queries(lodging_input))
        filtered = self._filter_candidates(candidates, lodging_input)
        top = filtered[: self._RESULT_COUNT] or candidates[: self._RESULT_COUNT]
        chosen = self._choose_selected_lodging(lodging_input, top)

        return LodgingResult(
            city=lodging_input.destination,
            candidates=top,
            selected_lodging=chosen,
            summary=self._summary(top, chosen),
            source="amap_poi",
            raw={"query_count": len(self._last_queries), "queries": self._last_queries, "raw_candidate_count": len(candidates)},
        )

    def _merge_transport_spots(self, lodging_input: LodgingInput, transport: Any) -> LodgingInput:
        if transport and getattr(transport, "daily_reports", None):
            spots = list(lodging_input.spots)
            for report in transport.daily_reports[:3]:
                if getattr(report, "attractions", None):
                    spots.extend(str(item).strip() for item in report.attractions if str(item).strip())
            lodging_input.spots = self._unique_strings(spots)
        return lodging_input

    def _grade_words(self, lodging_input: LodgingInput) -> list[str]:
        """档位词：preferences 里的档位偏好优先，否则按 budget 推导（通用规则）"""
        words: list[str] = []
        for pref in lodging_input.preferences:
            if any(hint in pref for hint in self._GRADE_HINTS):
                word = pref if "酒店" in pref else f"{pref}酒店"
                if word not in words:
                    words.append(word)
        if not words and lodging_input.budget:
            budget = lodging_input.budget
            if budget <= 300:
                words.append("经济型酒店")
            elif budget <= 600:
                words.append("舒适型酒店")
            elif budget <= 1500:
                words.append("四星酒店")
            else:
                words.append("五星酒店")
        return words

    def _build_queries(self, lodging_input: LodgingInput) -> list[tuple[str, str, str]]:
        """规则生成搜索词，优先级 base > spot > grade > pref，通用且聚焦"""
        city = lodging_input.destination
        queries: list[tuple[str, str, str]] = [(f"{city} 酒店", city, "base")]
        for spot in lodging_input.spots[:3]:
            queries.append((f"{city} {spot} 附近酒店", city, "spot"))
        for word in self._grade_words(lodging_input)[:2]:
            queries.append((f"{city} {word}", city, "grade"))
        pref_words = [pref for pref in lodging_input.preferences if not any(hint in pref for hint in self._GRADE_HINTS)]
        for word in pref_words[:2]:
            queries.append((f"{city} {word} 酒店", city, "pref"))
        return self._unique_queries(queries)[: self._MAX_QUERIES]

    def _search_candidates(self, lodging_input: LodgingInput, queries: list[tuple[str, str, str]]) -> list[LodgingCandidate]:
        if not amap_client.is_enabled():
            return []
        seen: set[str] = set()
        candidates: list[LodgingCandidate] = []
        for keywords, city, query_type in queries[: self._MAX_QUERIES]:
            self._last_queries.append({"city": city, "keywords": keywords, "type": query_type})
            try:
                pois = amap_client.search_pois(keywords=keywords, city=city) or []
            except Exception:
                pois = []
            for poi in pois[: self._MAX_CANDIDATES_PER_QUERY]:
                candidate = self._poi_to_candidate(lodging_input, poi, near_spot=(query_type == "spot"))
                if candidate is None or candidate.name in seen:
                    continue
                seen.add(candidate.name)
                candidates.append(candidate)
        return candidates

    def _score_candidate(self, lodging_input: LodgingInput, candidate: LodgingCandidate) -> int:
        score = 0
        text = f"{candidate.name} {candidate.area or ''} {' '.join(candidate.tags)}".lower()
        preferred_keywords = ["酒店", "饭店", "宾馆", "hotel", "inn", "hostel", "residence"]
        suspicious_keywords = ["故居", "景区", "公园", "博物馆", "宫", "坛", "庙", "堂", "院", "图书馆", "纪念馆", "湿地", "广场"]
        if self._is_strong_lodging_name(candidate.name):
            score -= 8
        elif any(keyword in text for keyword in preferred_keywords):
            score -= 4
        if "四星" in text or "五星" in text or "高档" in text or "舒适型" in text:
            score -= 3
        if "旅馆招待所" in text:
            score += 4
        if "民宿" in text:
            score += 3
        if any(keyword in text for keyword in suspicious_keywords) and not self._is_strong_lodging_name(candidate.name):
            score += 6
        rating = self._to_float(candidate.rating)
        if rating is not None:
            score -= int(rating * 2)  # 评分 4.5 → -9，高分优先
        star = self._to_int(candidate.star)
        if star is not None:
            score -= star  # 星级越高越优先
        for spot in lodging_input.spots:
            if spot.lower() in text:
                score -= 2
        if candidate.near_spot:
            score -= 3  # 来自景点附近搜索，位置优先
        for pref in lodging_input.preferences:
            if pref.lower() in text:
                score -= 1
        return score

    def _summary(self, candidates: list[LodgingCandidate], selected: SelectedLodging | None) -> str:
        if not candidates:
            return "暂无合适住宿候选。"
        if selected:
            return f"已筛出 {len(candidates)} 个住宿候选，优先推荐：{selected.name}。"
        return f"已筛出 {len(candidates)} 个住宿候选，优先推荐：{'、'.join(item.name for item in candidates[:3])}。"

    def _unique_queries(self, queries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        unique, seen = [], set()
        for keywords, city, query_type in queries:
            key = (str(keywords).strip(), str(city).strip())
            if key[0] and key not in seen:
                seen.add(key)
                unique.append((key[0], key[1], query_type))
        return unique

    def _deduplicate_candidates(self, candidates: list[LodgingCandidate]) -> list[LodgingCandidate]:
        seen: set[str] = set()
        unique: list[LodgingCandidate] = []
        for item in candidates:
            if item.name not in seen:
                seen.add(item.name)
                unique.append(item)
        return unique

    def _poi_to_candidate(self, lodging_input: LodgingInput, poi: dict, near_spot: bool = False) -> LodgingCandidate | None:
        name = str(poi.get("name") or "").strip()
        poi_type = str(poi.get("type") or "")
        if not name or self._is_noise_spot(name) or self._is_non_lodging_poi(name, poi_type):
            return None
        area = poi.get("business_area") or poi.get("adname") or poi.get("cityname") or poi.get("address") or lodging_input.destination
        return LodgingCandidate(
            poi_id=poi.get("poi_id"),
            name=name,
            area=str(area).strip() if area else lodging_input.destination,
            source="amap_poi",
            tags=[poi_type] if poi_type else [],
            near_spot=near_spot,
            price=poi.get("price"),
            rating=poi.get("rating"),
            star=poi.get("star"),
            lng=poi.get("lng"),
            lat=poi.get("lat"),
            address=poi.get("address"),
            tel=poi.get("tel"),
        )

    def _is_noise_spot(self, name: str) -> bool:
        return any(keyword in name for keyword in ["医院", "学校", "中学", "小学", "酒店式公寓", "派出所", "站", "机场", "公司", "超市"])

    def _is_non_lodging_poi(self, name: str, poi_type: str | None) -> bool:
        text = f"{name} {poi_type or ''}"
        return any(
            keyword in text
            for keyword in [
                "餐饮",
                "小吃",
                "火锅",
                "咖啡",
                "茶馆",
                "景点",
                "风景",
                "博物馆",
                "公园",
                "图书馆",
                "购物",
                "商场",
                "美食",
                "快餐",
                "面馆",
            ]
        )

    def _is_strong_lodging_name(self, name: str | None) -> bool:
        if not name:
            return False
        lowered = name.lower()
        strong_keywords = ["酒店", "饭店", "宾馆", "hotel", "inn", "hostel", "residence"]
        weak_keywords = ["客栈", "旅舍", "民宿"]
        reject_keywords = ["博物馆", "公园", "故居", "宫", "坛", "庙", "堂", "院", "图书馆", "纪念馆", "湿地", "广场", "长城", "胡同", "景区", "咖啡"]
        if any(keyword in lowered for keyword in strong_keywords):
            return not any(keyword in name for keyword in reject_keywords)
        if any(keyword in lowered for keyword in weak_keywords):
            return False
        return False

    def _filter_candidates(self, candidates: list[LodgingCandidate], lodging_input: LodgingInput) -> list[LodgingCandidate]:
        if not candidates:
            return []
        strong = []
        weak = []
        for candidate in self._deduplicate_candidates(candidates):
            if self._is_noise_spot(candidate.name) or self._is_non_lodging_poi(candidate.name, " ".join(candidate.tags)):
                continue
            if any(avoid and avoid.lower() in candidate.name.lower() for avoid in lodging_input.avoid_spots):
                continue  # 规避项命中名称即硬排除
            text = candidate.name.lower()
            hotel_like = self._is_strong_lodging_name(candidate.name) or any(keyword in text for keyword in ["酒店", "饭店", "宾馆", "客栈", "旅舍", "民宿", "hotel", "inn", "hostel", "residence"])
            suspicious = any(keyword in text for keyword in ["博物馆", "公园", "故居", "宫", "坛", "庙", "堂", "院", "图书馆", "纪念馆", "湿地", "广场", "胡同", "咖啡"]) and not self._is_strong_lodging_name(candidate.name)
            if suspicious:
                continue
            if self._is_strong_lodging_name(candidate.name):
                strong.append(candidate)
            elif hotel_like:
                weak.append(candidate)
        ranked = sorted(strong + weak, key=lambda item: (self._score_candidate(lodging_input, item), item.name))
        return ranked

    def _choose_selected_lodging(self, lodging_input: LodgingInput, candidates: list[LodgingCandidate]) -> SelectedLodging | None:
        if not candidates:
            return None
        strong_candidates = [item for item in candidates if self._is_strong_lodging_name(item.name)]
        pool = strong_candidates or candidates
        best = sorted(pool, key=lambda item: (self._score_candidate(lodging_input, item), item.name))[0]
        return SelectedLodging(
            poi_id=best.poi_id,
            name=best.name,
            area=best.area,
            source=best.source,
            booking_note="建议优先确认可取消房型，并在出行前 3-7 天完成预订。",
        )

    def _unique_strings(self, items: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)
        return unique

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_int(self, value: Any) -> int | None:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


lodging_tool = LodgingTool()
