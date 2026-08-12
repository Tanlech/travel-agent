from __future__ import annotations

import json
import time

from app.infrastructure.amap.client import amap_client
from app.infrastructure.llm.client import get_llm_client
from app.tools.prompt.attraction import ATTRACTION_AGENT_PROMPT
from app.tools.schema.attraction import AttractionCandidate, AttractionInput, AttractionResult


class AttractionTool:
    name = "attraction_tool"

    def run(self, input_data: AttractionInput) -> AttractionResult:
        input_data = AttractionInput(
            city=input_data.city,
            days=max(1, int(input_data.days or 1)),
            must_visit_spots=list(input_data.must_visit_spots or []),
            avoid_spots=list(input_data.avoid_spots or []),
            preferences=list(input_data.preferences or []),
            existing_candidates=list(input_data.existing_candidates or []),
            target_count=input_data.target_count,
            target_count_min=input_data.target_count_min,
            target_count_max=input_data.target_count_max,
        )
        self._last_queries: list[dict[str, str]] = []
        self._llm_metrics: list[dict[str, object]] = []

        verified = self._verify_spots(input_data)
        must_visit_verified = verified["must_visit_verified"]
        avoid_verified = verified["avoid_verified"]

        queries = self._build_queries(input_data)
        raw_candidates = self._search_candidates(input_data, avoid_verified, queries)
        cleaned_candidates = self._clean_candidates(input_data, raw_candidates, avoid_verified)
        llm_cleaned_candidates = self._llm_clean_candidates(input_data, must_visit_verified, cleaned_candidates, avoid_verified)
        enriched_candidates = self._enrich_candidates(input_data, must_visit_verified, llm_cleaned_candidates, avoid_verified)
        final_candidates = self._final_select(input_data, must_visit_verified, avoid_verified, enriched_candidates)

        return AttractionResult(
            city=input_data.city,
            candidates=final_candidates,
            must_visit_verified=must_visit_verified,
            avoid_verified=avoid_verified,
            source="amap_poi",
            raw={
                "must_visit_verified": [item.model_dump() for item in must_visit_verified],
                "avoid_verified": [item.model_dump() for item in avoid_verified],
                "queries": self._last_queries,
                "query_count": len(self._last_queries),
                "raw_candidate_count": len(raw_candidates),
                "clean_candidate_count": len(cleaned_candidates),
                "llm_clean_candidate_count": len(llm_cleaned_candidates),
                "enriched_candidate_count": len(enriched_candidates),
                "final_candidate_count": len(final_candidates),
                "llm_metrics": self._llm_metrics,
            },
        )

    def _verify_spots(self, input_data: AttractionInput) -> dict:
        return {
            "must_visit_verified": self._verify_spot_list(input_data.city, input_data.must_visit_spots),
            "avoid_verified": self._verify_spot_list(input_data.city, input_data.avoid_spots),
        }

    def _verify_spot_list(self, city: str, spots: list[str]) -> list[AttractionCandidate]:
        verified: list[AttractionCandidate] = []
        seen: set[str] = set()
        for spot in spots:
            name = str(spot).strip()
            if not name:
                continue
            raw = self._llm_json(
                "校验景点是否存在",
                f"城市: {city}\n景点: {name}\n请判断该景点是否在该城市真实存在，并尽量给出最稳定的正式景点名。只输出严格 JSON，格式包含 verified、matched_name、poi_id、area。",
            )
            parsed = self._parse_json(raw, {"verified": False, "matched_name": name, "poi_id": None, "area": city})
            matched_name = str(parsed.get("matched_name") or name).strip()
            normalized = self._normalize_name(matched_name)
            if not parsed.get("verified") or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            verified.append(
                AttractionCandidate(
                    poi_id=parsed.get("poi_id"),
                    name=matched_name,
                    area=str(parsed.get("area") or city).strip() if parsed.get("area") else city,
                    source="amap_poi",
                )
            )
        return verified

    def _build_queries(self, input_data: AttractionInput) -> list[tuple[str, str]]:
        reference_queries = self._reference_queries(input_data.city, input_data.preferences)
        raw = self._llm_json(
            "生成景点查询",
            (
                f"城市: {input_data.city}\n"
                f"用户偏好: {input_data.preferences}\n"
                f"参考 query 格式: {reference_queries}\n"
                "根据城市特点和用户偏好，参考 query 组成方式，生成适合地图搜索的关键词并组成 query。"
                "query 要优先覆盖城市的核心文化主线、自然主线、地标主线和补充主线，优先输出‘城市 + 主题关键词’格式。"
                "不要直接输出具体景点名称，不要输出解释，不要输出太细的子景区、附属设施或服务点名称。"
                "请输出 5~8 条 query，尽量避免重复或过于相近的表达。只输出严格 JSON，格式包含 queries。"
            ),
        )
        parsed = self._parse_json(raw, {"queries": []})
        queries = self._parse_queries(parsed.get("queries"), input_data.city)
        if len(queries) < 5:
            queries = self._dedupe_queries(queries + reference_queries)
        if not queries:
            queries = reference_queries
        return queries

    def _reference_queries(self, city: str, preferences: list[str]) -> list[tuple[str, str]]:
        pref_text = " ".join([str(item).strip() for item in preferences if str(item).strip()])
        pref_text = pref_text or "历史文化 自然景观 城市地标 博物馆"
        return self._dedupe_queries(
            [
                (f"{city} {pref_text}", city),
                (f"{city} 历史文化", city),
                (f"{city} 自然景观", city),
                (f"{city} 城市地标", city),
                (f"{city} 博物馆", city),
            ]
        )

    def _search_candidates(
        self,
        input_data: AttractionInput,
        avoid_verified: list[AttractionCandidate],
        query_plan: list[tuple[str, str]],
    ) -> list[AttractionCandidate]:
        seen: set[str] = {self._normalize_name(c.name) for c in input_data.existing_candidates if c.name}
        seen.update(self._normalize_name(c.name) for c in avoid_verified if c.name)
        results: list[AttractionCandidate] = []
        for keyword, city in query_plan:
            self._record_tool_call(keyword, city)
            for poi in self._safe_search(keyword, city):
                candidate = self._poi_to_candidate(city, poi)
                if candidate is None:
                    continue
                normalized = self._normalize_name(candidate.name)
                if not normalized or normalized in seen or self._is_noise_spot(candidate.name) or self._is_avoid_related(candidate, avoid_verified):
                    continue
                seen.add(normalized)
                results.append(candidate)
        return self._unique_candidates(results)

    def _clean_candidates(
        self,
        input_data: AttractionInput,
        raw_candidates: list[AttractionCandidate],
        avoid_verified: list[AttractionCandidate],
    ) -> list[AttractionCandidate]:
        avoid_names = {self._normalize_name(item.name) for item in avoid_verified if item.name}
        existing_names = {self._normalize_name(item.name) for item in input_data.existing_candidates if item.name}
        cleaned: list[AttractionCandidate] = []
        for candidate in raw_candidates:
            normalized = self._normalize_name(candidate.name)
            if not normalized or normalized in avoid_names or normalized in existing_names:
                continue
            if self._is_micro_noise(candidate.name):
                continue
            cleaned.append(candidate)
        return self._unique_candidates(cleaned)

    def _llm_clean_candidates(
        self,
        input_data: AttractionInput,
        must_visit_candidates: list[AttractionCandidate],
        candidates: list[AttractionCandidate],
        avoid_verified: list[AttractionCandidate],
    ) -> list[AttractionCandidate]:
        pool = self._unique_candidates(input_data.existing_candidates + must_visit_candidates + candidates)
        if not pool:
            return []
        candidate_text = "\n".join(f"- {c.name} | area={c.area} | entity_level={c.entity_level or 'unknown'}" for c in pool[:60])
        raw = self._llm_json(
            "清洗景点候选",
            (
                f"城市: {input_data.city}\n"
                f"用户偏好: {input_data.preferences}\n"
                f"必去已命中: {[c.name for c in must_visit_candidates]}\n"
                f"不去已命中: {[c.name for c in avoid_verified]}\n"
                f"候选列表:\n{candidate_text}\n"
                "请清洗候选池，剔除明显是同一景点的子项、馆内单元、展示楼、珍品馆、分馆、附属楼、附属展厅等。"
                "如果候选已经有主景点，则优先保留主景点，删除其明显子项；如果没有主景点，可最多保留一个最具代表性的子项。"
                "对主景区结构明显的城市，要优先保留主景区、主场馆和核心主体；子景区、分段、附属单元应视为从属项。"
                "不要删除真正独立的景点。只输出严格 JSON，格式包含 keep_names。"
            ),
        )
        parsed = self._parse_json(raw, {"keep_names": []})
        keep_names = {self._normalize_name(name) for name in parsed.get("keep_names", [])}
        if not keep_names:
            return pool
        selected: list[AttractionCandidate] = []
        for candidate in pool:
            normalized = self._normalize_name(candidate.name)
            if normalized in keep_names:
                selected.append(candidate)
        if not selected:
            return pool
        return self._unique_candidates(selected)

    def _enrich_candidates(
        self,
        input_data: AttractionInput,
        must_visit_candidates: list[AttractionCandidate],
        candidates: list[AttractionCandidate],
        avoid_verified: list[AttractionCandidate],
    ) -> list[AttractionCandidate]:
        pool = self._unique_candidates(input_data.existing_candidates + must_visit_candidates + candidates)
        if not pool:
            return []
        candidate_text = "\n".join(f"- {c.name} | area={c.area} | entity_level={c.entity_level or 'unknown'}" for c in pool[:40])
        context = (
            f"城市: {input_data.city}\n"
            f"天数: {input_data.days}\n"
            f"用户偏好: {input_data.preferences}\n"
            f"必去已命中: {[c.name for c in must_visit_candidates]}\n"
            f"不去已命中: {[c.name for c in avoid_verified]}\n"
            f"已有候选: {[c.name for c in input_data.existing_candidates]}\n"
            f"候选列表:\n{candidate_text}\n"
            "请为候选池中的景点补充 reason 和 estimated_visit_duration_hours。"
            "不要过早大量筛掉候选，应尽量保留更多对上游编排行程有价值的高质量候选。"
            "reason 必须是结构化说明，至少明确包含：是否主景点/子景点、核心看点、是否属于某个主景区或主场馆、以及为什么适合或不适合单独保留。"
            "不要写成泛泛而谈的宣传文案。"
            "必去景点必须保留；不去景点必须排除。只输出严格 JSON，格式包含 reasons、durations、entity_levels。"
        )
        parsed = self._parse_json(self._llm_json("补充景点信息", context), {"reasons": {}, "durations": {}, "entity_levels": {}})
        reason_map = self._normalize_text_map(parsed.get("reasons", {}))
        duration_map = self._normalize_duration_map(parsed.get("durations", {}))
        entity_level_map = self._normalize_entity_level_map(parsed.get("entity_levels", {}))
        avoid_names = {self._normalize_name(item.name) for item in avoid_verified if item.name}

        enriched: list[AttractionCandidate] = []
        for candidate in pool:
            normalized = self._normalize_name(candidate.name)
            if normalized in avoid_names:
                continue
            enriched.append(
                candidate.model_copy(
                    update={
                        "reason": reason_map.get(normalized) or self._fallback_reason(candidate),
                        "estimated_visit_duration_hours": duration_map.get(normalized) or self._fallback_duration(candidate),
                        "entity_level": entity_level_map.get(normalized) or candidate.entity_level or "independent",
                    }
                )
            )
        return self._unique_candidates(enriched)

    def _final_select(
        self,
        input_data: AttractionInput,
        must_visit_candidates: list[AttractionCandidate],
        avoid_verified: list[AttractionCandidate],
        candidates: list[AttractionCandidate],
    ) -> list[AttractionCandidate]:
        if not candidates:
            return must_visit_candidates

        must_names = {self._normalize_name(item.name) for item in must_visit_candidates if item.name}
        avoid_names = {self._normalize_name(item.name) for item in avoid_verified if item.name}
        candidate_text = "\n".join(
            f"- {c.name} | area={c.area} | entity_level={c.entity_level or 'unknown'} | duration={c.estimated_visit_duration_hours} | reason={c.reason}"
            for c in candidates[:40]
        )
        raw = self._llm_json(
            "最终景点推荐",
            (
                f"城市: {input_data.city}\n"
                f"天数: {input_data.days}\n"
                f"用户偏好: {input_data.preferences}\n"
                f"目标数量范围: {input_data.target_count_min or input_data.target_count or 8}-{input_data.target_count_max or input_data.target_count or 12}\n"
                f"必去已命中: {[c.name for c in must_visit_candidates]}\n"
                f"不去已命中: {[c.name for c in avoid_verified]}\n"
                f"已有候选: {[c.name for c in input_data.existing_candidates]}\n"
                f"候选列表:\n{candidate_text}\n"
                "你是城市主景点裁判，而不是高质量推荐器。"
                "你的任务不是尽量多保留，而是从候选池中只挑出最能代表该城市的主景点。"
                "请先筛出城市最具代表性的核心景点；如果数量不足，则继续补充能形成最小完整城市叙事的次代表性景点，优先补到目标下限（例如 8 个）；如果数量过多，再舍弃非核心项。"
                "请从候选中严格选出落在目标数量范围内的城市代表性旅游地标。"
                "优先保留最能代表该城市的核心独立景点和主景区；entity_level=main 与 independent 优先，entity_level=sub 只有在极少数情况下才可保留。"
                "主景区内部的寺庙、寺院、亭台、栈道、观景台、步道、子馆、分段、附属单元、附属展厅、微景点，除非它们本身就是城市级代表点，否则不要保留。"
                "主题乐园、文旅综合体、夜游地标、亲子娱乐核心如果是城市代表性旅游地标，可以按主景点处理，但不要把普通商业设施、局部娱乐点或附属片区当作主景点。"
                "不要因为有名就保留，也不要因为和主景区相关就保留。"
                "对主景区结构明显的城市，要把子景区、分段、附属单元、观景点、微景点视为从属项，除非它们本身就是城市最具代表性的核心点，否则不要保留。"
                "不要保留明显只是主景区一部分的子景区、分段、附属展厅、馆内单元、体验型碎片点、城市背景对象或不具代表性的补充点。"
                "如果已有候选中已经有某些景点，新推荐应尽量补充而不是重复相同价值的候选。"
                "必去景点必须保留；不去景点必须排除；最终结果必须尽量落在目标数量范围内。只输出严格 JSON，格式包含 selected_names。"
            ),
        )
        parsed = self._parse_json(raw, {"selected_names": []})
        selected_names = {self._normalize_name(name) for name in parsed.get("selected_names", [])}
        if not selected_names:
            selected_names = must_names

        target_min = input_data.target_count_min or input_data.target_count or 8
        target_max = input_data.target_count_max or input_data.target_count or 12
        if target_min > target_max:
            target_min, target_max = target_max, target_min

        selected: list[AttractionCandidate] = []
        selected_norms: set[str] = set()
        for candidate in candidates:
            normalized = self._normalize_name(candidate.name)
            if normalized in avoid_names:
                continue
            if normalized in must_names or normalized in selected_names:
                selected.append(candidate)
                selected_norms.add(normalized)

        for candidate in must_visit_candidates:
            normalized = self._normalize_name(candidate.name)
            if normalized not in selected_norms and normalized not in avoid_names:
                selected.insert(
                    0,
                    candidate.model_copy(
                        update={
                            "reason": self._fallback_reason(candidate),
                            "estimated_visit_duration_hours": self._fallback_duration(candidate),
                        }
                    ),
                )
                selected_norms.add(normalized)

        if len(selected) < target_min:
            for candidate in candidates:
                normalized = self._normalize_name(candidate.name)
                if normalized in selected_norms or normalized in avoid_names:
                    continue
                selected.append(candidate)
                selected_norms.add(normalized)
                if len(selected) >= target_min:
                    break

        if len(selected) > target_max:
            selected = selected[:target_max]
        return self._unique_candidates(selected)

    def _parse_queries(self, queries: object, default_city: str) -> list[tuple[str, str]]:
        if not isinstance(queries, list):
            return []
        parsed: list[tuple[str, str]] = []
        for item in queries:
            if isinstance(item, dict):
                keywords = str(item.get("keywords") or "").strip()
                city = str(item.get("city") or default_city).strip() or default_city
                if keywords:
                    parsed.append((keywords, city))
        return self._dedupe_queries(parsed)

    def _parse_json(self, raw: str, default: dict) -> dict:
        try:
            data = json.loads(raw) if raw else default
        except Exception:
            return default
        return data if isinstance(data, dict) else default

    def _normalize_text_map(self, values: object) -> dict[str, str]:
        if not isinstance(values, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, value in values.items():
            key_text = self._normalize_name(key)
            value_text = str(value).strip()
            if key_text and value_text:
                normalized[key_text] = value_text
        return normalized

    def _normalize_duration_map(self, durations: object) -> dict[str, float]:
        if not isinstance(durations, dict):
            return {}
        normalized: dict[str, float] = {}
        for key, value in durations.items():
            key_text = self._normalize_name(key)
            try:
                duration = float(value)
            except Exception:
                continue
            if key_text:
                normalized[key_text] = duration
        return normalized

    def _normalize_entity_level_map(self, values: object) -> dict[str, str]:
        if not isinstance(values, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, value in values.items():
            key_text = self._normalize_name(key)
            level = str(value).strip().lower()
            if key_text and level in {"main", "sub", "independent"}:
                normalized[key_text] = level
        return normalized

    def _dedupe_queries(self, queries: list[tuple[str, str]]) -> list[tuple[str, str]]:
        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for keyword, city in queries:
            item = (str(keyword).strip(), str(city).strip())
            if not item[0] or item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    def _record_tool_call(self, keyword: str, city: str) -> None:
        city = str(city).strip()
        self._last_queries.append({"city": city, "keywords": str(keyword).strip()})

    def _safe_search(self, keyword: str, city: str) -> list[dict]:
        try:
            return amap_client.search_pois(keywords=keyword, city=city) or []
        except Exception:
            return []

    def _poi_to_candidate(self, city: str, poi: dict) -> AttractionCandidate | None:
        name = str(poi.get("name") or "").strip()
        if not name:
            return None
        area = poi.get("adname") or poi.get("cityname") or poi.get("address") or city
        return AttractionCandidate(poi_id=poi.get("poi_id"), name=name, area=str(area).strip() if area else city, source="amap_poi")

    def _normalize_name(self, value: str) -> str:
        return "".join(ch for ch in str(value).lower().strip() if not ch.isspace() and ch not in {"-", "_", "（", "）", "(", ")", "·", "/", "，", ","})

    def _is_micro_noise(self, name: str) -> bool:
        return any(keyword in str(name).strip() for keyword in ["票处", "停车场", "停车点", "索道", "游客中心", "服务中心", "入口", "出口", "检票口", "办公区", "办公点", "观景台", "观景点", "乘车处", "导览图", "社区", "超市", "特产", "客栈", "酒店", "山庄", "中学", "政府", "门票站", "候车处", "休息长廊"])

    def _fallback_reason(self, candidate: AttractionCandidate) -> str:
        area = f"，位于{candidate.area}" if candidate.area else ""
        return f"主流景点{area}，适合纳入本次行程。"

    def _fallback_duration(self, candidate: AttractionCandidate) -> float:
        text = self._normalize_name(candidate.name)
        if any(keyword in text for keyword in ["古镇", "街区", "景区", "博物馆", "遗址", "公园"]):
            return 2.5
        return 2.0

    def _is_avoid_related(self, candidate: AttractionCandidate, avoid_verified: list[AttractionCandidate]) -> bool:
        candidate_text = self._normalize_name(candidate.name)
        area_text = self._normalize_name(candidate.area or "")
        for item in avoid_verified:
            avoid_text = self._normalize_name(item.name)
            if not avoid_text:
                continue
            if avoid_text in candidate_text or candidate_text in avoid_text:
                return True
            if area_text and self._normalize_name(item.area or "") and area_text == self._normalize_name(item.area or ""):
                if any(keyword in avoid_text for keyword in ["草堂", "武侯", "锦里", "宽窄", "熊猫", "都江堰", "青城", "金沙"]):
                    return True
        return False

    def _is_noise_spot(self, name: str) -> bool:
        return any(keyword in name for keyword in ["票处", "停车场", "停车点", "索道", "游客中心", "服务中心", "入口", "出口", "检票口", "办公区", "办公点", "观景台", "观景点", "乘车处", "导览图", "社区", "超市", "特产", "客栈", "酒店", "山庄", "中学", "政府", "门票站", "候车处", "休息长廊"])

    def _unique_candidates(self, candidates: list[AttractionCandidate]) -> list[AttractionCandidate]:
        seen: set[str] = set()
        unique: list[AttractionCandidate] = []
        for candidate in candidates:
            normalized = self._normalize_name(candidate.name)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(candidate)
        return unique

    def _llm_json(self, system_label: str, context: str) -> str:
        client = get_llm_client()
        if not client.is_enabled():
            return ""
        messages = [
            {"role": "system", "content": ATTRACTION_AGENT_PROMPT + "\n\n只输出 JSON，不要输出解释。"},
            {"role": "user", "content": context},
        ]
        started_at = time.perf_counter()
        try:
            response = client.client.chat.completions.create(
                model=client.model,
                temperature=client.temperature,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except Exception:
            self._llm_metrics.append({"step": system_label, "latency_ms": round((time.perf_counter() - started_at) * 1000, 1), "success": False})
            return ""
        latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
        self._llm_metrics.append({"step": system_label, "latency_ms": latency_ms, "success": bool(response.choices)})
        if not response.choices:
            return ""
        return (response.choices[0].message.content or "").strip()


attraction_tool = AttractionTool()
