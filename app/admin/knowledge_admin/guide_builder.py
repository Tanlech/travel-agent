"""城市攻略生成：把 data/attraction 的结构化景点数据编排成 markdown 攻略，写入 data/qa。

- data/qa 与 data/attraction 目录隔离，仅作为问答库（qa_kb）的文档数据源。
- 问答库整库重导时从 data/qa 递归读取所有 .md，按标题结构化分块入库。
- 用途：作为「生成城市攻略」功能的初始实现，后续可在此扩展更细的攻略编排。

运行：python -m app.admin.knowledge_admin.guide_builder
"""

from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ATTRACTION_DIR = _PROJECT_ROOT / "data" / "attraction"
_QA_DIR = _PROJECT_ROOT / "data" / "qa"

# 编排「代表性去处 / 优先级推荐」时优先的特色标签
_TOP_LANDMARK_TAGS = ("城市地标", "地方美食")


def _load_attraction(city_file: Path) -> dict | None:
    try:
        data = json.loads(city_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 数据文件损坏时跳过
        return None
    if not data.get("spots"):
        return None
    return data


def _spot_md(spot: dict) -> list[str]:
    name = spot.get("name")
    area = spot.get("area") or ""
    reason = (spot.get("reason") or "").strip()
    duration = spot.get("estimated_visit_duration_hours")
    dur = f"（建议游玩 {duration:.0f} 小时）" if isinstance(duration, (int, float)) else ""
    tags = spot.get("tags") or []
    lines = [f"### {name}{dur}", ""]
    if area:
        lines.append(f"- 区域：{area}")
    if reason:
        lines.append(f"- 简介：{reason}")
    if tags:
        lines.append(f"- 特色：{'、'.join(tags)}")
    lines.append("")
    return lines


def _build_city_md(city: str, data: dict) -> str:
    province = data.get("province") or ""
    spots = data["spots"]
    top = [s.get("name") for s in spots if any(t in (s.get("tags") or []) for t in _TOP_LANDMARK_TAGS)]
    top = (top[:6] or [s.get("name") for s in spots[:4]])

    lines = [f"# {city}旅行攻略", ""]

    lines.append("## 城市概览")
    lines.append(f"{city}是{province}的重要旅行目的地，拥有丰富的景点与人文自然资源。" if province else f"{city}是值得一游的旅行目的地。")
    if top:
        lines.append(f"代表性去处包括：{'、'.join(top)}。")
    lines.append("")

    lines.append("## 主要景点")
    lines.append("以下按城市汇总值得一游的景点，含所在区域、简介与建议游玩时长。")
    lines.append("")
    for spot in spots:
        lines.extend(_spot_md(spot))

    lines.append("## 出行贴士")
    lines.append("- 提前了解各景点营业时间与是否需预约，热门场馆建议线上预订门票。")
    lines.append("- 出行前关注当地天气，合理安排室内外活动。")
    lines.append("- 各景点建议游玩时长已在正文标注，可据此规划每日节奏。")
    lines.append("- 时间有限时，优先安排「城市地标」「地方美食」等特色景点。")

    return "\n".join(lines).rstrip() + "\n"


def build_city_guides() -> dict[str, Path]:
    """读取 data/attraction 全部城市景点数据，生成游玩攻略到 data/qa/{城市}.md，返回 {城市: 路径}"""
    _QA_DIR.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    if not _ATTRACTION_DIR.exists():
        return written
    for city_file in sorted(_ATTRACTION_DIR.rglob("*.json")):
        if city_file.name == "_tag_library.json":
            continue
        data = _load_attraction(city_file)
        if not data:
            continue
        city = data.get("city") or city_file.stem
        md = _build_city_md(city, data)
        out = _QA_DIR / f"{city}.md"
        out.write_text(md, encoding="utf-8")
        written[city] = out
    return written


if __name__ == "__main__":
    result = build_city_guides()
    for city, path in result.items():
        print(f"生成 {city} -> {path}")
    print(f"共生成 {len(result)} 份攻略")