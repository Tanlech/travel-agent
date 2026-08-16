from __future__ import annotations

import json
from typing import Any

from app.infrastructure.amap_client import amap_client
from app.infrastructure.llm_client import get_llm_client
from app.tools.prompt.lodging import LODGING_TOOL_PROMPT
from app.tools.schema.lodging import LodgingCandidate, LodgingInput, LodgingResult, SelectedLodging


class LodgingTool:
    name = "lodging_tool"
    _MAX_QUERIES = 5
    _MAX_CANDIDATES_PER_QUERY = 10

    def run(self, lodging_input: LodgingInput, transport=None) -> LodgingResult:
        lodging_input = self._merge_transport_spots(lodging_input, transport)
        self._last_queries: list[dict[str, str]] = []
        self._last_tool_calls: list[dict[str, str]] = []

        candidates = self._search_candidates(lodging_input, self._queries(lodging_input))
        selected = self._select_candidates(lodging_input, candidates)
        if len(selected) < 3:
            selected = self._select_candidates(lodging_input, self._deduplicate_candidates(candidates + self._search_candidates(lodging_input, self._queries(lodging_input, relaxed=True))))

        filtered = self._filter_candidates(selected or candidates, lodging_input)
        chosen = self._choose_selected_lodging(lodging_input, filtered)
        final_candidates = filtered[:3] if filtered else selected[:3]

        return LodgingResult(
            city=lodging_input.destination,
            candidates=final_candidates,
            selected_lodging=chosen,
            summary=self._summary(final_candidates, chosen),
            source="amap_poi",
            raw={"query_count": len(self._last_queries), "queries": self._last_queries, "tool_calls": self._last_tool_calls, "raw_candidate_count": len(candidates)},
        )

    def _merge_transport_spots(self, lodging_input: LodgingInput, transport: Any) -> LodgingInput:
        if transport and getattr(transport, "daily_reports", None):
            spots = list(lodging_input.spots)
            for report in transport.daily_reports[:3]:
                if getattr(report, "attractions", None):
                    spots.extend(str(item).strip() for item in report.attractions if str(item).strip())
            lodging_input.spots = self._unique_strings(spots)
        return lodging_input

    def _queries(self, lodging_input: LodgingInput, relaxed: bool = False) -> list[tuple[str, str]]:
        payload = self._llm_json(lodging_input, self._query_prompt(lodging_input, relaxed=relaxed))
        queries = self._parse_queries(payload, lodging_input.destination)
        hotel_focused = self._hotel_focused_queries(lodging_input, relaxed=relaxed)
        return self._unique_queries((queries or []) + hotel_focused)

    def _select_candidates(self, lodging_input: LodgingInput, raw_candidates: list[LodgingCandidate]) -> list[LodgingCandidate]:
        if not raw_candidates:
            return []
        payload = self._llm_json(lodging_input, self._selection_prompt(lodging_input, raw_candidates[:30]))
        selected_names = self._parse_selection(payload)
        if not selected_names:
            return self._fallback_select(lodging_input, raw_candidates)
        selected = [item for item in raw_candidates if item.name in set(selected_names)]
        return selected or self._fallback_select(lodging_input, raw_candidates)

    def _llm_json(self, lodging_input: LodgingInput, user_prompt: str) -> str:
        client = get_llm_client()
        if not client.is_enabled():
            return ""
        messages = [
            {"role": "system", "content": LODGING_TOOL_PROMPT + "\n\n只输出 JSON，不要输出解释。"},
            {"role": "user", "content": self._context(lodging_input) + "\n\n" + user_prompt},
        ]
        try:
            response = client.client.chat.completions.create(
                model=client.model,
                temperature=client.temperature,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except Exception:
            return ""
        if not response.choices:
            return ""
        return (response.choices[0].message.content or "").strip()

    def _context(self, lodging_input: LodgingInput) -> str:
        return "\n".join([
            f"城市: {lodging_input.destination}",
            f"总预算: {lodging_input.budget}",
            f"用户偏好: {lodging_input.preferences}",
            f"规避项: {lodging_input.avoid_spots}",
            f"景点: {lodging_input.spots}",
        ])

    def _query_prompt(self, lodging_input: LodgingInput, *, relaxed: bool = False) -> str:
        if relaxed:
            return '请放宽条件生成酒店查询，只输出严格 JSON: {"queries": [{"keywords": "...", "city": "..."}]}'
        return '请生成酒店查询，尽量围绕用户偏好和景点分布组织查询，只输出严格 JSON: {"queries": [{"keywords": "...", "city": "..."}]}'

    def _selection_prompt(self, lodging_input: LodgingInput, candidates: list[LodgingCandidate]) -> str:
        candidate_text = "\n".join(self._format_candidate(item) for item in candidates)
        return (
            f"候选住宿:\n{candidate_text}\n"
            "请从候选中选出最适合用户的酒店名称列表，只输出严格 JSON: {\"selected_names\": [\"酒店1\", \"酒店2\"]}"
        )

    def _search_candidates(self, lodging_input: LodgingInput, queries: list[tuple[str, str]]) -> list[LodgingCandidate]:
        if not amap_client.is_enabled():
            return []
        seen: set[str] = set()
        candidates: list[LodgingCandidate] = []
        for keywords, city in queries[: self._MAX_QUERIES]:
            self._last_queries.append({"city": city, "keywords": keywords})
            self._last_tool_calls.append({"tool": "amap_maps_text_search", "city": city, "keywords": keywords})
            try:
                pois = amap_client.search_pois(keywords=keywords, city=city) or []
            except Exception:
                pois = []
            for poi in pois[: self._MAX_CANDIDATES_PER_QUERY]:
                candidate = self._poi_to_candidate(lodging_input, poi)
                if candidate is None or candidate.name in seen:
                    continue
                seen.add(candidate.name)
                candidates.append(candidate)
        return candidates

    def _fallback_queries(self, lodging_input: LodgingInput, *, relaxed: bool = False) -> list[tuple[str, str]]:
        return self._hotel_focused_queries(lodging_input, relaxed=relaxed)

    def _hotel_focused_queries(self, lodging_input: LodgingInput, *, relaxed: bool = False) -> list[tuple[str, str]]:
        area_hints = self._derive_area_hints(lodging_input)
        base = [
            f"{lodging_input.destination} 酒店",
            f"{lodging_input.destination} 连锁酒店",
            f"{lodging_input.destination} 舒适型酒店",
        ]
        for area in area_hints[:3]:
            base.extend(
                [
                    f"{lodging_input.destination} {area} 酒店",
                    f"{lodging_input.destination} {area} 连锁酒店",
                    f"{lodging_input.destination} {area} 舒适型酒店",
                    f"{lodging_input.destination} {area} 四星酒店",
                ]
            )
        for spot in lodging_input.spots[:2]:
            base.append(f"{lodging_input.destination} {spot} 附近酒店")
        if relaxed:
            base.extend([f"{lodging_input.destination} 精品酒店", f"{lodging_input.destination} 商务酒店"])
        return self._unique_queries([(item, lodging_input.destination) for item in base])

    def _fallback_select(self, lodging_input: LodgingInput, raw_candidates: list[LodgingCandidate]) -> list[LodgingCandidate]:
        ranked = sorted(raw_candidates, key=lambda item: (self._score_candidate(lodging_input, item), item.name))
        strong_only = [item for item in ranked if self._is_strong_lodging_name(item.name)]
        return (strong_only or ranked)[:3]

    def _score_candidate(self, lodging_input: LodgingInput, candidate: LodgingCandidate) -> int:
        score = 0
        text = f"{candidate.name} {candidate.area or ''} {' '.join(candidate.tags)}".lower()
        preferred_keywords = ["酒店", "宾馆", "hotel", "inn", "hostel", "residence"]
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
        for spot in lodging_input.spots:
            if spot.lower() in text:
                score -= 2
        for pref in lodging_input.preferences:
            if pref.lower() in text:
                score -= 1
        for avoid in lodging_input.avoid_spots:
            if avoid.lower() in text:
                score += 2
        return score

    def _summary(self, candidates: list[LodgingCandidate], selected: SelectedLodging | None) -> str:
        if not candidates:
            return "暂无合适住宿候选。"
        if selected:
            return f"已筛出 {len(candidates)} 个住宿候选，优先推荐：{selected.name}。"
        return f"已筛出 {len(candidates)} 个住宿候选，优先推荐：{'、'.join(item.name for item in candidates[:3])}。"

    def _parse_queries(self, raw: str, default_city: str) -> list[tuple[str, str]]:
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            return []
        queries = data.get("queries") if isinstance(data, dict) else []
        if not isinstance(queries, list):
            return []
        parsed = []
        for item in queries:
            if isinstance(item, dict):
                keywords = str(item.get("keywords") or "").strip()
                city = str(item.get("city") or default_city).strip() or default_city
                if keywords:
                    parsed.append((keywords, city))
        return self._unique_queries(parsed)

    def _parse_selection(self, raw: str) -> list[str]:
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            return []
        selected = data.get("selected_names") if isinstance(data, dict) else []
        if not isinstance(selected, list):
            return []
        return [str(name).strip() for name in selected if str(name).strip()]

    def _unique_queries(self, queries: list[tuple[str, str]]) -> list[tuple[str, str]]:
        unique, seen = [], set()
        for keywords, city in queries:
            item = (str(keywords).strip(), str(city).strip())
            if item[0] and item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _deduplicate_candidates(self, candidates: list[LodgingCandidate]) -> list[LodgingCandidate]:
        seen: set[str] = set()
        unique: list[LodgingCandidate] = []
        for item in candidates:
            if item.name not in seen:
                seen.add(item.name)
                unique.append(item)
        return unique

    def _poi_to_candidate(self, lodging_input: LodgingInput, poi: dict) -> LodgingCandidate | None:
        name = str(poi.get("name") or "").strip()
        poi_type = str(poi.get("type") or "")
        if not name or self._is_noise_spot(name) or self._is_non_lodging_poi(name, poi_type):
            return None
        area = poi.get("adname") or poi.get("cityname") or poi.get("address") or lodging_input.destination
        return LodgingCandidate(
            poi_id=poi.get("poi_id"),
            name=name,
            area=str(area).strip() if area else lodging_input.destination,
            source="amap_poi",
            tags=[poi_type] if poi_type else [],
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
                "烤鸭",
                "锅贴",
                "爆肚",
            ]
        )

    def _derive_area_hints(self, lodging_input: LodgingInput) -> list[str]:
        area_map = {
            "故宫博物院": "东城区",
            "中国国家博物馆": "东城区",
            "天安门广场": "东城区",
            "国子监": "东城区",
            "首都博物馆": "西城区",
            "北京古代建筑博物馆": "西城区",
            "烟袋斜街": "西城区",
            "团城": "西城区",
            "颐和园": "海淀区",
            "圆明园遗址公园": "海淀区",
            "八达岭长城": "延庆区",
            "地坛公园": "东城区",
        }
        hints: list[str] = []
        for spot in lodging_input.spots:
            area = area_map.get(spot)
            if area and area not in hints:
                hints.append(area)
        return hints

    def _is_strong_lodging_name(self, name: str | None) -> bool:
        if not name:
            return False
        lowered = name.lower()
        strong_keywords = ["酒店", "宾馆", "hotel", "inn", "hostel", "residence"]
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
            text = candidate.name.lower()
            hotel_like = self._is_strong_lodging_name(candidate.name) or any(keyword in text for keyword in ["酒店", "宾馆", "客栈", "旅舍", "民宿", "hotel", "inn", "hostel", "residence"])
            suspicious = any(keyword in text for keyword in ["博物馆", "公园", "故居", "宫", "坛", "庙", "堂", "院", "图书馆", "纪念馆", "湿地", "广场", "胡同", "咖啡", "锅贴", "爆肚"]) and not self._is_strong_lodging_name(candidate.name)
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
        if not strong_candidates:
            return None
        best = sorted(strong_candidates, key=lambda item: (self._score_candidate(lodging_input, item), item.name))[0]
        return SelectedLodging(
            poi_id=best.poi_id,
            name=best.name,
            area=best.area,
            source=best.source,
            booking_note="建议优先确认可取消房型，并在出行前 3-7 天完成预订。",
        )

    def _format_candidate(self, candidate: LodgingCandidate) -> str:
        area = f" | area={candidate.area}" if candidate.area else ""
        return f"- {candidate.name}{area}"

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
