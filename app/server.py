from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.admin.account.service import auth_service
from app.admin.knowledge_admin.manager import kb_admin_manager
from app.admin.routes import _extract_token, admin_router
from app.agent.agents.orchestrator import travel_orchestrator
from app.agent.agents.schema.orchestrator import AgentRequest
from app.agent.domain.memory.manager import memory_manager
from app.agent.domain.memory.store import memory_store
from app.agent.domain.session.repository import redis_session_repository
from app.infrastructure.mysql_client import init_db
from app.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)


def _prewarm_models() -> None:
    """启动预热：把检索重排等一次性模型成本提前加载，避免首个用户买单。失败仅记日志，不影响启动。

    重排默认关闭（rerank_model 为空）时 is_enabled() 快速返回，无副作用；开启后这里负责联网下载并加载。"""
    try:
        from app.agent.knowledge.reranker import reranker

        if reranker.is_enabled():
            logger.info("启动预热：reranker 模型已加载")
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动预热：reranker 加载失败（不影响启动）: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：MySQL 初始化 + 种子管理员（降级不阻断）→ 知识库重建调度器 → 后台预热模型
    _startup_sync()
    await _startup_scheduler()
    threading.Thread(target=_prewarm_models, daemon=True).start()
    yield
    await _shutdown_scheduler()


class ChatRequest(BaseModel):
    """对话入口请求体。"""

    session_id: str | None = None
    message: str
    user_id: str | None = None
    # 可选客户端透传的请求标识：网关/客户端对同一请求重试时复用该值，服务端据此做请求级幂等
    # （同一 request_id 二次投递直接返回首次结果，防重复规划/重复落记忆）；缺省时服务端自生成
    request_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """对话入口响应体。"""

    session_id: str
    status: str
    mode: str
    summary: str | None = None
    follow_up_question: str | None = None
    plan: dict | None = None
    draft: dict | None = None
    trace_id: str | None = None


app = FastAPI(title="Travel Agent", version="0.1.0", lifespan=lifespan)

# 允许前端跨域访问（开发期宽松，生产应收紧来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static"


def _spa_index() -> HTMLResponse:
    """返回 Vue 单页应用入口（聊天/登录/后台均由其前端路由接管）。"""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# 托管 Vite 构建产物（JS/CSS 等静态资源）
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

# 后台管理系统路由：认证（/auth/*）+ 管理后台（/admin/*）
app.include_router(admin_router)


@app.get("/health")
def health() -> dict:
    try:
        get_redis().ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}


# -------------------------- lifespan 启动/关闭 --------------------------

def _startup_sync() -> None:
    """同步启动：初始化 MySQL 表并创建种子管理员（未就绪降级，不阻断服务）"""
    try:
        init_db()
        auth_service.seed_admin()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动：MySQL 初始化失败（降级继续）: %s", exc)


async def _startup_scheduler() -> None:
    """启动 RAG 景点知识库的定时重建调度器"""
    try:
        await kb_admin_manager.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动：知识库调度器启动失败（降级继续）: %s", exc)


async def _shutdown_scheduler() -> None:
    try:
        await kb_admin_manager.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭：知识库调度器停止异常: %s", exc)


@app.get("/session/{session_id}")
def get_session(session_id: str) -> dict:
    """返回会话状态，供前端加载历史对话与累计需求。"""
    st = redis_session_repository.load(session_id)
    if st is None:
        return {"status": "not_found", "session_id": session_id}
    plan, draft = redis_session_repository.load_artifacts(session_id)
    return {
        "status": "ok",
        "session_id": session_id,
        "stage": st.conversation_stage,
        "revision_count": st.revision_count,
        "current_request": st.current_request_state.model_dump(),
        "recent_messages": [m.model_dump() for m in st.recent_messages],
        "has_plan": bool(plan),
        "has_draft": bool(draft),
        # 完整行程产物：前端恢复历史对话时据此重建行程卡（仅文本无法渲染卡片）
        "plan": plan,
        "draft": draft,
    }


@app.get("/sessions")
def list_sessions() -> dict:
    """返回全部会话摘要，供前端多会话列表服务端同步（换设备/清缓存后恢复）。"""
    return {"status": "ok", "sessions": redis_session_repository.list_summaries()}


@app.get("/memory/{user_id}")
def get_memory(user_id: str) -> dict:
    """返回用户长期记忆：偏好 + 历史行程（供前端"对话长期记忆"详情展示）。"""
    user_memory = memory_manager.load_user_memory(user_id)
    trip_memories = memory_manager.load_trip_memories(user_id)
    return {
        "status": "ok",
        "user_id": user_id,
        "user_memory": user_memory.model_dump() if user_memory else None,
        "trip_memories": [m.model_dump() for m in trip_memories],
    }


@app.delete("/session/{session_id}")
def delete_session(session_id: str) -> dict:
    """删除会话及其产物、幂等缓存。"""
    redis_session_repository.delete(session_id)
    return {"status": "ok", "session_id": session_id}


@app.get("/session/{session_id}/events")
def get_session_events(session_id: str) -> dict:
    """返回会话事件日志（user_message / assistant_reply / plan_commit，只追加）。
    前端"对话历史"可据此重建完整上下文（不依赖本地的截断消息缓存）。"""
    events = redis_session_repository.list_events(session_id)
    return {"status": "ok", "session_id": session_id, "events": events}


@app.post("/session/{session_id}/undo")
def undo_session(session_id: str) -> dict:
    """撤销最近一次行程产物变更（规划/改稿）：弹出撤销栈快照并恢复为上一版 plan/draft。
    无快照或会话不存在时返回 ok 但要提示前端无可撤销。"""
    state = redis_session_repository.load(session_id)
    if state is None:
        return {"status": "not_found", "session_id": session_id}

    # 原子判断+弹栈：避免 has_undo 与 pop 两步之间被并发请求抢走快照
    had_undo, snapshot = redis_session_repository.pop_undo_if_any(session_id)
    if not had_undo:
        return {"status": "ok", "session_id": session_id, "undone": False, "has_plan": state.artifacts.has_plan}

    redis_session_repository.restore_artifacts(session_id, snapshot)
    redis_session_repository.append_event(
        session_id,
        "plan_undo",
        {
            "restored_plan": bool(snapshot and snapshot.get("plan")),
            "restored_draft": bool(snapshot and snapshot.get("draft")),
        },
    )

    # 同步会话内嵌产物摘要标记，保证 has_plan/寻址 summary 与恢复后的产物一致
    plan, draft = redis_session_repository.load_artifacts(session_id)
    has_plan = bool(plan)
    updated = state.model_copy(
        update={
            "artifacts": state.artifacts.model_copy(
                update={
                    "has_plan": has_plan,
                    "plan_updated_at": datetime.now(timezone.utc) if has_plan else None,
                    "plan_summary": (plan or {}).get("summary") if plan else None,
                }
            )
        }
    )
    redis_session_repository.save(updated)
    return {
        "status": "ok",
        "session_id": session_id,
        "undone": True,
        "plan": plan,
        "draft": draft,
        "has_plan": bool(plan),
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    agent_request = _build_agent_request(req, authorization)
    try:
        resp = travel_orchestrator.handle(agent_request)
    except Exception as exc:
        return ChatResponse(
            session_id=req.session_id or "",
            status="error",
            mode="error",
            summary=f"服务内部错误：{exc}",
            trace_id=None,
        )
    return ChatResponse(**_chat_payload(resp))


def _build_agent_request(req: ChatRequest, authorization: str | None) -> AgentRequest:
    """解析登录身份并构造编排请求；未登录/令牌失效抛 401"""
    user_id = req.user_id
    if authorization:
        # 已登录用户：令牌解析出的 user_id 优先，匿名 user_id 仅作未登录兜底
        account = auth_service.authenticate(_extract_token(authorization))
        if account is None:
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        user_id = account.user_id
        auth_service.touch_active(account.user_id)
    return AgentRequest(
        request_id=req.request_id or str(uuid4()),
        session_id=req.session_id,
        user_id=user_id,
        message=req.message,
        metadata=req.metadata or {},
    )


def _chat_payload(resp) -> dict:
    """把编排输出映射为对外响应载荷（非流式 JSON 与流式 done 事件共用同一口径）"""
    return {
        "session_id": resp.session_id,
        "status": resp.status,
        "mode": resp.mode,
        "summary": resp.summary,
        "follow_up_question": resp.follow_up_question,
        "plan": resp.plan,
        "draft": resp.draft,
        "trace_id": resp.trace_id,
    }


def _sse_json_default(o: object):
    """SSE 事件序列化的兜底：处理 Pydantic 模型、日期/时间、Decimal/UUID 等非基本类型，
    避免 done 事件因子字段非 JSON 可序列化而在序列化阶段崩溃，导致整条流断在最后一步。"""
    if hasattr(o, "model_dump"):
        try:
            return o.model_dump()
        except Exception:  # noqa: BLE001
            return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (Decimal, UUID)):
        return str(o)
    return str(o)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, authorization: str | None = Header(default=None)) -> StreamingResponse:
    """SSE 流式对话：后台线程跑编排，边跑边推送 stage/token/ping，最后推送 done（完整响应）。

    长耗时规划期间用心跳保活并让前端展示"已等待秒数"；qa 分支的真文本逐 token 推送。
    非流式 /chat 原样保留，二者共用 _build_agent_request 与 _chat_payload 保证口径一致。
    """
    agent_request = _build_agent_request(req, authorization)
    # 跨线程进度事件队列 + 运行结束信号（非流式端点返回后编排线程即终止）
    event_queue: queue.Queue = queue.Queue()
    finished = threading.Event()
    start_time = time.monotonic()
    payload: dict = {}

    def emit(kind: str, data: dict) -> None:
        event_queue.put({"event": kind, "data": data})

    def run() -> None:
        nonlocal payload
        try:
            resp = travel_orchestrator.handle(agent_request, progress=emit)
            payload = _chat_payload(resp)
        except Exception as exc:
            logger.exception("chat_stream_handle_error")
            payload = {
                "session_id": agent_request.session_id or "",
                "status": "error",
                "mode": "error",
                "summary": f"服务内部错误：{exc}",
                "follow_up_question": None,
                "plan": None,
                "draft": None,
                "trace_id": None,
            }
        finally:
            event_queue.put({"event": "done", "data": payload})
            event_queue.put(None)  # 结束哨兵，令生成器退出
            finished.set()

    def pinger() -> None:
        # 编排期间每 3s 推心跳（finished 置位后退出），供"已等待秒数"展示与连接保活
        while not finished.wait(timeout=3.0):
            event_queue.put({"event": "ping", "data": {"elapsed": int(time.monotonic() - start_time)}})

    threading.Thread(target=pinger, daemon=True).start()
    threading.Thread(target=run, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(event_queue.get)
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False, default=_sse_json_default)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """返回 Vue 单页应用入口。"""
    return _spa_index()


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    """登录/注册页（Vue 前端路由接管）。"""
    return _spa_index()


@app.get("/admin", response_class=HTMLResponse)
def admin_index() -> HTMLResponse:
    """管理后台（Vue 前端路由接管）。"""
    return _spa_index()


@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(full_path: str) -> HTMLResponse:
    """前端 history 模式路由统一回退到单页入口。"""
    return _spa_index()
