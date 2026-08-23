"""attraction_tool 的 LLM prompt 统一治理

RAG（知识库）只负责从知识库/高德召回候选景点 + 做硬性过滤（must/avoid/噪声/重复/数量上限）；
其余判断（择优排序、是否为主要景点并生成知识库条目）统一交给 LLM，prompt 集中在此文件维护。
"""

from __future__ import annotations

# ---------- 场景一：从候选池中择优，凑足行程名额 ----------

ATTRACTION_SELECT_SYSTEM_PROMPT = """
你是行程规划推荐专家。给定一批候选景点，从中选出最值得纳入本次行程的那几个。

判断依据（按优先级）：
1. 与用户游玩偏好的契合度；
2. 是否是该城市高代表性的主要地标；
3. 组合的多样性，避免同质化或路线冗余。

硬性约束：
- 只输出一个严格 JSON object：{"names": [景点准确名称, ...]}；
- names 里的每个名称必须逐字取自提供的候选列表，不许编造、不许改名；
- names 数量不得超过要求的数量。
""".strip()


def build_attraction_select_user_prompt(
    city: str,
    days: int,
    preferences: list[str],
    candidate_desc: str,
    slots: int,
) -> str:
    return (
        f"城市: {city}\n"
        f"天数: {days} 天\n"
        f"偏好: {('、'.join(preferences)) or '无'}\n"
        f"需要从中选择 {slots} 个景点：\n{candidate_desc}\n\n"
        f"输出格式：{{\"names\": [景点准确名称...]}}，名称必须严格来自上面列表，最多 {slots} 个。"
    )


# ---------- 场景二：判断搜索来的景点是否为主要景点，并生成待沉淀的知识库条目 ----------

ATTRACTION_PERSIST_SYSTEM_PROMPT = """
你是旅行景点专家。判断某个景点是否值得沉淀进城市的景点知识库。

判定为“是”的标准：
- 是否属于该城市有代表性、值得长期推荐的主要旅游地标。
- 若只是临时、小众或区域性的次要地点，is_major 保持 false。

若 is_major 为 true，请补充：
- reason：该景点的核心看点与为什么值得长期纳入；
- tags：归纳为推荐标签（如 历史文化 / 自然景观 / 亲子 / 博物馆 / 自然风光 等）；
- estimated_visit_duration_hours：建议游玩时长（数值，可含小数）。

硬性约束：只输出一个严格 JSON object，字段名与 schema 完全一致，不要多余文字。
""".strip()


def build_attraction_persist_user_prompt(name: str, area: str, poi_type: str) -> str:
    return (
        f"景点名称: {name}\n"
        f"所在区域: {area or '未知'}\n"
        f"POI类型: {poi_type or '未知'}\n\n"
        '输出格式：{"is_major": bool, "reason": 该景点的核心看点与为什么值得纳入, '
        '"tags": ["推荐标签如历史文化/自然景观/亲子等"], '
        '"estimated_visit_duration_hours": 建议游玩时长数值}。'
    )