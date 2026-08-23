"""把清洗后的景点文档导入 Qdrant（按城市隔离；文档带 province 字段时一并写入，支持省市隔离）

用法：
    python scripts/import_attraction/import_attraction.py                 # 导入 data/attraction 下全部文档
    python scripts/import_attraction/import_attraction.py 北京            # 只导入指定城市
    python scripts/import_attraction/import_attraction.py 北京 成都       # 导入多个城市

说明：
    - 自动识别 data/attraction/{city}.json 或 data/attraction/{city}/{city}.json
    - province 取文档顶层字段（如 {"city": "宜昌", "province": "湖北"}），未写则 metadata 为空
    - 同景点重复导入自动覆盖（id = 城市:景点名）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接运行（python scripts/import_attraction/import_attraction.py）时也能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.knowledge import ATTRACTION_COLLECTION, knowledge_service  # noqa: E402
from app.knowledge.ingest.attraction import ingest_city  # noqa: E402

# 数据目录：项目根 data/attraction
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "attraction"


def _find_doc_paths() -> list[Path]:
    """识别 data/attraction/*.json 与 data/attraction/*/*.json 两种布局"""
    if not OUT_DIR.exists():
        return []
    paths = list(OUT_DIR.glob("*.json"))
    for sub in sorted(p for p in OUT_DIR.iterdir() if p.is_dir()):
        paths.extend(sub.glob("*.json"))
    return sorted(set(paths))


def import_spots(city: str, spots: list[dict], province: str = "") -> int:
    """把一个城市的景点清单写入 Qdrant（复用唯一入库入口）"""
    return ingest_city(city, spots, province=province)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入清洗后的景点文档到 Qdrant")
    parser.add_argument("cities", nargs="*", help="城市列表，缺省导入全部文档")
    args = parser.parse_args()

    if args.cities:
        target_cities = set(args.cities)
        doc_paths = [p for p in _find_doc_paths() if p.stem in target_cities]
    else:
        doc_paths = _find_doc_paths()

    if not doc_paths:
        print(f"[skip] 未找到文档（目录: {OUT_DIR}）")
        return

    total = 0
    for path in doc_paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[fail] {path}: {exc}")
            continue
        city = str(doc.get("city") or "").strip() or path.stem
        province = str(doc.get("province") or "").strip()
        count = import_spots(city, doc.get("spots", []), province=province)
        if count:
            print(f"[ok] {city}: 导入 {count} 条（province: {province or '未填写'}）← {path}")
            total += count
        else:
            print(f"[skip] {city}: 无有效景点 ← {path}")

    print(f"\n导入完成：共 {total} 条")
    print(f"Qdrant attraction 集合总数: {knowledge_service.count(ATTRACTION_COLLECTION)}")


if __name__ == "__main__":
    main()
