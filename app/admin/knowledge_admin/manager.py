"""RAG 景点知识库管理：定时重建 + 手动重建 + 运行状态/历史

- 数据源：data/attraction/*.json（与 scripts/import_attraction 一致）
- 重建 = 逐城市 ingest_city（先清该城市旧点再整体重导，避免脏数据残留）
- 并发保护：Redis 分布式锁，定时与手动重建互斥
- 配置/状态/历史存 Redis，后台页面可实时查看与调整
- 调度：服务启动后由独立 asyncio 后台任务每分钟检查一次到点即触发
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from app.infrastructure.conversions import safe_float
from app.infrastructure.redis_client import get_redis
from app.agent.knowledge import ATTRACTION_COLLECTION, knowledge_service
from app.agent.knowledge.embedder import sparse_text_embedder, text_embedder
from app.agent.knowledge.ingest.attraction import ingest_city
from app.agent.knowledge.ingest.common import CHAT_COLLECTION, QA_COLLECTION

logger = logging.getLogger(__name__)

_CFG_KEY = "kb_admin:config"
_STATUS_KEY = "kb_admin:status"
_HISTORY_KEY = "kb_admin:history"
_LOCK_KEY = "kb_admin:lock"
_LOCK_TTL_SECONDS = 3600
_HISTORY_LIMIT = 20
_DEFAULT_INTERVAL_MINUTES = 1440  # 默认每天重建一次

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "attraction"

# 全部知识库类型注册表：后台据此区分多个知识库
_KB_TYPES = [
    {"collection": ATTRACTION_COLLECTION, "label": "景点知识库", "desc": "城市景点 RAG（可按地点管理景点）"},
    {"collection": QA_COLLECTION, "label": "问答知识库", "desc": "QA 知识问答库"},
    {"collection": CHAT_COLLECTION, "label": "对话知识库", "desc": "多轮对话记忆库"},
]


class KnowledgeAdminManager:
    """景点知识库的定时/手动重建与运行态管理。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # ---------- Redis 辅助 ----------
    @staticmethod
    def _r():
        return get_redis()

    # ---------- 配置 ----------
    def get_config(self) -> dict:
        try:
            raw = self._r().get(_CFG_KEY)
            cfg = json.loads(raw) if raw else {}
        except Exception:
            cfg = {}
        return {
            "enabled": bool(cfg.get("enabled")),
            "interval_minutes": int(cfg.get("interval_minutes") or _DEFAULT_INTERVAL_MINUTES),
            "updated_at": cfg.get("updated_at") or "",
        }

    def update_config(self, enabled: bool | None = None, interval_minutes: int | None = None) -> dict:
        cfg = self.get_config()
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if interval_minutes is not None:
            cfg["interval_minutes"] = max(1, int(interval_minutes))
        cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._r().set(_CFG_KEY, json.dumps(cfg, ensure_ascii=False))
        return cfg

    # ---------- 运行状态 / 历史 ----------
    def get_status(self) -> dict:
        try:
            raw = self._r().get(_STATUS_KEY)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _set_status(self, **fields: object) -> None:
        status = self.get_status()
        status.update(fields)
        self._r().set(_STATUS_KEY, json.dumps(status, ensure_ascii=False))

    def get_history(self) -> list[dict]:
        history: list[dict] = []
        for item in self._r().lrange(_HISTORY_KEY, 0, _HISTORY_LIMIT - 1) or []:
            try:
                history.append(json.loads(item))
            except Exception:
                continue
        return history

    def _push_history(self, entry: dict) -> None:
        r = self._r()
        r.lpush(_HISTORY_KEY, json.dumps(entry, ensure_ascii=False))
        r.ltrim(_HISTORY_KEY, 0, _HISTORY_LIMIT - 1)

    # ---------- 重建触发 ----------
    def run_now(self, trigger: str = "manual") -> dict:
        """触发一次知识库重建；已有任务在运行则跳过。返回 {started, message}"""
        run_id = uuid.uuid4().hex
        acquired = self._r().set(_LOCK_KEY, run_id, nx=True, ex=_LOCK_TTL_SECONDS)
        if not acquired:
            return {"started": False, "message": "已有重建任务正在运行，请稍候"}
        self._set_status(
            state="running",
            trigger=trigger,
            started_at=time.time(),
            finished_at=None,
            duration_ms=None,
            total=0,
            cities={},
            error=None,
        )
        logger.info("kb reindex start trigger=%s run_id=%s", trigger, run_id)
        thread = threading.Thread(target=self._worker, args=(run_id, trigger), daemon=True)
        thread.start()
        return {"started": True, "message": "知识库重建已启动"}

    def _worker(self, run_id: str, trigger: str) -> None:
        """同步重建工作线程：逐城市重导并记录结果（独立线程避免阻塞事件循环）"""
        started = time.time()
        cities: dict[str, int] = {}
        total = 0
        state = "success"
        error: str | None = None
        try:
            doc_paths = self._find_doc_paths()
            if not doc_paths:
                raise RuntimeError("未找到景点文档（data/attraction/*.json）")
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
        except Exception as exc:
            logger.exception("kb reindex failed run_id=%s", run_id)
            state = "failed"
            error = str(exc)
        finally:
            duration_ms = int((time.time() - started) * 1000)
            self._set_status(
                state=state,
                finished_at=time.time(),
                duration_ms=duration_ms,
                total=total,
                cities=cities,
                error=error,
            )
            self._push_history(
                {
                    "trigger": trigger,
                    "state": state,
                    "started_at": started,
                    "finished_at": time.time(),
                    "duration_ms": duration_ms,
                    "total": total,
                    "cities_count": len(cities),
                    "error": error,
                }
            )
            self._r().delete(_LOCK_KEY)

    # ---------- 统计 ----------
    def get_stats(self) -> dict:
        """集合总点数 + 分城市点数 + 稠密/稀疏向量可用状态"""
        by_city: dict[str, int] = {}
        total = 0
        try:
            for item in knowledge_service.get_all(ATTRACTION_COLLECTION):
                city = (item.metadata or {}).get("city") or "未知"
                by_city[city] = by_city.get(city, 0) + 1
            total = sum(by_city.values())
        except Exception as exc:
            logger.warning("kb stats failed: %s", exc)
        return {
            "collection": ATTRACTION_COLLECTION,
            "total": total,
            "cities": dict(sorted(by_city.items(), key=lambda kv: kv[1], reverse=True)),
            "dense_enabled": bool(text_embedder.is_enabled()),
            "sparse_enabled": bool(sparse_text_embedder.is_enabled()),
        }

    # ---------- 知识库类型 / 地点 / 景点 CRUD ----------
    def get_bases(self) -> list[dict]:
        """列出全部知识库类型及其数据量，供后台区分多种数据库"""
        bases: list[dict] = []
        for kb in _KB_TYPES:
            count = 0
            try:
                count = knowledge_service.count(kb["collection"])
            except Exception as exc:
                logger.warning("kb base count fail %s: %s", kb["collection"], exc)
            bases.append({**kb, "count": count})
        return bases

    def list_cities(self) -> list[dict]:
        """按地点聚合景点数量与省份（景点知识库）"""
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

    def list_spots(self, city: str) -> list[dict]:
        """查询某地点下包含的全部景点"""
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

    def create_spot(self, data: dict) -> bool:
        """在某地点新建景点（写 json + 整体重导）；重名/缺字段返回 False"""
        from app.agent.knowledge.ingest.attraction import add_spot

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

    def delete_spot(self, city: str, name: str) -> bool:
        from app.agent.knowledge.ingest.attraction import remove_spot

        return remove_spot(str(city or "").strip(), str(name or "").strip())

    def delete_city(self, city: str) -> bool:
        from app.agent.knowledge.ingest.attraction import remove_city

        return remove_city(str(city or "").strip())

    # ---------- 定时调度 ----------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._scheduler_loop())
            logger.info("kb admin scheduler started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                self._maybe_trigger_scheduled()
            except Exception:
                logger.exception("kb scheduler tick error")
            await asyncio.sleep(60)

    def _maybe_trigger_scheduled(self) -> None:
        cfg = self.get_config()
        if not cfg.get("enabled"):
            return
        if self.get_status().get("state") == "running":
            return
        last = self.get_status().get("finished_at") or self.get_status().get("started_at") or 0
        interval_seconds = int(cfg.get("interval_minutes") or _DEFAULT_INTERVAL_MINUTES) * 60
        if not last or (time.time() - last) >= interval_seconds:
            self.run_now(trigger="schedule")

    @staticmethod
    def _find_doc_paths() -> list[Path]:
        """识别 data/attraction/*.json 与 data/attraction/*/*.json 两种布局"""
        if not _DATA_DIR.exists():
            return []
        paths = list(_DATA_DIR.glob("*.json"))
        for sub in sorted(p for p in _DATA_DIR.iterdir() if p.is_dir()):
            paths.extend(sub.glob("*.json"))
        return sorted(set(paths))


kb_admin_manager = KnowledgeAdminManager()
