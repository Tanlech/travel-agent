"""知识库管理调度器：触发/记录重建，为后台页面提供配置与运行态。

- 只负责"调度 + 运维"：重建触发锁、运行状态/历史、定时器、配置、概览统计
- 支持「子知识库 = 单独某个库」与「总知识库 = 全部子库集合」两种重建方式：
    - 重建任一子库只会重建它自己（清它自己的集合 + 导它自己的数据源），互不污染
    - 「全部重建」= 依次重建每个子库，仍是各自只重建自己
- 每个子库有独立的定时配置（开关 + 间隔），到点只重建自己
- 子库重建职责：
    - 景点库  attraction : attraction_kb.reindex_all，从 data/attraction 文档整库重导
    - 问答库  qa_kb     : 清空后从 data/qa 攻略文档整库重导（可先用 guide_builder 生成）
    - 对话库  chat_kb   : 运行时记忆库（无持久源），重建 = 清空后供运行时自动重新积累
- 配置/状态/历史存 Redis，后台页面可实时查看与调整
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from app.agent.knowledge import ATTRACTION_COLLECTION, knowledge_service
from app.agent.knowledge.ingest.common import CHAT_COLLECTION, QA_COLLECTION

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_CFG_KEY = "kb_admin:config"
_STATUS_KEY = "kb_admin:status"
_HISTORY_KEY = "kb_admin:history"
_LOCK_KEY = "kb_admin:lock"
_LAST_PREFIX = "kb_admin:last:"
_LOCK_TTL_SECONDS = 7200
_HISTORY_LIMIT = 30
_DEFAULT_INTERVAL_MINUTES = 1440  # 默认每天重建一次


def _rebuild_qa() -> tuple[dict, int]:
    """问答库整库重建：清空 qa_kb 后从 data/qa 攻略文档重导（只动自己）"""
    qa_dir = _PROJECT_ROOT / "data" / "qa"
    if not qa_dir.exists():
        return {"note": "data/qa 目录不存在，可先运行 guide_builder 生成攻略"}, 0
    knowledge_service.clear(QA_COLLECTION)
    n = knowledge_service.ingest_documents(QA_COLLECTION, [str(qa_dir)])
    return {"note": "data/qa 攻略文档已重导"}, n


def _rebuild_chat() -> tuple[dict, int]:
    """对话库整库重建：运行时记忆库，清空后由后续对话自动重新积累（只动自己）"""
    knowledge_service.clear(CHAT_COLLECTION)
    return {"note": "对话库已清空，后续对话将自动重新积累"}, 0


def _rebuild_attraction() -> tuple[dict, int]:
    """景点库整库重建：逐城市从 data/attraction 文档整体重导（只动自己）"""
    from app.agent.knowledge.attraction_kb import reindex_all

    return reindex_all()


# 全部可重建子库注册表：collection -> 元信息 + 专属重建函数
_KB_PROFILES: dict[str, dict] = {
    ATTRACTION_COLLECTION: {
        "label": "景点知识库",
        "icon": "🧭",
        "desc": "城市景点 RAG：从 data/attraction 文档整库重导",
        "builder": _rebuild_attraction,
    },
    QA_COLLECTION: {
        "label": "城市攻略知识库",
        "icon": "💬",
        "desc": "城市攻略库：从 data/qa 攻略文档整库重导",
        "builder": _rebuild_qa,
    },
    CHAT_COLLECTION: {
        "label": "对话知识库",
        "icon": "🧠",
        "desc": "多轮对话记忆库（运行时数据）：重建将清空后重新积累",
        "builder": _rebuild_chat,
    },
}


def _default_bases_config() -> dict:
    return {
        coll: {"enabled": False, "interval_minutes": _DEFAULT_INTERVAL_MINUTES}
        for coll in _KB_PROFILES
    }


class KnowledgeAdminManager:
    """知识库定时/手动调度与运行态管理（支持按子库独立重建与独立定时）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # ---------- Redis 辅助 ----------
    @staticmethod
    def _r():
        from app.infrastructure.redis_client import get_redis

        return get_redis()

    # ---------- 配置 ----------
    def get_config(self) -> dict:
        try:
            raw = self._r().get(_CFG_KEY)
            cfg = json.loads(raw) if raw else {}
        except Exception:
            cfg = {}
        bases = _default_bases_config()
        for coll, bc in (cfg.get("bases") or {}).items():
            if coll in bases:
                bases[coll] = {
                    "enabled": bool(bc.get("enabled")),
                    "interval_minutes": max(1, int(bc.get("interval_minutes") or _DEFAULT_INTERVAL_MINUTES)),
                }
        return {"bases": bases, "updated_at": cfg.get("updated_at") or ""}

    def update_config(self, bases: dict | None = None, enabled: bool | None = None, interval_minutes: int | None = None) -> dict:
        """更新定时配置。bases 形如 {collection: {enabled, interval_minutes}}；
        enabled / interval_minutes 为旧版全局参数，传入时将应用到全部子库。"""
        cfg = self.get_config()
        target = bases or {}
        if enabled is not None:
            for coll in cfg["bases"]:
                target.setdefault(coll, {})["enabled"] = bool(enabled)
        if interval_minutes is not None:
            for coll in cfg["bases"]:
                target.setdefault(coll, {})["interval_minutes"] = max(1, int(interval_minutes))
        for coll, bc in target.items():
            if coll not in cfg["bases"]:
                continue
            merged = dict(cfg["bases"][coll])
            if "enabled" in bc:
                merged["enabled"] = bool(bc["enabled"])
            if "interval_minutes" in bc:
                merged["interval_minutes"] = max(1, int(bc["interval_minutes"]))
            cfg["bases"][coll] = merged
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

    def _last_for(self, collection: str) -> dict:
        try:
            raw = self._r().get(_LAST_PREFIX + collection)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _set_last(self, collection: str, **fields: object) -> None:
        last = self._last_for(collection)
        last.update(fields)
        self._r().set(_LAST_PREFIX + collection, json.dumps(last, ensure_ascii=False))

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

    # ---------- 统计 ----------
    def get_stats(self) -> dict:
        """景点集合总点数 + 分城市点数 + 稠密/稀疏向量可用状态"""
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
            "dense_enabled": bool(getattr(knowledge_service, "dense_enabled", True)),
            "sparse_enabled": bool(getattr(knowledge_service, "sparse_enabled", True)),
        }

    # ---------- 知识库类型 ----------
    def get_bases(self) -> list[dict]:
        """列出全部可重建子库：元信息 + 数据量 + 各自定时配置 + 最近一次重建结果"""
        cfg = self.get_config()["bases"]
        bases: list[dict] = []
        for coll, prof in _KB_PROFILES.items():
            count = 0
            try:
                count = knowledge_service.count(coll)
            except Exception as exc:
                logger.warning("kb base count fail %s: %s", coll, exc)
            bc = cfg.get(coll, {})
            last = self._last_for(coll)
            bases.append(
                {
                    "collection": coll,
                    "label": prof["label"],
                    "icon": prof["icon"],
                    "desc": prof["desc"],
                    "count": count,
                    "enabled": bool(bc.get("enabled")),
                    "interval_minutes": bc.get("interval_minutes", _DEFAULT_INTERVAL_MINUTES),
                    "last": last,
                }
            )
        return bases

    # ---------- 重建触发 ----------
    def run_now(self, target: str | list[str] | None = None, trigger: str = "manual") -> dict:
        """立即重建。target 为单个子库 collection、子库 collection 列表；None 表示全部子库。
        每个子库只会重建自己；已有重建任务在运行则跳过。返回 {started, bases, skipped_bases, message}"""
        if target is None:
            bases = list(_KB_PROFILES.keys())
        elif isinstance(target, (list, tuple)):
            bases = [c for c in target if c in _KB_PROFILES]
        else:
            bases = [target] if target in _KB_PROFILES else []

        if not bases:
            return {"started": False, "bases": [], "skipped_bases": [], "message": "没有可重建的知识库"}
        run_id = uuid.uuid4().hex
        acquired = self._r().set(_LOCK_KEY, run_id, nx=True, ex=_LOCK_TTL_SECONDS)
        if not acquired:
            return {"started": False, "bases": bases, "skipped_bases": bases, "message": "已有重建任务正在运行，请稍候"}
        labels = [_KB_PROFILES[c]["label"] for c in bases]
        self._set_status(
            state="running",
            trigger=trigger,
            started_at=time.time(),
            finished_at=None,
            duration_ms=None,
            total=0,
            error=None,
            bases={c: {"state": "running", "total": 0, "error": None, "detail": {}} for c in bases},
        )
        logger.info("kb rebuild start trigger=%s run_id=%s bases=%s", trigger, run_id, labels)
        thread = threading.Thread(target=self._worker, args=(run_id, trigger, bases), daemon=True)
        thread.start()
        return {"started": True, "bases": bases, "skipped_bases": [], "message": f"{('、'.join(labels)) or '知识库'} 重建已启动"}

    def _worker(self, run_id: str, trigger: str, bases: list[str]) -> None:
        """后台线程：顺序重建目标列表中的每个子库（每个子库只重建自己）"""
        started = time.time()
        total = 0
        results: dict[str, dict] = {}
        overall_error: str | None = None
        failed = False
        for coll in bases:
            prof = _KB_PROFILES.get(coll)
            if not prof:
                continue
            state = "success"
            error: str | None = None
            count = 0
            detail: dict = {}
            try:
                detail, count = prof["builder"]()
            except Exception as exc:
                logger.exception("kb rebuild failed coll=%s run_id=%s", coll, run_id)
                state = "failed"
                error = str(exc)
                failed = True
            finally:
                total += count
                results[coll] = {"state": state, "total": count, "error": error, "detail": detail}
                self._set_last(
                    coll,
                    state=state,
                    total=count,
                    error=error,
                    started_at=started,
                    finished_at=time.time(),
                    trigger=trigger,
                )
        duration_ms = int((time.time() - started) * 1000)
        overall_state = "failed" if failed else "success"
        if failed:
            overall_error = "部分子库重建失败"
        self._set_status(
            state=overall_state,
            finished_at=time.time(),
            duration_ms=duration_ms,
            total=total,
            error=overall_error,
            bases=results,
        )
        self._push_history(
            {
                "trigger": trigger,
                "state": overall_state,
                "started_at": started,
                "finished_at": time.time(),
                "duration_ms": duration_ms,
                "total": total,
                "bases": results,
                "error": overall_error,
            }
        )
        self._r().delete(_LOCK_KEY)

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
        """逐子库判断是否到点：到点才把该子库加入本次重建，每个子库只重建自己"""
        cfg = self.get_config()["bases"]
        if self.get_status().get("state") == "running":
            return
        now = time.time()
        due: list[str] = []
        for coll, bc in cfg.items():
            if not bc.get("enabled"):
                continue
            last = self._last_for(coll).get("finished_at") or 0
            interval = int(bc.get("interval_minutes") or _DEFAULT_INTERVAL_MINUTES) * 60
            if not last or (now - last) >= interval:
                due.append(coll)
        if due:
            self.run_now(target=due, trigger="schedule")


kb_admin_manager = KnowledgeAdminManager()