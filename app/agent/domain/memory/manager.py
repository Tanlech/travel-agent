"""记忆融合：存储偏好 + 当前请求偏好 → UserContext；规划后持久化"""

from __future__ import annotations

from app.agent.domain.common.planning import PlanningRequest
from app.agent.domain.common.user import UserContext
from app.agent.domain.memory.schema import TripMemory, UserMemory
from app.agent.domain.memory.store import MemoryStore, memory_store

# 否定前缀（长前缀优先，避免"不喜欢"被"不"提前截断）
_NEGATION_PREFIXES = ("不喜欢", "不想", "不要", "讨厌", "不愿意", "别", "不")

# 以"不"开头但属正向的常见词（如"不错的推荐"），避免被单字"不"误判为负面偏好
_NON_NEGATION_NO_WORDS = ("不错",)

# 口语化偏好前后缀（入库前剔除，收敛为短词，避免长期记忆被"想轻松一点"这类长短语污染）
# 前缀长词优先（"想要"先于"想"），避免"想要轻松"被"想"截断后残留"要"
# 注意：后缀不含独立"点"，否则"景点"/"热点"会被削成"景"/"热"（"一点"已覆盖口语化场景）
_PREFERENCE_STRIP_PREFIXES = ("想要", "想", "要", "喜欢", "偏好", "希望", "比较", "打算", "计划", "去")
_PREFERENCE_STRIP_SUFFIXES = ("一点", "一些", "些", "就行", "就好", "的话", "的行程", "的")

# 中缀噪声短语（剥前后缀后仍残留的修饰成分，二次剥离收敛为短词，如"轻松一点的地方玩"→"轻松"）
# 只用多字短语，避免单字误伤实词（如"看"拆"看展"、"逛"拆"逛街"）
_PREFERENCE_MIDDLE_NOISE = (
    # 长短语优先，避免"的地方"先于"的地方玩"匹配导致残渣（"轻松的地方玩"→"轻松玩"）
    "的地方玩", "的地方", "去玩", "玩一下", "去看看", "逛一逛", "转一转",
    "一些", "一点", "就行", "就好",
)

# 长期偏好容量上限（超限裁剪最早累积的条目，防止记忆无限膨胀污染 LLM 上下文）
MAX_PREFERENCE_ITEMS = 20


def _normalize_preference(item: str) -> str:
    """收敛口语化偏好为短词：去常见前后缀与中缀噪声（如"想轻松一点"→"轻松"）；无法收敛时原样返回"""
    text = item.strip()
    for prefix in _PREFERENCE_STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
    for suffix in _PREFERENCE_STRIP_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)].rstrip()
    for noise in _PREFERENCE_MIDDLE_NOISE:
        text = text.replace(noise, "")
    text = text.strip()
    return text or item.strip()


def _normalize_preferences(items: list[str]) -> list[str]:
    """批量归一 + 保序去重（供存量记忆回溯清洗复用）"""
    return list(dict.fromkeys(p for p in (_normalize_preference(p) for p in items) if p))


def _cap_preferences(items: list[str]) -> list[str]:
    """容量上限裁剪：保留最新累积的条目（记忆反映近期口味），淘汰最早的"""
    return items[-MAX_PREFERENCE_ITEMS:]


def _parse_preference_item(item: str) -> tuple[str | None, str]:
    """拆否定前缀，返回 (前缀, 正文)；无否定时前缀为 None"""
    for prefix in _NEGATION_PREFIXES:
        if item.startswith(prefix):
            # 单字"不"需排除正向误判词（如"不错"），其余前缀直接生效
            if prefix == "不" and any(item.startswith(w) for w in _NON_NEGATION_NO_WORDS):
                return None, item.strip()
            body = item[len(prefix):].strip()
            if body:
                return prefix, body
    return None, item.strip()


class MemoryManager:
    """读取侧融合记忆与当前请求，写入侧持久化（store 可注入）"""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store: MemoryStore = store or memory_store

    def build_user_context(self, request: PlanningRequest, *, user_id: str | None = None) -> UserContext:
        """融合 stored 记忆与当前请求偏好 → UserContext（负面偏好进 disliked_styles）"""
        stored = self._store.load_user_memory(user_id)
        if stored is not None:
            # 回溯清洗历史偏好，读取时重新归一为短词（幂等：已干净的词不变），并裁剪超限条目，
            # 让喂给 LLM 的记忆始终是短词，避免污染只增不减
            cleaned_preferred = _cap_preferences(_normalize_preferences(stored.preferred_styles))
            cleaned_disliked = _cap_preferences(_normalize_preferences(stored.disliked_styles))
            # 惰性回写：仅当清洗结果有变化（存量脏数据/超限条目）才落盘，幂等后不再写，
            # 避免每次读取都对同一批脏数据重复清洗，也让 Redis 里沉淀的是干净短词
            if cleaned_preferred != stored.preferred_styles or cleaned_disliked != stored.disliked_styles:
                self._store.save_user_memory(
                    stored.model_copy(update={"preferred_styles": cleaned_preferred, "disliked_styles": cleaned_disliked})
                )
            stored.preferred_styles = cleaned_preferred
            stored.disliked_styles = cleaned_disliked
        # 拆否定：正向偏好做风格判定，"不要紧凑"不进正向信号
        positive_prefs: list[str] = []
        disliked_items: list[str] = []
        for item in (p.strip() for p in request.preferences if p and p.strip()):
            prefix, body = _parse_preference_item(item)
            if prefix:
                disliked_items.append(_normalize_preference(body))
            else:
                positive_prefs.append(_normalize_preference(item))

        pace = stored.pace_preference if stored else None
        if any("轻松" in item for item in positive_prefs):
            pace = "relaxed"
        elif any("深度" in item or "紧凑" in item for item in positive_prefs):
            pace = "dense"

        # 正向命中标记：本轮明确正向最高优先，否定信号不覆盖本轮正向
        theme_park_positive = any(any(kw in item for kw in ("乐园", "方特", "欢乐谷", "迪士尼")) for item in positive_prefs)
        nightlife_positive = any(any(kw in item for kw in ("夜游", "夜景", "演艺")) for item in positive_prefs)
        family_positive = any(any(kw in item.lower() for kw in ("亲子", "儿童", "带娃", "带小孩", "family", "kids")) for item in positive_prefs)
        senior_positive = any(any(kw in item.lower() for kw in ("老人", "长辈", "父母", "银发", "senior", "elder")) for item in positive_prefs)

        accept_theme_park = stored.accept_theme_park if stored else None
        if theme_park_positive:
            accept_theme_park = True
        accept_nightlife = stored.accept_nightlife if stored else None
        if nightlife_positive:
            accept_nightlife = True

        family_friendly = stored.family_friendly if stored else None
        if family_positive:
            family_friendly = True
        senior_friendly = stored.senior_friendly if stored else None
        if senior_positive:
            senior_friendly = True

        preferred_styles = _cap_preferences(list(dict.fromkeys((stored.preferred_styles if stored else []) + positive_prefs)))
        disliked_styles = _cap_preferences(list(dict.fromkeys((stored.disliked_styles if stored else []) + disliked_items)))
        # 否定信号：本轮"不要 X"可覆盖历史正向（偏好可翻转），避免历史记录把新表态永远锁死
        for body in disliked_items:
            if not theme_park_positive and any(kw in body for kw in ("乐园", "方特", "欢乐谷", "迪士尼")):
                accept_theme_park = False
            if not nightlife_positive and any(kw in body for kw in ("夜游", "夜景", "演艺")):
                accept_nightlife = False
            if not family_positive and any(kw in body for kw in ("亲子", "儿童", "带娃", "带小孩")):
                family_friendly = False
            if not senior_positive and any(kw in body for kw in ("老人", "长辈", "父母", "银发")):
                senior_friendly = False
        return UserContext(
            preferred_styles=preferred_styles,
            disliked_styles=disliked_styles,
            pace_preference=pace,
            accept_theme_park=accept_theme_park,
            accept_nightlife=accept_nightlife,
            family_friendly=family_friendly,
            senior_friendly=senior_friendly,
        )

    def load_user_memory(self, user_id: str | None) -> UserMemory | None:
        """读取已存储的用户偏好（原始记录，供前端"对话长期记忆"详情展示）"""
        if not user_id:
            return None
        return self._store.load_user_memory(user_id)

    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]:
        """读取历史行程记忆（匿名返回空，供 revise 参考）"""
        if not user_id:
            return []
        return self._store.load_trip_memories(user_id)

    def load_trip_history(self, user_id: str | None) -> list[dict]:
        """聚合历史行程为轻量记录（按目的地去重，保留最新），供 intent/revise 参考"""
        seen: dict[str, dict] = {}
        for memory in self.load_trip_memories(user_id):
            dest = memory.destination or "unknown"
            seen[dest] = {
                "destination": dest,
                "days": memory.days,
                "accepted_spots": list(memory.accepted_spots),
                "rejected_spots": list(memory.rejected_spots),
                "summary": (memory.summary or "")[:200],
            }
        return list(seen.values())

    def persist_user_memory(self, user_id: str | None, context: UserContext) -> None:
        """把本轮 UserContext 写回用户记忆（整体覆盖；字段与 UserMemory 一致，model_dump 直接映射）"""
        if not user_id:
            return
        self._store.save_user_memory(UserMemory(user_id=user_id, **context.model_dump()))

    def persist_trip_memory(self, user_id: str | None, request: PlanningRequest, accepted_spots: list[str], rejected_spots: list[str], summary: str | None, response_mode: str | None = None) -> None:
        """追加一条行程记忆（store 内部裁剪上限）"""
        if not user_id:
            return
        self._store.append_trip_memory(
            user_id,
            TripMemory(
                destination=request.destination,
                days=request.days,
                budget=request.budget,
                accepted_spots=accepted_spots,
                rejected_spots=rejected_spots,
                summary=summary,
                response_mode=response_mode,
            ),
        )


memory_manager = MemoryManager()
