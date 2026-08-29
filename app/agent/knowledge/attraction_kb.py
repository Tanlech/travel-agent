"""景点知识库领域模块：把景点库全部业务集中在一个入口。

涵盖：
- 数据真源：data/attraction/{city}/{city}.json（与 scripts/import_attraction 一致）
- 入库 / 单点同步：build_attraction_entries / ingest_city（整体重导）/
  add_spot（单点 upsert） / remove_spot（按 id 删单点） / remove_city（清空整城）
- 后台查询与增删改：list_cities / list_spots / create_spot / create_spots_batch /
  update_spot（编辑，支持改名）/ delete_spot / delete_city / ai_generate_city_spots
- 整体重建：find_doc_paths / reindex_all（手动/定时 reindex 用）

关键约束：单个景点新增/编辑只同步该条（id = 城市:景点名，幂等覆盖），不做整城重导；
只有整体重建才逐城市 ingest_city。对 service / llm_client 采用惰性导入，避免循环依赖。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from app.infrastructure.conversions import safe_float
from app.agent.knowledge.ingest.common import ATTRACTION_COLLECTION

logger = logging.getLogger(__name__)

# 路径与 scripts/import_attraction/import_attraction.py 保持一致
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "attraction"

# 异步沉淀 / 后台增删可能并发写同一 json，用全局锁保护"读-改-写"
_WRITE_LOCK = threading.Lock()


def _pretty_dumps(data, indent: int = 4) -> str:
    """json.dumps 的落盘替代：dict 层级缩进美观，但纯值数组（如 tags）输出为单行。

    例如 tags 输出为：
        "tags": ["历史", "古建筑", "非遗"]
    而非逐行展开，便于人工阅读与维护。
    """
    from json import JSONEncoder

    _encoder = JSONEncoder(ensure_ascii=False)
    sp = " " * indent

    def scalar(v):
        return _encoder.encode(v)

    def is_leaf_list(lst):
        return all(isinstance(x, (str, int, float, bool)) or x is None for x in lst)

    def fmt(v, d):
        pad = sp * d
        if isinstance(v, dict):
            if not v:
                return "{}"
            lines = [f"{pad}{scalar(k)}: {fmt(val, d + 1)}" for k, val in v.items()]
            return "{\n" + ",\n".join(lines) + "\n" + sp * (d - 1) + "}"
        if isinstance(v, list):
            if not v:
                return "[]"
            if is_leaf_list(v):
                return "[" + ", ".join(scalar(x) for x in v) + "]"
            elems = [fmt(x, d + 1) for x in v]
            return "[\n" + ",\n".join(elems) + "\n" + sp * (d - 1) + "]"
        return scalar(v)

    return fmt(data, 1)

_METADATA_KEYS = ("name", "city", "province", "area", "tags", "duration", "reason")


# ---------- AI 生成的结构化输出 ----------


class AttractionSpotAiOutput(BaseModel):
    """AI 生成的景点结构化输出（用于后台"AI 新增景点"）。"""

    name: str
    province: str = ""
    area: str = ""
    estimated_visit_duration_hours: float = 2.0
    reason: str = ""
    tags: list[str] = Field(default_factory=list)


class AttractionCityAiOutput(BaseModel):
    """AI 生成某城市的一批景点。"""

    spots: list[AttractionSpotAiOutput] = Field(default_factory=list)


class TagUpgradeItem(BaseModel):
    """AI 升级标签库：单个标准标签及其别名、归属分类"""

    tag: str = ""
    category: str = ""
    aliases: list[str] = Field(default_factory=list)


class TagUpgradeLibraryOutput(BaseModel):
    """AI 全自动升级后的标准标签库输出"""

    tags: list[TagUpgradeItem] = Field(default_factory=list)


class SpotRetagItemOutput(BaseModel):
    """AI 重打标签：单个景点选中的标准标签"""

    name: str = ""
    tags: list[str] = Field(default_factory=list)


class SpotRetagCityOutput(BaseModel):
    """AI 重打标签：某城市全部景点"""

    spots: list[SpotRetagItemOutput] = Field(default_factory=list)


class QualityAiItemOutput(BaseModel):
    """AI 对单个景点对的判定"""

    main: str = ""
    sub: str = ""
    judgment: str = ""
    reasoning: str = ""
    suggested_action: str = "keep"


class QualityAiOutput(BaseModel):
    """AI 质量判定输出"""

    items: list[QualityAiItemOutput] = Field(default_factory=list)


# ---------- 数据真源（json 落盘 + 向量入库） ----------


def _normalize(value: str) -> str:
    return "".join(ch for ch in str(value).lower().strip() if not ch.isspace())


def _doc_path(city: str) -> Path:
    """定位城市景点 json：兼容 {city}/{city}.json、{city}.json 与按省份分组的 {province}/{city}.json 布局"""
    candidate = DATA_DIR / city / f"{city}.json"
    if candidate.exists():
        return candidate
    candidate = DATA_DIR / f"{city}.json"
    if candidate.exists():
        return candidate
    # 按省份分组布局（如 data/attraction/湖北/武汉.json）：扫描全部 json，按文档内 city 字段匹配
    if DATA_DIR.is_dir():
        target = _normalize(city)
        for path in DATA_DIR.rglob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if _normalize(str(doc.get("city") or "")) == target:
                return path
    return candidate


def load_city_doc(city: str) -> dict | None:
    """读取城市景点 json（兼容 {city}/{city}.json 与 {city}.json），不存在或损坏返回 None"""
    path = _doc_path(city)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_DOC_TOP_KEYS = ["province", "city", "generated_at", "spots"]


def _write_city_doc(city: str, doc: dict, province: str = "") -> Path:
    """统一落盘城市 json：
    - generated_at 刷新为更新时间；
    - 顶层键顺序固定为 province → city → generated_at → spots；
    - 新城市且给定省份时，写入省份分组布局 data/attraction/{省份}/{城市}.json
    （已存在的文件保持原布局，仅覆写同一路径）。
    返回实际写入的路径。
    """
    doc["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    province = str(province or "").strip() or str(doc.get("province") or "").strip()
    if province:
        doc["province"] = province
    doc["city"] = city
    # 顶层键重排
    ordered: dict = {}
    for k in _DOC_TOP_KEYS:
        if k in doc:
            ordered[k] = doc[k]
    for k, v in doc.items():
        if k not in _DOC_TOP_KEYS:
            ordered[k] = v
    doc.clear()
    doc.update(ordered)
    path = _doc_path(city)
    if not path.exists() and province:
        path = DATA_DIR / province / f"{city}.json"
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_pretty_dumps(doc), encoding="utf-8")
    return path


def build_attraction_entries(city: str, spots: list[dict], province: str = "") -> list[dict]:
    """把景点清单标准化为入库条目：text 为可检索正文，其余字段作为 metadata"""
    entries: list[dict] = []
    for spot in spots:
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
    return entries


def ingest_city(
    city: str,
    spots: list[dict],
    province: str = "",
    collection: str = ATTRACTION_COLLECTION,
) -> int:
    """把某城市全部景点写入知识库（先清该城市旧点，再整体重导，避免脏数据残留）；无有效景点时返回 0"""
    entries = build_attraction_entries(city, spots, province)
    if not entries:
        return 0
    from app.agent.knowledge.service import knowledge_service

    # 以当前清单为准整体重建：清掉该城市已删除的旧景点点
    knowledge_service.clear(collection, where={"city": city})
    return knowledge_service.ingest_entries(
        collection,
        entries,
        text_key="text",
        metadata_keys=_METADATA_KEYS,
    )


def _sync_spot_to_kb(city: str, spot: dict, province: str) -> int:
    """单点同步：只把这一个景点入库到 Qdrant（id=城市:景点名，幂等覆盖）
    「单个景点新增/编辑」用，避免整城重导"""
    from app.agent.knowledge.service import knowledge_service

    entries = build_attraction_entries(city, [spot], province)
    if not entries:
        return 0
    return knowledge_service.upsert_entry(ATTRACTION_COLLECTION, entries[0], text_key="text", metadata_keys=_METADATA_KEYS)


def _sync_delete_spots(city: str, names: list[str]) -> None:
    """按稳定 id 精确删除 Qdrant 中对应景点点（单个景点删除用）"""
    from app.agent.knowledge.service import knowledge_service

    ids = [f"{city}:{n}" for n in names if n]
    if ids:
        knowledge_service.delete_entries(ATTRACTION_COLLECTION, ids)


def add_spot(city: str, spot: dict, province: str = "") -> bool:
    """把单个景点写入城市 json + 单点同步向量；同名景点已存在则跳过。返回是否新增。"""
    name = str(spot.get("name") or "").strip()
    if not name:
        return False
    with _WRITE_LOCK:
        doc = load_city_doc(city)
        if doc is None:
            doc = {"province": province, "city": city, "generated_at": None, "spots": []}
        spots = doc.setdefault("spots", [])
        if any(_normalize(str(item.get("name") or "")) == _normalize(name) for item in spots):
            return False

        entry = {
            "name": name,
            "area": str(spot.get("area") or "").strip() or city,
            "estimated_visit_duration_hours": spot.get("estimated_visit_duration_hours", 2.0),
            "reason": str(spot.get("reason") or "").strip(),
            "tags": clean_tags([str(t).strip() for t in (spot.get("tags") or []) if str(t).strip()]),
        }
        spots.append(entry)
        province = str(spot.get("province") or "").strip() or str(doc.get("province") or "").strip() or province
        _write_city_doc(city, doc, province)
        province = str(doc.get("province") or "").strip()
        _sync_spot_to_kb(city, entry, province)
    return True


def update_spot(city: str, name: str, data: dict) -> bool:
    """编辑某城市的一个景点：写 json + 单点同步向量。

    支持改名：同步时删旧点（id=城市:原名）+ 入库新点（id=城市:新名）；
    不改名则直接覆盖同 id 单点。景点不存在或改名撞车其他景点时返回 False。
    """
    city = str(city or "").strip()
    name = str(name or "").strip()
    if not city or not name:
        return False
    with _WRITE_LOCK:
        doc = load_city_doc(city)
        if doc is None:
            return False
        spots = doc.setdefault("spots", [])
        norm = _normalize(name)
        idx = next(
            (i for i, s in enumerate(spots) if _normalize(str(s.get("name") or "")) == norm),
            None,
        )
        if idx is None:
            return False
        old = spots[idx]
        new_name = str(data.get("name") or "").strip() or name
        # 改名落在其他已有景点上视为冲突
        if _normalize(new_name) != norm and any(
            j != idx and _normalize(str(s.get("name") or "")) == _normalize(new_name)
            for j, s in enumerate(spots)
        ):
            return False
        new = {
            "name": new_name,
            "area": str(data.get("area") or "").strip() or city,
            "estimated_visit_duration_hours": safe_float(data.get("duration"))
            or safe_float(old.get("estimated_visit_duration_hours"))
            or 2.0,
            "reason": str(data.get("reason") or "").strip(),
            "tags": clean_tags([str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]),
        }
        spots[idx] = new
        province = str(data.get("province") or "").strip() or str(doc.get("province") or "").strip()
        _write_city_doc(city, doc, province)
        province = str(doc.get("province") or "").strip()
        # 同步向量：改名则先删旧点再入库新点，否则单点覆盖
        if _normalize(new_name) != norm:
            _sync_delete_spots(city, [name])
        _sync_spot_to_kb(city, new, province)
    return True


def remove_spot(city: str, name: str) -> bool:
    """删除某城市的一个景点（写 json + 按 id 删单点向量）；景点不存在返回 False"""
    name = str(name or "").strip()
    if not name:
        return False
    with _WRITE_LOCK:
        doc = load_city_doc(city)
        if doc is None:
            return False
        spots = doc.setdefault("spots", [])
        norm = _normalize(name)
        # 命中同名的精确名称，用于对 Qdrant 按 id 删除（id=城市:景点名）
        removed_names = [str(s.get("name") or "").strip() for s in spots if _normalize(str(s.get("name") or "")) == norm]
        kept = [s for s in spots if _normalize(str(s.get("name") or "")) != norm]
        if not removed_names:
            return False
        doc["spots"] = kept
        _write_city_doc(city, doc, str(doc.get("province") or "").strip())
        _sync_delete_spots(city, removed_names)
    return True


def remove_city(city: str) -> bool:
    """删除某地点（城市）的全部景点数据：json 落盘删除 + 知识库该城市点清空。返回是否删除"""
    city = str(city or "").strip()
    if not city:
        return False
    existed = False
    path = _doc_path(city)
    if path and path.exists():
        path.unlink()
        existed = True
    # 兼容残留的 {city}/{city}.json 布局文件
    legacy = DATA_DIR / city / f"{city}.json"
    if legacy.exists() and legacy != path:
        legacy.unlink()
        existed = True
    # 空目录一并清理
    dir_path = DATA_DIR / city
    if dir_path.is_dir() and not any(dir_path.iterdir()):
        try:
            dir_path.rmdir()
        except OSError:
            pass
    if existed:
        from app.agent.knowledge.service import knowledge_service

        knowledge_service.clear(ATTRACTION_COLLECTION, where={"city": city})
    return existed


# ---------- 后台查询 / 增删 ----------


def list_cities() -> list[dict]:
    """按地点聚合景点数量与省份（景点知识库）"""
    from app.agent.knowledge.service import knowledge_service

    by_city: dict[str, dict] = {}
    try:
        for item in knowledge_service.get_all(ATTRACTION_COLLECTION):
            meta = item.metadata or {}
            city = str(meta.get("city") or "未知").strip()
            info = by_city.setdefault(city, {"city": city, "province": "", "count": 0})
            info["count"] += 1
            province = str(meta.get("province") or "").strip()
            if province and not info["province"]:
                info["province"] = province
    except Exception as exc:
        logger.warning("kb list cities failed: %s", exc)
    return sorted(by_city.values(), key=lambda c: c["count"], reverse=True)


def list_spots(city: str) -> list[dict]:
    """查询某地点下包含的全部景点"""
    from app.agent.knowledge.service import knowledge_service

    city = str(city or "").strip()
    spots: list[dict] = []
    if not city:
        return spots
    try:
        for item in knowledge_service.get_all(ATTRACTION_COLLECTION, where={"city": city}):
            meta = item.metadata or {}
            spots.append(
                {
                    "name": str(meta.get("name") or "").strip(),
                    "area": str(meta.get("area") or "").strip(),
                    "duration": safe_float(meta.get("duration")),
                    "tags": [t for t in str(meta.get("tags") or "").split(",") if t],
                    "reason": str(meta.get("reason") or "").strip(),
                }
            )
    except Exception as exc:
        logger.warning("kb list spots fail %s: %s", city, exc)
    spots.sort(key=lambda s: s["name"])
    return spots


def create_spot(data: dict) -> bool:
    """在某地点新建景点（写 json + 单点同步）；重名/缺字段返回 False"""
    city = str(data.get("city") or "").strip()
    name = str(data.get("name") or "").strip()
    if not city or not name:
        return False
    return add_spot(
        city,
        {
            "name": name,
            "area": str(data.get("area") or "").strip() or city,
            "estimated_visit_duration_hours": safe_float(data.get("duration")) or 2.0,
            "reason": str(data.get("reason") or "").strip(),
            "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
        },
        province=str(data.get("province") or "").strip(),
    )


def create_spots_batch(city: str, spots: list[dict]) -> dict:
    """批量新建某地点景点（逐条写入 json + 单点同步），返回成功/已存在/失败统计"""
    city = str(city or "").strip()
    created, exists, failed = 0, 0, 0
    errors: list[str] = []
    for item in spots:
        name = str(item.get("name") or "").strip()
        if not name:
            failed += 1
            continue
        ok = create_spot({
            "city": city,
            "name": name,
            "province": str(item.get("province") or "").strip(),
            "area": str(item.get("area") or "").strip() or city,
            "duration": safe_float(item.get("duration")) or 2.0,
            "reason": str(item.get("reason") or "").strip(),
            "tags": [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()],
        })
        if ok:
            created += 1
        else:
            exists += 1
            errors.append(name)
    return {
        "status": "ok",
        "city": city,
        "created": created,
        "exists": exists,
        "failed": failed,
        "errors": errors[:20],
    }


def delete_spot(city: str, name: str) -> bool:
    """删除某地点下的一个景点"""
    return remove_spot(str(city or "").strip(), str(name or "").strip())


def delete_city(city: str) -> bool:
    """删除某地点（城市）及其全部景点数据"""
    return remove_city(str(city or "").strip())


# ---------- 标准标签库与标签清洗 ----------
_TAG_LIB_PATH = DATA_DIR / "_tag_library.json"
_TAG_LIB: dict | None = None
_TAG_LIB_MTIME = None

_DEFAULT_TAG_LIB = {
    "version": 1,
    "tags": {
        "历史": ["历史古迹", "古迹", "古建筑", "古城", "古镇", "遗址", "人文", "博物馆", "民俗", "文化", "文物"],
        "经典": ["必去", "招牌", "标志性"],
        "宗教": ["寺庙", "庙宇", "道观", "教堂", "禅"],
        "自然": ["自然风光", "自然景观", "风景区", "生态", "山水", "森林", "湿地"],
        "山川": ["山峰", "名山", "登山", "徒步", "爬山"],
        "湖泊": ["湖泊", "湖水", "水库", "湖面"],
        "海滨": ["海滨", "海岛", "沙滩", "海边", "海域"],
        "公园": ["绿地", "园林", "植物园", "动物园"],
        "地标": ["地标", "网红", "打卡", "地标建筑"],
        "美食": ["小吃", "夜市", "餐饮", "美食街", "饮食"],
        "亲子": ["遛娃", "家庭", "儿童", "游乐园", "乐园"],
        "摄影": ["拍照", "出片", "摄影", "拍照打卡"],
        "夜景": ["灯光", "夜游", "夜景观景"],
        "户外": ["探险", "露营", "骑行", "漂流", "攀岩"],
        "冰雪": ["滑雪", "冰雪"],
        "温泉": ["温泉", "养生"],
    },
}


def load_tag_library() -> dict:
    """读取标准标签库（data/attraction/_tag_library.json）；文件缺失/损坏返回内置默认库，带 mtime 缓存"""
    global _TAG_LIB, _TAG_LIB_MTIME
    try:
        mtime = _TAG_LIB_PATH.stat().st_mtime
    except Exception:
        mtime = None
    if _TAG_LIB is not None and mtime == _TAG_LIB_MTIME:
        return _TAG_LIB
    cfg = _DEFAULT_TAG_LIB
    try:
        data = json.loads(_TAG_LIB_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tags"), dict) and data["tags"]:
            cfg = {"version": data.get("version", 1), "tags": data["tags"]}
            # 保留文件里的分类，供后台按分类展示标签
            cats = data.get("categories")
            if isinstance(cats, dict) and cats:
                cfg["categories"] = cats
            # 保留被锁定的标签名（更新标签库时不会被改动）
            locked = data.get("locked")
            if isinstance(locked, list):
                cfg["locked"] = locked
    except Exception:
        pass
    _TAG_LIB = cfg
    _TAG_LIB_MTIME = mtime
    return _TAG_LIB


def get_standard_tags() -> list[str]:
    """返回标准标签名列表"""
    return list(load_tag_library()["tags"].keys())


def clean_tags(tags: list, lib: dict | None = None) -> list[str]:
    """把杂乱标签按标准标签库归并：命中标准名/别名 → 归并到标准名，否则丢弃，统一为库内标签"""
    lib = lib or load_tag_library()
    lookup: dict[str, str] = {}
    for std, aliases in (lib["tags"]).items():
        lookup[_normalize(std)] = std
        for a in aliases or []:
            a = _normalize(str(a))
            if a:
                lookup[a] = std
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        raw = str(t).strip()
        if not raw:
            continue
        std = lookup.get(_normalize(raw))
        if std and std not in seen:
            seen.add(std)
            out.append(std)
    # 兜底：有标签但全部未命中时，给一个标准标签"经典"，避免空标签影响检索
    if not out and any(str(t).strip() for t in (tags or [])):
        out = ["经典"]
    return out[:8]


def scrub_all_tags() -> dict:
    """一键重刷全部城市景点标签：按标准标签库清洗每个 json 的 tags 并对改动城市整体重导向量"""
    lib = load_tag_library()
    with _WRITE_LOCK:
        changed_cities = 0
        changed_spots = 0
        removed_total = 0
        for path in find_doc_paths():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            spots = doc.get("spots") or []
            if not spots:
                continue
            city = str(doc.get("city") or path.stem).strip()
            mutated = False
            for s in spots:
                old = [str(t).strip() for t in (s.get("tags") or []) if str(t).strip()]
                new = clean_tags(old, lib)
                if old != new:
                    diff = len(old) - len(new)
                    if diff > 0:
                        removed_total += diff
                    s["tags"] = new
                    mutated = True
                    changed_spots += 1
            if mutated:
                _write_city_doc(city, doc, str(doc.get("province") or "").strip())
                changed_cities += 1
                ingest_city(city, spots, str(doc.get("province") or "").strip())
    return {
        "status": "ok",
        "changed_cities": changed_cities,
        "changed_spots": changed_spots,
        "removed_tags": removed_total,
    }


def scrub_city_tags(city: str) -> dict:
    """用 LLM 重刷指定城市景点标签：逐个理解每个景点（名称/区域/简介），
    结合当前标准标签库重新选标签并整体重导向量（只动该城市）。"""
    from app.infrastructure.llm_client import get_llm_client

    city = str(city or "").strip()
    doc = load_city_doc(city)
    if not doc:
        return {"status": "error", "message": f"地点「{city}」不存在", "changed_spots": 0}
    spots = doc.get("spots") or []
    if not spots:
        return {"status": "ok", "changed_spots": 0, "note": "该地点暂无景点"}
    lib = load_tag_library()
    std_tags = list(lib["tags"].keys())

    # 组装景点信息供模型理解
    spot_lines: list[str] = []
    for idx, s in enumerate(spots):
        name = str(s.get("name") or "").strip() or f"景点{idx + 1}"
        area = str(s.get("area") or "").strip()
        reason = str(s.get("reason") or "").strip()
        cur = [str(t).strip() for t in (s.get("tags") or []) if str(t).strip()]
        spot_lines.append(
            f"{idx}. 名称：{name}；区域：{area or '无'}；简介：{reason[:200] or '无'}；现有标签：{('、'.join(cur) if cur else '无')}"
        )
    system_prompt = (
        "你是一名专业的中国旅游景点数据整理助手。根据每个景点的名称、区域简介，"
        "结合给定的标准标签库，为每个景点重新选取合适的标准标签并输出结构化 JSON。\n"
        f"可选标签（只能从中选，不要自创，每个景点 1~4 个）：{', '.join(std_tags)}\n"
        "要求：\n"
        "1. 输出 spots 数组，每项为 {\"name\": 景点原文名称, \"tags\": [选中的标准标签]}；\n"
        "2. tags 只能从上述标准标签中选取，按契合度取 1~4 个；\n"
        "3. 若某景点信息不足，tags 至少给 1 个最可能的标准标签，不要留空；\n"
        "4. 原文的名称必须原样输出，便于回填。"
    )
    user_prompt = f"城市：{city}\n以下是该城市全部景点，请逐一为每个景点打标签：\n" + "\n".join(spot_lines)
    result = get_llm_client()._generate_structured(
        schema=SpotRetagCityOutput,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        retry_hints=[
            "Return JSON only.",
            "spots must be an array, each item's name must match the original spot name exactly.",
            "tags must be chosen only from the provided standard tags (1~4 each).",
        ],
    )
    if result is None:
        return {"status": "error", "message": "AI 重打标签失败，请重试", "changed_spots": 0}

    norm_map: dict[str, list[str]] = {}
    for item in result.spots or []:
        key = _normalize(item.name)
        if key:
            norm_map[key] = item.tags
    allowed = set(std_tags)

    changed = 0
    new_spots: list[dict] = []
    with _WRITE_LOCK:
        for s in spots:
            ns = dict(s)
            key = _normalize(str(ns.get("name") or "").strip())
            new_tags = [t for t in (norm_map.get(key) or []) if t in allowed][:8]
            if not new_tags:
                new_tags = clean_tags(ns.get("tags") or [], lib)  # 兜底：模型漏判时沿用规则清洗
            old = [str(t).strip() for t in (ns.get("tags") or []) if str(t).strip()]
            if old != new_tags:
                ns["tags"] = new_tags
                changed += 1
            new_spots.append(ns)
        doc["spots"] = new_spots
        _write_city_doc(city, doc, str(doc.get("province") or "").strip())
        ingest_city(city, new_spots, str(doc.get("province") or "").strip())
    return {"status": "ok", "changed_spots": changed}


def scrub_all_cities_tags() -> dict:
    """用 LLM 重刷知识库内全部城市景点的标签：逐个城市复用 scrub_city_tags 的 LLM 打标逻辑"""
    city_names: list[str] = []
    for path in find_doc_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        c = str(doc.get("city") or path.stem).strip()
        if c and (doc.get("spots") or []):
            city_names.append(c)
    changed_cities, changed_spots = 0, 0
    errors: list[str] = []
    for c in city_names:
        r = scrub_city_tags(c)
        if r.get("status") == "ok":
            changed_cities += 1
            changed_spots += int(r.get("changed_spots") or 0)
        else:
            errors.append(f"{c}: {r.get('message') or '失败'}")
    return {"status": "ok", "changed_cities": changed_cities, "changed_spots": changed_spots, "errors": errors[:10]}


# ---------- 景点质量保证：疑似子景点/重复检测 ----------


def quality_check(city: str) -> list[dict]:
    """扫描某城市景点，找疑似『子景点/被大景点覆盖』与『名称近似重复』配对（供后台人工确认）"""
    doc = load_city_doc(str(city or "").strip())
    if not doc:
        return []
    from difflib import SequenceMatcher

    names = [str(s.get("name") or "").strip() for s in (doc.get("spots") or []) if str(s.get("name") or "").strip()]
    groups: list[dict] = []
    used: set[tuple[str, str]] = set()
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            na, nb = _normalize(a), _normalize(b)
            if na == nb:
                continue
            key = tuple(sorted([na, nb]))
            if key in used:
                continue
            # 覆盖：较长名包含较短名，且短名不短于 2 字
            if len(na) >= 4 and len(nb) >= 2 and (na in nb or nb in na):
                main, sub = (a, b) if len(na) > len(nb) else (b, a)
                used.add(key)
                groups.append({
                    "city": str(city).strip(),
                    "main": main,
                    "sub": sub,
                    "kind": "cover",
                    "reason": f"{sub} 疑似是 {main} 的子景点/被其覆盖",
                })
                continue
            ratio = SequenceMatcher(None, na, nb).ratio()
            if ratio >= 0.82:
                used.add(key)
                groups.append({
                    "city": str(city).strip(),
                    "main": a,
                    "sub": b,
                    "kind": "similar",
                    "reason": f"名称高度相近（相似度 {ratio:.0%}），疑似重复",
                })
    return groups


def merge_spot_into(city: str, main: str, sub: str) -> tuple[bool, str]:
    """把子景点 sub 的简介与标签合并进主景点 main，然后删除 sub（写 json + 同步向量）"""
    with _WRITE_LOCK:
        doc = load_city_doc(city)
        if doc is None:
            return False, "城市不存在"
        spots = doc.get("spots") or []
        sub_item = next(
            (s for s in spots if _normalize(str(s.get("name") or "")) == _normalize(str(sub or ""))),
            None,
        )
        if sub_item is None:
            return False, f"未找到 {city}/{sub}"
        idx_main = next(
            (i for i, s in enumerate(spots) if _normalize(str(s.get("name") or "")) == _normalize(str(main or ""))),
            None,
        )
        if idx_main is None:
            return False, f"未找到主景点 {city}/{main}"
        main_item = spots[idx_main]
        sub_reason = str(sub_item.get("reason") or "").strip()
        main_reason = str(main_item.get("reason") or "").strip()
        if sub_reason and sub_reason not in main_reason:
            main_item["reason"] = (main_reason + "；" + sub_reason).strip("；")
        merged_tags: list[str] = []
        for t in list(main_item.get("tags") or []) + list(sub_item.get("tags") or []):
            tt = str(t).strip()
            if tt and tt not in merged_tags:
                merged_tags.append(tt)
        main_item["tags"] = clean_tags(merged_tags)[:8]
        spots[:] = [s for s in spots if s is not sub_item]
        province = str(doc.get("province") or "").strip()
        _write_city_doc(city, doc, province)
        # 合并后的主景点单点同步（覆盖同 id 向量），并删除 sub 的向量
        _sync_spot_to_kb(city, main_item, province)
        _sync_delete_spots(city, [sub])
        return True, ""


def apply_quality(actions: list[dict]) -> dict:
    """应用人工确认后的质量处理：merge=合并到主景点并删子，delete=删除子，keep=忽略"""
    merged, deleted, kept = 0, 0, 0
    errors: list[str] = []
    for act in actions:
        kind = str(act.get("action") or "").strip().lower()
        city = str(act.get("city") or "").strip()
        main = str(act.get("main") or "").strip()
        sub = str(act.get("sub") or "").strip()
        if kind not in ("merge", "delete", "keep"):
            errors.append("未知操作类型")
            continue
        if not city or not sub:
            errors.append("缺少城市/子景点")
            continue
        if kind == "keep":
            kept += 1
        elif kind == "delete":
            if remove_spot(city, sub):
                deleted += 1
            else:
                errors.append(f"删除失败：{city}/{sub}")
        else:
            ok, msg = merge_spot_into(city, main, sub)
            if ok:
                merged += 1
            else:
                errors.append(msg)
    return {"status": "ok", "merged": merged, "deleted": deleted, "kept": kept, "errors": errors[:10]}


def upgrade_tag_library() -> dict:
    """AI 全自动升级标准标签库：基于当前标签库与全库景点实际标签，让模型产出更完善的标准标签库，
    自动写入 _tag_library.json 并重刷全部景点标签（全自动，无需人工确认）。"""
    from app.infrastructure.llm_client import get_llm_client

    current = load_tag_library()
    current_tags = list(current["tags"].keys())
    # 全库实际使用到的标签及词频
    freq: dict[str, int] = {}
    for path in find_doc_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for s in doc.get("spots") or []:
            for t in s.get("tags") or []:
                tt = str(t).strip()
                if tt:
                    freq[tt] = freq.get(tt, 0) + 1
    actual = [(t, n) for t, n in sorted(freq.items(), key=lambda x: -x[1])]

    cur_lines = "；".join(f"{std}(别名:{'/'.join(a or []) or '无'})" for std, a in current["tags"].items()) or "(暂无标准标签)"
    occur_lines = "；".join(f"{t}×{n}" for t, n in actual[:120]) or "(无实际标签数据)"

    system_prompt = (
        "你是中国旅游知识库的标签体系设计师。下面给出当前标准标签库，以及全库景点实际用到的标签词频。\n"
        "请设计一套**更丰富、更细分、带分类**的标准标签库：\n"
        "1. 每个标准标签都必须归属于一个分类(category，如：自然风光/山地森林/海滨海岛/温泉养生/冰雪滑雪/户外运动/历史文化/人文民俗/城市地标/美食购物/亲子休闲/艺术美拍，也可自拟通俗分类名)；\n"
        "2. 标准标签数量必须充足，**不少于 35 个、原则上 40~60 个**，细分为具体旅游主题（如把‘历史’拆成 古建筑/古城古镇/遗址/博物馆/红色旅游 等），不要把差异较大的主题塞进同一个标准标签；\n"
        "3. 覆盖主要旅游维度：自然山水、山川徒步、森林峡谷、湖泊湿地、海滨海岛、温泉康养、冰雪滑雪、户外运动、历史人文、宗教、民俗非遗、美食夜市、亲子乐园、夜景地标、摄影购物等，并把用户高频使用、语义清晰的标签吸收进对应标准标签；\n"
        "4. 每个标准标签给出常用别名（别名会被自动归并到该标准标签）；合并同义、避免重复；标准标签名用简洁规范的中文（2~4 字为主）；\n"
        "5. 输出 JSON，字段 tags 为数组，每项形如 {\"tag\": \"标准名\", \"category\": \"分类名\", \"aliases\": [\"别名1\", \"别名2\"]}。"
    )
    user_prompt = f"当前标准标签库：\n{cur_lines}\n\n全库景点的实际标签及词频：\n{occur_lines}"

    result = get_llm_client()._generate_structured(
        schema=TagUpgradeLibraryOutput,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        retry_hints=[
            "Return JSON only.",
            'tags must be a JSON array and each item like {"tag":"标准名","category":"分类名","aliases":["别名"]}',
        ],
    )
    if result is None:
        return {"status": "error", "message": "标签库升级失败：模型无返回"}
    new_tags: dict[str, list[str]] = {}
    new_categories: dict[str, list[str]] = {}
    for it in result.tags or []:
        tag = str(it.tag).strip()
        if not tag:
            continue
        aliases = [str(a).strip() for a in (it.aliases or []) if str(a).strip() and str(a).strip() != tag]
        new_tags[tag] = aliases
        cat = str(it.category or "").strip()
        if cat:
            new_categories.setdefault(cat, [])
            if tag not in new_categories[cat]:
                new_categories[cat].append(tag)
    if len(new_tags) < 20:
        return {"status": "error", "message": f"标签库升级失败：仅生成 {len(new_tags)} 个标准标签（不足），请重试"}
    # 保留被锁定的标签：模型即便遗漏，也要原样保留（含其别名与分类）
    locked = [t for t in (current.get("locked") or []) if t]
    if locked:
        old_cats = current.get("categories") or {}
        cat_of: dict[str, str] = {}
        for c_name, c_arr in old_cats.items():
            for t in c_arr:
                cat_of[t] = c_name
        for t in locked:
            if t not in new_tags:
                new_tags[t] = current["tags"].get(t, [])
                c_name = cat_of.get(t, "其他")
                new_categories.setdefault(c_name, [])
                if t not in new_categories[c_name]:
                    new_categories[c_name].append(t)
    # 自动写库并清缓存
    lib_out: dict = {"version": int(current.get("version") or 1) + 1}
    if new_categories:
        lib_out["categories"] = new_categories
    if locked:
        lib_out["locked"] = locked
    lib_out["tags"] = new_tags
    _TAG_LIB_PATH.write_text(
        json.dumps(lib_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    global _TAG_LIB, _TAG_LIB_MTIME
    _TAG_LIB = None
    _TAG_LIB_MTIME = None
    scrub = scrub_all_tags()
    return {"status": "ok", "old_count": len(current_tags), "new_tags": list(new_tags), "scrub": scrub}


def _write_tag_lib(tags: dict, categories: dict, locked: list | None = None) -> None:
    """把标准标签库写回磁盘（保留分类层级与锁定标签）并清缓存"""
    cur = load_tag_library()
    out: dict = {"version": int(cur.get("version") or 1) + 1}
    if categories:
        out["categories"] = categories
    kept = locked if locked is not None else list(cur.get("locked") or [])
    if kept:
        out["locked"] = kept
    out["tags"] = tags
    _TAG_LIB_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    global _TAG_LIB, _TAG_LIB_MTIME
    _TAG_LIB = None
    _TAG_LIB_MTIME = None


def add_tag(tag: str, aliases: list[str] | None = None, category: str = "其他") -> dict:
    """新增一个标准标签（可带别名与分类），写入标签库"""
    tag = (tag or "").strip()
    if not tag:
        return {"status": "error", "message": "标签名不能为空"}
    cat = (category or "其他").strip() or "其他"
    cur = load_tag_library()
    tags = cur["tags"]
    cats = cur.get("categories") or {}
    if tag in tags:
        return {"status": "error", "message": f"标准标签「{tag}」已存在"}
    tags[tag] = [a.strip() for a in (aliases or []) if a and a.strip() and a.strip() != tag]
    cats.setdefault(cat, [])
    if tag not in cats[cat]:
        cats[cat].append(tag)
    _write_tag_lib(tags, cats)
    return {"status": "ok", "message": f"已新增标准标签「{tag}」（分类：{cat}）"}


def remove_tag(tag: str) -> dict:
    """删除一个标准标签（从 tags 及所有分类中移除）"""
    tag = (tag or "").strip()
    cur = load_tag_library()
    tags = cur["tags"]
    cats = cur.get("categories") or {}
    if tag not in tags:
        return {"status": "error", "message": f"标准标签「{tag}」不存在"}
    tags.pop(tag, None)
    for name in list(cats):
        cats[name] = [t for t in cats[name] if t != tag]
        if not cats[name]:
            cats.pop(name, None)
    locked = [t for t in (cur.get("locked") or []) if t != tag]
    _write_tag_lib(tags, cats, locked)
    return {"status": "ok", "message": f"已删除标准标签「{tag}」"}


def update_tag(tag: str, aliases: list[str] | None = None, category: str | None = None, locked: bool | None = None) -> dict:
    """编辑标准标签：修改别名、所属分类、锁定状态"""
    tag = (tag or "").strip()
    cur = load_tag_library()
    tags = cur["tags"]
    if tag not in tags:
        return {"status": "error", "message": f"标准标签「{tag}」不存在"}
    cats = cur.get("categories") or {}
    locked_list = list(cur.get("locked") or [])
    if aliases is not None:
        tags[tag] = [a.strip() for a in aliases if a and a.strip() and a.strip() != tag]
    if category is not None and str(category).strip():
        new_cat = str(category).strip()
        for name in list(cats):
            cats[name] = [t for t in cats[name] if t != tag]
            if not cats[name]:
                cats.pop(name, None)
        cats.setdefault(new_cat, [])
        if tag not in cats[new_cat]:
            cats[new_cat].append(tag)
    if locked is True and tag not in locked_list:
        locked_list.append(tag)
    elif locked is False and tag in locked_list:
        locked_list.remove(tag)
    _write_tag_lib(tags, cats, locked_list)
    extra = "（已锁定，更新标签库时保持不变）" if locked is True else ""
    return {"status": "ok", "message": f"已更新标准标签「{tag}」{extra}"}


def quality_ai_check(llm_client, groups: list[dict]) -> list[dict]:
    """对规则扫描出的候选景点对，用大模型判断关系并自主决定主/子方向。

    规则只标注『两个景点名疑似重复或一层包含」，不预设主/子顺序；由模型判断它们是否同一实体或上下位，
    并把更通用、更知名的一方放在 main（例如主用『西安博物院』，子用『西安博物院小雁塔园区（含荐福寺）』）。
    返回增强后的候选组（每条附带 ai_judgment / ai_reason / ai_action，并会按模型判断调整 main/sub），
    最终仍由人工确认应用。第 i 条结果与输入第 i 条按顺序一一对应。
    """
    if not groups:
        return []
    parts = "\n".join(f"{i + 1}. A：{g.get('main')}；B：{g.get('sub')}" for i, g in enumerate(groups))
    system_prompt = (
        "你是中国旅游知识库校对助手。下面是若干『两个景点名 A 与 B』，它们疑似重复、或其中一个包含/覆盖另一个。请逐个判断：\n"
        "1. 先确认 A、B 是否实为同一景点（如『西安博物院』与『西安博物院小雁塔园区（含荐福寺）』是同一景点的不同表述），或存在上下位/覆盖关系；\n"
        "2. judgment 仅限：duplicate（重复，同一景点）、sub（上下位/覆盖）、independent（两个都独立）、unrelated（无关，保留两者）；\n"
        "3. 当需要保留一个作为主条目时，把更通用、更知名、更简洁的名字放在 main，另一个放在 sub（例如主用『西安博物院』，子用『西安博物院小雁塔园区（含荐福寺）』，不要反过来）；\n"
        "4. suggested_action：duplicate/sub → merge（把 sub 合并进 main 并删除 sub），independent/unrelated → keep；\n"
        "5. 输出 JSON，字段 items 为数组，每项形如 {\"main\":\"主名\",\"sub\":\"子名\",\"judgment\":\"duplicate\",\"reasoning\":\"简短理由\",\"suggested_action\":\"merge\"}，条数与输入一致且顺序一致，main/sub 必须使用我给出的原名。"
    )
    result = llm_client._generate_structured(
        schema=QualityAiOutput,
        system_prompt=system_prompt,
        user_prompt=parts,
        retry_hints=["Return JSON only.", f"items must contain exactly {len(groups)} entries, in the same order as input."],
    )
    if not result or not result.items:
        return [dict(g, ai_judgment="", ai_reason="AI 判定失败", ai_action="keep") for g in groups]
    out: list[dict] = []
    for i, g in enumerate(groups):
        it = result.items[i] if i < len(result.items) else None
        if it is None:
            out.append(dict(g, ai_judgment="", ai_reason="AI 判定失败", ai_action="keep"))
            continue
        main = (it.main or "").strip() or g.get("main")
        sub = (it.sub or "").strip() or g.get("sub")
        out.append(
            dict(
                g,
                main=main,
                sub=sub,
                ai_judgment=(it.judgment or "").strip(),
                ai_reason=(it.reasoning or "").strip(),
                ai_action=(it.suggested_action or "keep").strip(),
            )
        )
    return out


def quality_ai_scan(llm_client, city: str) -> list[dict]:
    """用大模型对某城市**每个景点**逐一分析：识别低质量景点（简介空/重复/疑似营销/非景点），
    以及『子景点被大景点包含』『名称重复』等情况；只返回需处置的候选，由人工确认后应用。"""
    doc = load_city_doc(str(city or "").strip())
    if not doc:
        return []
    spots = doc.get("spots") or []
    if not spots:
        return []
    lines: list[str] = []
    for i, s in enumerate(spots):
        name = str(s.get("name") or "").strip() or f"景点{i + 1}"
        area = str(s.get("area") or "").strip()
        reason = str(s.get("reason") or "").strip()[:200]
        lines.append(f"{i}. 名称：{name}；区域：{area or '无'}；简介：{reason or '无'}")
    system_prompt = (
        "你是中国旅游知识库的景点数据质检助手。下面给出某城市全部景点（含名称/区域/简介）。\n"
        "请对每个景点逐一分析，找出：\n"
        "1. 低质量/需删除的景点：简介几乎为空、与其他景点重复、疑似广告营销词、并非真实景点的条目；\n"
        "2. 子景点被包含：一个景点是另一个景点的下属/园区/更细划分（如『西安博物院』与『西安博物院小雁塔园区』），"
        "应把通用、更知名、更简洁的一方作 main，被包含者作 sub 并合并；\n"
        "3. 名称重复：同一景点的不同表述，保留更知名简洁的一个作 main。\n"
        "只输出需要处置的项；正常、独立、无关的都不要输出。\n"
        "输出 JSON，字段 items 为数组，每项形如 "
        "{\"main\":\"保留的景点原文名\",\"sub\":\"子/重复/待删的景点原文名\",\"judgment\":\"low_quality|child|duplicate\","
        "\"reasoning\":\"简短理由\",\"suggested_action\":\"delete|merge|keep\"}。\n"
        "- 低质量/无包含关系 → main 与 sub 都填该景点名，suggested_action 用 delete；\n"
        "- child/duplicate → main 填保留项，sub 填被合并/删除项，suggested_action 用 merge。\n"
        "main/sub 必须使用我给出的景点原文名称，便于回填。"
    )
    result = llm_client._generate_structured(
        schema=QualityAiOutput,
        system_prompt=system_prompt,
        user_prompt=f"城市：{city}\n全部景点：\n" + "\n".join(lines),
        retry_hints=["Return JSON only.", "items must use the original spot names for main/sub."],
    )
    groups: list[dict] = []
    if not result or not result.items:
        return groups
    for it in result.items:
        main = str(it.main or "").strip()
        sub = str(it.sub or "").strip()
        judgment = (it.judgment or "").strip().lower()
        action = (it.suggested_action or "").strip().lower() or "keep"
        if not main and not sub:
            continue
        if not sub:
            sub = main
        groups.append(
            {
                "city": str(city).strip(),
                "main": main,
                "sub": sub,
                "kind": judgment,
                "reason": "",
                "ai_judgment": judgment,
                "ai_reason": str(it.reasoning or "").strip(),
                "ai_action": action,
            }
        )
    return groups


def quality_ai_scan_all(llm_client) -> dict:
    """对知识库中所有城市逐一做逐景点大模型质检，汇总返回全部需处置的候选"""
    cities: list[str] = []
    for path in find_doc_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        c = str(doc.get("city") or path.stem).strip()
        if c and (doc.get("spots") or []):
            cities.append(c)
    groups: list[dict] = []
    for c in cities:
        groups.extend(quality_ai_scan(llm_client, c))
    return {"status": "ok", "groups": groups, "cities": len(cities), "issues": len(groups)}


def _spot_key(name: str) -> str:
    """景点名规范化主键：忽略空格与「景区/公园/风景区」等常见后缀，用于同景去重"""
    return str(name).replace(" ", "").replace("景区", "").replace("公园", "").replace("风景区", "")


def _existing_spot_keys(city: str) -> tuple[list[str], set[str]]:
    """读取某城市已入库景点：返回(原始景点名列表, 规范化 key 集合)，用于 AI 扩增去重"""
    names: list[str] = []
    keys: set[str] = set()
    doc = load_city_doc(city)
    if not doc:
        return names, keys
    for s in doc.get("spots") or []:
        name = str(s.get("name") or "").strip()
        if name:
            names.append(name)
            keys.add(_spot_key(name))
    return names, keys


def _clean_spot_list(city: str, spots: list[dict], skip_keys: set[str] | None = None) -> tuple[list[dict], int]:
    """清洗低质量景点：按名称去重、剔除缺失/待核实项；skip_keys 内的景点（已存在或已生成）一并跳过。

    只剔除真正无价值的项（缺名称 / 重复 / 待核实），不因缺少 reason 或 tags 而丢弃，
    缺失的字段用兜底值补充，以免 AI 生成数量被过度清洗。返回（清洗后列表, 剔除数量）。
    """
    cleaned: list[dict] = []
    seen: set[str] = set()
    skip = skip_keys or set()
    dropped = 0
    for raw in spots:
        name = str(raw.get("name") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        tags = clean_tags(raw.get("tags") or [])
        if not name:
            dropped += 1
            continue
        key = _spot_key(name)
        if key in seen or key in skip:
            dropped += 1
            continue
        if reason.startswith("待核实") or reason.startswith("信息待核实") or "不确定" in reason[:20]:
            dropped += 1
            continue
        seen.add(key)
        area = str(raw.get("area") or "").strip() or city
        # 缺失 reason/tags 时用兜底值，避免此地直接被清洗掉导致数量骤减
        if not reason:
            reason = f"位于{area}，是{city}常被推荐的游玩景点。"
        if not tags:
            tags = ["经典"]
        cleaned.append({
            "name": name,
            "province": str(raw.get("province") or "").strip(),
            "area": area,
            "duration": safe_float(raw.get("estimated_visit_duration_hours")) or 2.0,
            "reason": reason,
            "tags": tags[:6],
        })
    return cleaned, dropped


def _llm_generate_batch(llm_client, city: str, hint: str, target: int, exclude_names: set[str]) -> AttractionCityAiOutput | None:
    """调用模型生成某一批景点；排除 exclude_names 中已有/已生成的景点避免重复"""
    if target < 1:
        return None
    system_prompt = (
        "你是一名专业的中国旅游景点数据整理助手。根据用户提供的城市，"
        f"推荐该城市一批真实存在、质量高、值得游玩的景点（约 {target} 个），输出结构化 JSON。\n"
        "清洗要求（重要）：\n"
        "1. 只保留真实存在、有游玩价值的景点；剔除尚未开发、信息不实、名不副实的低质量景点；\n"
        "2. 不要重复列出同一景点（如「东湖」与「东湖风景区」视为同一，只保留一个），也不要输出我已提供的已有景点；\n"
        "3. 对每个景点输出：name / province / area / estimated_visit_duration_hours / reason / tags；\n"
        "4. province、area 使用真实行政区划（如 湖北 / 武汉市武昌区）；\n"
        "5. estimated_visit_duration_hours 为建议游玩时长（小时，数字）；\n"
        "6. reason 为简介与推荐理由（40~100 字，客观真实）；\n"
    ) + (
        f"7. tags 必须从以下标准标签中选取（数量 1~4 个，只能选下列标签，不要自创）：\n{', '.join(get_standard_tags())}\n"
        "8. 若个别景点你无法确认真实性，请直接剔除，不要输出。"
    )
    user_prompt = f"城市：{city}\n请务必生成约 {target} 个景点；若确实无法凑足，宁缺毋滥，只输出真实且不重复的景点。"
    if hint:
        user_prompt += f"\n偏好/补充说明：{hint}"
    if exclude_names:
        user_prompt += "\n以下景点已存在或已生成，请不要再重复输出：\n" + "、".join(sorted(exclude_names))
    return llm_client._generate_structured(
        schema=AttractionCityAiOutput,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        retry_hints=[
            "Return JSON only.",
            "spots must be a JSON array of real high-quality attractions.",
            "Do not repeat the attractions listed as already existing.",
            "province/area must be real Chinese administrative divisions.",
            "estimated_visit_duration_hours must be a number (hours).",
            "Do not include unverified or low-quality spots.",
        ],
    )


def ai_generate_city_spots(city: str, hint: str = "", count: int | None = None) -> dict:
    """用 AI 推荐某城市的一批高质量景点（结构化 JSON）。

    - 扩增去重：先读取该城市已有景点，生成时排除它们，避免重复出现；
    - 数量补全：当目标数量不足时`分多轮补充`，尽量逼近用户期望的数量。
    """
    from app.infrastructure.llm_client import get_llm_client

    city = str(city or "").strip()
    if not city:
        return {"status": "error", "message": "请填写城市"}
    # 目标数量：未指定默认 20；指定时按用户请求数量来，仅作 100 的安全上限防止异常输入
    if count is None:
        target = 20
    else:
        target = max(1, min(int(count), 100))

    # 已存在景点（扩增去重）：原始名用于提示模型，规范化 key 用于清洗剔除
    existing_names, existing_keys = _existing_spot_keys(city)
    collected: list[dict] = []
    used_keys: set[str] = set(existing_keys)
    excluded_names: set[str] = set(existing_names)
    dropped_total = 0

    llm_client = get_llm_client()
    # 多轮补全：每轮生成剩余数量；去重后若还有缺口则继续补充，
    # 直到达到目标数量，或某轮没有任何新的有效景点（说明该城市确实凑不出更多）为止
    max_rounds = 10
    for _ in range(max_rounds):
        remaining = target - len(collected)
        if remaining <= 0:
            break
        # 每轮多要一点余量，弥补模型产出中的低质量/重复项被清洗掉的损耗
        ask = min(remaining + 4, 100)
        result = _llm_generate_batch(llm_client, city, hint, ask, excluded_names)
        if result is None:
            break
        raw = result.model_dump().get("spots") or []
        cleaned, dropped = _clean_spot_list(city, raw, skip_keys=used_keys)
        dropped_total += dropped
        lib = load_tag_library()
        for s in cleaned:
            # AI 生成的标签归一到标准标签库，避免产生库外脏标签
            s["tags"] = clean_tags(s.get("tags") or [], lib)
            used_keys.add(_spot_key(s["name"]))
            excluded_names.add(s["name"])
        collected.extend(cleaned)
        if not cleaned:
            break  # 本轮没有任何新的有效景点，说明该城市较高概率确实凑不出更多，停止补全

    return {
        "status": "ok",
        "city": city,
        "spots": collected,
        "total": len(collected) + dropped_total,
        "dropped": dropped_total,
        "excluded_existing": len(existing_names),
        "json_text": _pretty_dumps({"city": city, "spots": collected}, indent=2),
    }


# ---------- 整体重建（手动/定时 reindex） ----------


def find_doc_paths() -> list[Path]:
    """识别 data/attraction/*.json 与 data/attraction/*/*.json 两种布局"""
    if not DATA_DIR.exists():
        return []
    paths = list(DATA_DIR.glob("*.json"))
    for sub in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        paths.extend(sub.glob("*.json"))
    return sorted(set(paths))


def reindex_all() -> tuple[dict[str, int], int]:
    """逐城市整体重导全部景点文档。返回 (cities: 城市->点数, total)，无文档时抛 RuntimeError"""
    doc_paths = find_doc_paths()
    if not doc_paths:
        raise RuntimeError("未找到景点文档（data/attraction/*.json）")
    cities: dict[str, int] = {}
    total = 0
    for path in doc_paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("kb reindex parse fail path=%s err=%s", path, exc)
            continue
        city = str(doc.get("city") or "").strip() or path.stem
        count = ingest_city(
            city,
            doc.get("spots", []),
            province=str(doc.get("province") or "").strip(),
        )
        if count:
            cities[city] = count
            total += count
    return cities, total


# ---------- 全局长任务（后台执行，供页面刷新后恢复运行态） ----------
# clean_all  = 更新知识库所有城市景点标签；quality_all = 检测知识库所有城市景点质检
_task_lock = threading.Lock()
_tasks = {
    "clean_all": {"running": False, "state": "idle", "message": "", "error": None,
                  "started_at": None, "finished_at": None,
                  "total_cities": 0, "done_cities": 0, "changed_spots": 0},
    "quality_all": {"running": False, "state": "idle", "message": "", "error": None,
                    "started_at": None, "finished_at": None,
                    "total_cities": 0, "done_cities": 0, "issues": 0, "groups": []},
    "clean_city": {"running": False, "state": "idle", "message": "", "error": None,
                   "started_at": None, "finished_at": None, "scope": "", "changed_spots": 0},
    "quality_city": {"running": False, "state": "idle", "message": "", "error": None,
                     "started_at": None, "finished_at": None, "scope": "", "issues": 0, "groups": []},
    "ai_generate": {"running": False, "state": "idle", "message": "", "error": None,
                    "started_at": None, "finished_at": None, "scope": "", "result": None},
}


def global_task_status() -> dict:
    """返回两个后台长任务的当前状态与进度（用于前台刷新后恢复运行态）"""
    with _task_lock:
        return {k: dict(v) for k, v in _tasks.items()}


def _cities_with_spots(province: str = "") -> list[str]:
    """返回含景点的城市列表；province 非空时仅返回该省份的城市。"""
    province = str(province or "").strip()
    cities: list[str] = []
    seen: set[str] = set()
    for path in find_doc_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        p = str(doc.get("province") or "").strip()
        if province and p != province:
            continue
        c = str(doc.get("city") or path.stem).strip()
        if c and c not in seen and (doc.get("spots") or []):
            seen.add(c)
            cities.append(c)
    return cities


def _task_begin(key, **fields) -> None:
    """将某个后台任务置为运行态并写入任务名附加字段。"""
    with _task_lock:
        _tasks[key].update(running=True, state="running",
                           started_at=time.time(), finished_at=None, error=None, **fields)


def _task_finish(key, err=None, **fields) -> None:
    """结束某个后台任务：err 为空记 done / 有 err 记 error，并写入附加结果字段。"""
    with _task_lock:
        _tasks[key].update(running=False, finished_at=time.time(),
                           state="done" if not err else "error", error=err, **fields)


def _start_task(key, target, *args) -> bool:
    """后台启动某个任务：已在运行返回 False，否则起线程并返回 True。"""
    with _task_lock:
        if _tasks[key]["running"]:
            return False
    threading.Thread(target=target, args=args, daemon=True).start()
    return True


def _run_clean_all() -> None:
    cities = _cities_with_spots()
    total = len(cities)
    changed = 0
    _task_begin("clean_all", total_cities=total, done_cities=0, changed_spots=0)
    errors: list[str] = []
    for idx, c in enumerate(cities, 1):
        try:
            r = scrub_city_tags(c)
            if r.get("status") == "ok":
                changed += int(r.get("changed_spots") or 0)
            else:
                errors.append(f"{c}: {r.get('message') or '失败'}")
        except Exception as exc:
            logger.exception("clean-all fail city=%s", c)
            errors.append(f"{c}: {exc}")
        with _task_lock:
            _tasks["clean_all"].update(done_cities=idx, changed_spots=changed)
    _task_finish("clean_all",
                 err="；".join(errors[:10]) or None,
                 state="done_with_errors" if errors else "done",
                 changed_spots=changed)


def start_clean_all_tags() -> bool:
    """后台启动『更新所有城市标签』。已在运行返回 False，否则启动线程并返回 True。"""
    return _start_task("clean_all", _run_clean_all)


def _run_quality_all(cities: list[str], scope_label: str = "全部") -> None:
    from app.infrastructure.llm_client import get_llm_client

    llm_client = get_llm_client()
    total = len(cities)
    groups: list[dict] = []
    _task_begin("quality_all", total_cities=total, done_cities=0, issues=0, groups=[], scope=scope_label)
    for idx, c in enumerate(cities, 1):
        try:
            groups.extend(quality_ai_scan(llm_client, c))
        except Exception as exc:
            logger.exception("quality-all fail city=%s", c)
        with _task_lock:
            _tasks["quality_all"].update(done_cities=idx, issues=len(groups), groups=list(groups))
    _task_finish("quality_all", issues=len(groups), groups=list(groups))


def start_quality_ai_all() -> bool:
    """后台启动『检测所有城市景点』。已在运行返回 False，否则启动线程并返回 True。"""
    return _start_task("quality_all", _run_quality_all, _cities_with_spots(), "全部")


def start_quality_ai_province(province: str) -> bool:
    """后台启动『检测某省份的全部城市景点』。已在运行返回 False，否则启动线程并返回 True。"""
    province = str(province or "").strip()
    cities = _cities_with_spots(province)
    if not cities:
        return False
    return _start_task("quality_all", _run_quality_all, cities, province)


def _run_clean_city(city: str) -> None:
    """后台执行『更新城市标签』（单城市），状态写入 _tasks['clean_city']"""
    _task_begin("clean_city", scope=city, changed_spots=0)
    err = None
    changed = 0
    try:
        r = scrub_city_tags(city)
        if r.get("status") == "ok":
            changed = int(r.get("changed_spots") or 0)
        else:
            err = r.get("message") or "清洗失败"
    except Exception as exc:
        logger.exception("clean-city fail city=%s", city)
        err = str(exc)
    _task_finish("clean_city", err=err, changed_spots=changed)


def start_clean_city_tags(city: str) -> bool:
    """后台启动『更新城市标签』（单城市）。已在运行返回 False，否则启动线程并返回 True。"""
    city = str(city or "").strip()
    if not city:
        return False
    return _start_task("clean_city", _run_clean_city, city)


def _run_quality_city(city: str) -> None:
    """后台执行『检测城市景点』（单城市），状态与结果写入 _tasks['quality_city']"""
    from app.infrastructure.llm_client import get_llm_client

    _task_begin("quality_city", scope=city, issues=0, groups=[])
    err = None
    groups: list[dict] = []
    try:
        groups = quality_ai_scan(get_llm_client(), city)
    except Exception as exc:
        logger.exception("quality-city fail city=%s", city)
        err = str(exc)
    _task_finish("quality_city", err=err, groups=list(groups), issues=len(groups))


def start_quality_ai_city(city: str) -> bool:
    """后台启动『检测城市景点』（单城市）。已在运行返回 False，否则启动线程并返回 True。"""
    city = str(city or "").strip()
    if not city:
        return False
    return _start_task("quality_city", _run_quality_city, city)


def _run_ai_generate(city: str, hint: str, count: int | None) -> None:
    """后台执行『AI 生成景点』（单城市），结果存入 _tasks['ai_generate']，供刷新后恢复"""
    _task_begin("ai_generate", scope=city, result=None)
    err = None
    try:
        result = ai_generate_city_spots(city, hint or "", count)
    except Exception as exc:
        logger.exception("ai-generate fail city=%s", city)
        err = str(exc)
        result = {"status": "error", "message": err}
    _task_finish("ai_generate", err=err, result=result)


def start_ai_generate(city: str, hint: str = "", count: int | None = None) -> bool:
    """后台启动『AI 生成景点』（单城市）。已在运行返回 False，否则启动线程并返回 True。"""
    city = str(city or "").strip()
    if not city:
        return False
    return _start_task("ai_generate", _run_ai_generate, city, hint or "", count)