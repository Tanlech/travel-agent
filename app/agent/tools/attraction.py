from __future__ import annotations

from app.infrastructure.amap_client import amap_client
from app.infrastructure.conversions import safe_float
from app.infrastructure.settings import settings
from app.agent.knowledge import ATTRACTION_COLLECTION, knowledge_service
from app.agent.tools.prompt.attraction import (
    ATTRACTION_PERSIST_SYSTEM_PROMPT,
    ATTRACTION_SELECT_SYSTEM_PROMPT,
    build_attraction_persist_user_prompt,
    build_attraction_select_user_prompt,
)
from app.agent.tools.schema.attraction import (
    AttractionCandidate,
    AttractionInput,
    AttractionResult,
    SpotKbEntry,
    SpotSelection,
)

# LLM 不可用时的主要景点降级判断：名称或 POI type 命中这些地标特征词即视为主要景点
_LANDMARK_KEYWORDS = [
    "风景名胜", "景区", "风景", "森林公园", "国家公园", "湿地公园", "公园广场", "公园",
    "博物馆", "纪念馆", "展览馆", "美术馆", "图书馆", "科技馆", "海洋馆", "水族馆",
    "故居", "遗址", "古镇", "古城", "古村", "历史街区", "老街", "老城",
    "寺庙", "寺院", "道观", "教堂", "佛塔", "古塔", "陵墓", "陵园", "纪念碑",
    "故宫", "宫殿", "宫城", "园林", "皇家", "御苑",
    "瀑布", "温泉", "峡谷", "雪山", "草原", "湖泊", "湖畔", "海岛", "岛屿",
    "主题乐园", "游乐场", "游乐", "冰雪", "动物园", "植物园",
]


class AttractionTool:
    """召回某城市代表性景点候选（知识库为主，高德搜索补充）"""
    name = "attraction_tool"

    def run(self, input_data: AttractionInput) -> AttractionResult:
        input_data = self._normalize_input(input_data)
        # 知识库为主：命中城市返回知识库结果；未命中走搜索兜底
        kb_result = self._run_from_kb(input_data)
        if kb_result is not None:
            return kb_result
        return self._run_from_search(input_data)

    def _normalize_input(self, input_data: AttractionInput) -> AttractionInput:
        """输入归一化：天数下限为 1，各列表去空，字段语义不变"""
        return AttractionInput(
            city=input_data.city,
            days=max(1, int(input_data.days or 1)),
            must_visit_spots=[str(s).strip() for s in (input_data.must_visit_spots or []) if str(s).strip()],
            avoid_spots=[str(s).strip() for s in (input_data.avoid_spots or []) if str(s).strip()],
            preferences=[str(p).strip() for p in (input_data.preferences or []) if str(p).strip()],
            existing_candidates=list(input_data.existing_candidates or []),
            target_count=input_data.target_count,
            target_count_min=input_data.target_count_min,
            target_count_max=input_data.target_count_max,
        )

    # ========================= 路径①：知识库为主 =========================
    def _run_from_kb(self, input_data: AttractionInput) -> AttractionResult | None:
        """城市已入库：RAG 召回 + 硬过滤（must 三级兜底 / avoid 排除 / 噪声 / 现有 / 数量上限）；
        非必去名额的排序与择优交给 LLM，失败降级按库序截断"""
        items = knowledge_service.get_all(ATTRACTION_COLLECTION, where={"city": input_data.city})
        if not items:
            return None

        spots: list[AttractionCandidate] = []
        for item in items:
            meta = item.metadata
            name = str(meta.get("name") or "").strip()
            if not name or self._is_noise_spot(name):
                continue
            area = str(meta.get("area") or "").strip() or input_data.city
            spots.append(
                AttractionCandidate(
                    name=name,
                    area=area,
                    estimated_visit_duration_hours=safe_float(meta.get("duration")),
                    reason=str(meta.get("reason") or "").strip() or self._fallback_reason(name, area),
                )
            )
        if not spots:
            return None

        # ---- 必去景点：三级兜底（库命中 → 搜索+沉淀 → 原始名保留） ----
        existing_names = {self._normalize_name(c.name) for c in input_data.existing_candidates if c.name}
        must_verified: list[AttractionCandidate] = []
        searched_count = 0
        searched_failed = 0
        for raw in input_data.must_visit_spots:
            # 已在旧候选（改稿增量）中的必去景点，直接跳过，避免无谓的搜索+LLM 沉淀
            if self._normalize_name(raw) in existing_names:
                continue
            matched = next((c for c in spots if self._kb_name_match(raw, c.name)), None)
            if matched:
                must_verified.append(matched)
                continue
            found = self._search_and_persist(raw, input_data.city)
            if found is not None:
                searched_count += 1
                if self._normalize_name(found.name) not in existing_names:
                    must_verified.append(found)
            else:
                searched_failed += 1
                must_verified.append(AttractionCandidate(name=raw, area=input_data.city))

        # ---- 不去景点：知识库匹配名 + 原始名双保险排除 ----
        avoid_verified: list[AttractionCandidate] = []
        avoid_names: set[str] = set()
        for raw in input_data.avoid_spots:
            matched = next((c for c in spots if self._kb_name_match(raw, c.name)), None)
            avoid_verified.append(matched or AttractionCandidate(name=raw, area=input_data.city))
            avoid_names.add(self._normalize_name(matched.name if matched else raw))

        # ---- 其余候选：剔除 must/avoid/existing ----
        must_names = {self._normalize_name(c.name) for c in must_verified}
        selected: list[AttractionCandidate] = []
        for c in spots:
            normalized = self._normalize_name(c.name)
            if normalized in must_names or normalized in avoid_names or normalized in existing_names:
                continue
            selected.append(c)
        selected = self._unique_candidates(must_verified + selected)

        # ---- 数量与排序：必去景点恒保留；剩余名额的排序/择优整体交给 LLM（失败降级按库顺序截断） ----
        must_list = [c for c in selected if self._normalize_name(c.name) in must_names]
        pool = [c for c in selected if self._normalize_name(c.name) not in must_names]
        target_max = input_data.target_count_max or input_data.target_count or 12
        slots = min(len(pool), max(0, target_max - len(must_list)))
        selected = must_list + self._select_recommended(
            city=input_data.city,
            days=input_data.days,
            preferences=input_data.preferences or [],
            pool=pool,
            slots=slots,
        )

        return AttractionResult(
            city=input_data.city,
            candidates=selected,
            must_visit_verified=must_verified,
            avoid_verified=avoid_verified,
            raw={
                "kb_hit": True,
                "kb_count": len(items),
                "kb_collection": ATTRACTION_COLLECTION,
                "must_visit_searched": searched_count,
                "must_visit_search_failed": searched_failed,
            },
        )

    # =================== 路径②：城市未入库（搜索兜底） ===================
    def _run_from_search(self, input_data: AttractionInput) -> AttractionResult:
        """城市未入库：不铺开主题召回，只对用户想去/必去的景点逐个搜索；无必去则返回空并提示"""
        if not amap_client.is_enabled():
            return AttractionResult(
                city=input_data.city,
                error="高德地图未配置（AMAP_KEY）",
                raw={"kb_hit": False, "must_visit_searched": 0, "must_visit_search_failed": 0},
            )
        if not input_data.must_visit_spots:
            return AttractionResult(
                city=input_data.city,
                error="城市未入库且未提供想去的景点，无法推荐",
                raw={"kb_hit": False, "must_visit_searched": 0, "must_visit_search_failed": 0},
            )

        searched: list[AttractionCandidate] = []
        seen: set[str] = set()
        existing_names = {self._normalize_name(c.name) for c in input_data.existing_candidates if c.name}
        searched_count = 0
        searched_failed = 0
        for raw in input_data.must_visit_spots:
            # 已在旧候选（改稿增量）中的必去景点，直接跳过，避免重复搜索+推荐
            if self._normalize_name(raw) in existing_names:
                continue
            found = self._search_and_persist(raw, input_data.city)
            if found is not None:
                searched_count += 1
            else:
                searched_failed += 1
                found = AttractionCandidate(name=raw, area=input_data.city)
            normalized = self._normalize_name(found.name)
            if normalized in seen:
                continue
            seen.add(normalized)
            searched.append(found)

        return AttractionResult(
            city=input_data.city,
            candidates=searched,
            must_visit_verified=searched,
            avoid_verified=[
                AttractionCandidate(name=raw, area=input_data.city) for raw in input_data.avoid_spots if raw
            ],
            raw={"kb_hit": False, "must_visit_searched": searched_count, "must_visit_search_failed": searched_failed},
        )

    # ===================== 搜索 + 沉淀闭环 =====================
    def _search_and_persist(self, raw_name: str, city: str) -> AttractionCandidate | None:
        """用用户原始名搜索高德，取名字相关的非噪声 POI 作为该必去景点的正式候选"""
        if not amap_client.is_enabled():
            return None
        best: AttractionCandidate | None = None
        best_poi: dict | None = None
        for poi in self._safe_search(raw_name, city):
            candidate = self._poi_to_candidate(city, poi)
            if candidate is None or self._is_noise_spot(candidate.name):
                continue
            if best is None:
                best, best_poi = candidate, poi
            if self._kb_name_match(raw_name, candidate.name):  # 名称强相关即为命中
                best, best_poi = candidate, poi
                break
        if best is None:
            return None

        result = AttractionCandidate(
            name=best.name,
            area=best.area,
            estimated_visit_duration_hours=self._fallback_duration(best),
            reason=self._fallback_reason(best.name, best.area),
        )
        if settings.attraction_persist_enabled:
            self._persist_spot(city, best, best_poi)
        return result

    def _persist_spot(self, city: str, candidate: AttractionCandidate, poi: dict | None) -> None:
        """同步沉淀：LLM 判断是否主要景点并生成 reason/tags/duration（失败降级规则）"""
        entry = self._build_kb_entry(city, candidate, poi)
        if not entry["is_major"]:
            return
        try:
            from app.agent.knowledge.attraction_kb import add_spot

            add_spot(
                city,
                {
                    "name": entry["name"],
                    "area": entry["area"],
                    "estimated_visit_duration_hours": entry["estimated_visit_duration_hours"],
                    "reason": entry["reason"],
                    "tags": entry["tags"],
                },
                province=entry["province"],
            )
        except Exception:
            pass

    def _build_kb_entry(self, city: str, candidate: AttractionCandidate, poi: dict | None) -> dict:
        """构造待沉淀条目：优先 LLM 判断主要性 + 生成描述，LLM 不可用/失败降级为规则 + fallback。"""
        poi_type = str((poi or {}).get("type") or "") if isinstance(poi, dict) else ""
        province = str((poi or {}).get("pname") or "").strip() if isinstance(poi, dict) else ""
        fallback_tags = [seg.strip() for seg in poi_type.split(";") if seg.strip()][:3]
        area = candidate.area or city

        parsed = self._classify_spot(candidate.name, area, poi_type)
        if parsed is not None:
            is_major = bool(parsed.is_major)
            reason = (parsed.reason or "").strip() or self._fallback_reason(candidate.name, area)
            tags = [t.strip() for t in parsed.tags if t.strip()] or fallback_tags
            duration = parsed.estimated_visit_duration_hours or self._fallback_duration(candidate)
        else:
            is_major = self._is_major_spot(candidate.name, poi_type)
            reason = self._fallback_reason(candidate.name, area)
            tags = fallback_tags
            duration = self._fallback_duration(candidate)
        return {
            "is_major": is_major,
            "name": candidate.name,
            "area": area,
            "reason": reason,
            "tags": tags,
            "estimated_visit_duration_hours": duration,
            "province": province,
        }

    def _classify_spot(self, name: str, area: str, poi_type: str) -> SpotKbEntry | None:
        """LLM 一次完成：是否主要景点 + 生成 reason/tags/duration。LLM 不可用/失败返回 None"""
        try:
            from app.infrastructure.llm_client import get_llm_client

            client = get_llm_client()
        except Exception:
            client = None
        if client is None or not client.is_enabled():
            return None
        try:
            return client._generate_structured(
                schema=SpotKbEntry,
                system_prompt=ATTRACTION_PERSIST_SYSTEM_PROMPT,
                user_prompt=build_attraction_persist_user_prompt(name, area, poi_type),
                retry_hints=["严格按照 JSON 输出，字段名与格式必须一致。"],
            )
        except Exception:
            return None

    @staticmethod
    def _is_major_spot(name: str, poi_type: str | None = None) -> bool:
        """降级启发式：名称或 POI type 命中地标特征词即视为主要景点"""
        text = f"{poi_type or ''} {name}"
        return any(keyword in text for keyword in _LANDMARK_KEYWORDS)

    def _select_recommended(
        self, city: str, days: int, preferences: list[str], pool: list[AttractionCandidate], slots: int
    ) -> list[AttractionCandidate]:
        """剩余名额的排序/择优整体交给 LLM：给定候选池返回按推荐顺序的 slots 个景点"""
        if slots <= 0 or not pool:
            return []
        candidate_desc = "\n".join(f"{i + 1}. {c.name}（{c.area or city}）｜{c.reason or '主流景点'}" for i, c in enumerate(pool))
        parsed = self._classify_selection(city, days, preferences, candidate_desc, slots)
        if parsed is None:
            return pool[:slots]
        by_name = {self._normalize_name(c.name): c for c in pool}
        chosen: list[AttractionCandidate] = []
        seen: set[str] = set()
        # 优先按 LLM 给定顺序排列
        for n in parsed.names:
            wn = self._normalize_name(n)
            c = by_name.get(wn)
            if c is None or wn in seen:
                continue
            seen.add(wn)
            chosen.append(c)
        # 选中不足 slots（LLM 少给或名称未匹配）：按库顺序补足
        for c in pool:
            if len(chosen) >= slots:
                break
            wn = self._normalize_name(c.name)
            if wn in seen:
                continue
            seen.add(wn)
            chosen.append(c)
        return chosen[:slots]

    def _classify_selection(
        self, city: str, days: int, preferences: list[str], candidate_desc: str, slots: int
    ) -> SpotSelection | None:
        """LLM 择优：给候选池 + 偏好，选 slots 个最值得纳入的景点。不可用/失败返回 None"""
        try:
            from app.infrastructure.llm_client import get_llm_client

            client = get_llm_client()
        except Exception:
            client = None
        if client is None or not client.is_enabled():
            return None
        try:
            return client._generate_structured(
                schema=SpotSelection,
                system_prompt=ATTRACTION_SELECT_SYSTEM_PROMPT,
                user_prompt=build_attraction_select_user_prompt(city, days, preferences, candidate_desc, slots),
                retry_hints=["严格按照 JSON 输出，names 用候选中出现的准确名称，数量不超过上限。"],
            )
        except Exception:
            return None

    # ===================== 搜索基础能力 =====================

    def _safe_search(self, keyword: str, city: str) -> list[dict]:
        """高德对部分关键词偶发返回空，空结果时重试"""
        for _attempt in range(3):
            try:
                pois = amap_client.search_pois(keywords=keyword, city=city) or []
            except Exception:
                pois = []
            if pois:
                return pois
        return []

    def _poi_to_candidate(self, city: str, poi: dict) -> AttractionCandidate | None:
        name = str(poi.get("name") or "").strip()
        if not name:
            return None
        area = poi.get("adname") or poi.get("cityname") or poi.get("address") or city
        return AttractionCandidate(name=name, area=str(area).strip() if area else city)

    # ================ 规则：匹配 / 打分 / 兜底 / 去重 ================

    @staticmethod
    def _kb_name_match(a: str, b: str) -> bool:
        na = AttractionTool._normalize_name(a)
        nb = AttractionTool._normalize_name(b)
        return bool(na and nb and (na in nb or nb in na))

    @staticmethod
    def _normalize_name(value: str) -> str:
        return "".join(ch for ch in str(value).lower().strip() if not ch.isspace() and ch not in {"-", "_", "（", "）", "(", ")", "·", "/", "，", ","})

    def _is_noise_spot(self, name: str) -> bool:
        return any(keyword in str(name).strip() for keyword in ["票处", "停车场", "停车点", "索道", "游客中心", "服务中心", "入口", "出口", "检票口", "办公区", "办公点", "观景台", "观景点", "乘车处", "导览图", "社区", "超市", "特产", "客栈", "酒店", "山庄", "中学", "政府", "门票站", "候车处", "休息长廊", "模型室", "模型馆", "展厅", "展馆", "基石"])

    def _fallback_reason(self, name: str, area: str = "") -> str:
        area_text = f"，位于{area}" if area else ""
        return f"主流景点{area_text}，适合纳入本次行程。"

    def _fallback_duration(self, candidate: AttractionCandidate) -> float:
        text = self._normalize_name(candidate.name)
        if any(keyword in text for keyword in ["古镇", "街区", "景区", "博物馆", "遗址", "公园"]):
            return 2.5
        return 2.0

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


attraction_tool = AttractionTool()