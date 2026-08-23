"""attraction 知识库沉淀：把工具搜索确认的主要景点写回城市 json，并重新入库

数据布局与 scripts/import_attraction 保持一致（data/attraction/{city}/{city}.json），
写入后调用 knowledge_service.ingest_entries 把该城市整体重导，id = 城市:景点名，幂等覆盖。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from app.knowledge import ATTRACTION_COLLECTION, knowledge_service

# 数据目录：与 scripts/import_attraction/import_attraction.py 保持一致
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "attraction"

# 异步沉淀可能并发写同一 json，用全局锁保护"读-改-写"
_WRITE_LOCK = threading.Lock()


def _normalize(value: str) -> str:
    return "".join(ch for ch in str(value).lower().strip() if not ch.isspace())


def load_city_doc(city: str) -> dict | None:
    """读取城市景点 json（兼容 {city}/{city}.json 与 {city}.json），不存在或损坏返回 None"""
    path = _doc_path(city)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def add_spot(city: str, spot: dict, province: str = "") -> bool:
    """把单个景点写入城市 json；同名景点已存在则跳过。返回是否新增（新增即触发重导）。"""
    name = str(spot.get("name") or "").strip()
    if not name:
        return False
    with _WRITE_LOCK:
        doc = load_city_doc(city)
        if doc is None:
            doc = {"city": city, "province": province, "generated_at": None, "spots": []}
        spots = doc.setdefault("spots", [])
        if any(_normalize(str(item.get("name") or "")) == _normalize(name) for item in spots):
            return False

        entry = {
            "name": name,
            "area": str(spot.get("area") or "").strip() or city,
            "estimated_visit_duration_hours": spot.get("estimated_visit_duration_hours", 2.0),
            "reason": str(spot.get("reason") or "").strip(),
            "tags": [str(t).strip() for t in (spot.get("tags") or []) if str(t).strip()],
        }
        spots.append(entry)
        if not str(doc.get("province") or "").strip():
            doc["province"] = province
        doc["city"] = city
        doc.setdefault("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        path = _doc_path(city)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=4), encoding="utf-8")

        _reingest_city(city, doc)
    return True


def _reingest_city(city: str, doc: dict) -> int:
    """把该城市全部景点重新入库（幂等覆盖 + 新增）"""
    province = str(doc.get("province") or "").strip()
    entries: list[dict] = []
    for spot in doc.get("spots", []):
        name = str(spot.get("name") or "").strip()
        if not name:
            continue
        area = str(spot.get("area") or "").strip()
        duration = spot.get("estimated_visit_duration_hours", 2.0)
        reason = str(spot.get("reason") or "").strip()
        tags = [str(t).strip() for t in (spot.get("tags") or []) if str(t).strip()]
        entries.append(
            {
                "id": f"{city}:{name}",
                "text": f"{name}：{reason}（位于{area}，建议游玩{duration}小时）",
                "name": name,
                "city": city,
                "province": province,
                "area": area,
                "tags": ",".join(tags),
                "duration": str(duration),
                "reason": reason,
            }
        )
    if not entries:
        return 0
    return knowledge_service.ingest_entries(
        ATTRACTION_COLLECTION,
        entries,
        text_key="text",
        metadata_keys=("name", "city", "province", "area", "tags", "duration", "reason"),
    )


def _doc_path(city: str) -> Path:
    """兼容 data/attraction/{city}/{city}.json 与 data/attraction/{city}.json 两种布局"""
    candidate = DATA_DIR / city / f"{city}.json"
    if candidate.exists():
        return candidate
    return DATA_DIR / f"{city}.json"