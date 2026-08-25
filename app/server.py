from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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


app = FastAPI(title="Travel Agent", version="0.1.0")

# 允许前端跨域访问（开发期宽松，生产应收紧来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


@app.on_event("startup")
def _startup() -> None:
    """启动时初始化 MySQL 表并创建种子管理员（MySQL 未就绪时降级，不阻断服务）"""
    init_db()
    auth_service.seed_admin()


@app.on_event("startup")
async def _start_kb_scheduler() -> None:
    """启动 RAG 景点知识库的定时重建调度器"""
    await kb_admin_manager.start()


@app.on_event("shutdown")
async def _stop_kb_scheduler() -> None:
    await kb_admin_manager.stop()


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


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    user_id = req.user_id
    if authorization:
        # 已登录用户：令牌解析出的 user_id 优先，匿名 user_id 仅作未登录兜底
        account = auth_service.authenticate(_extract_token(authorization))
        if account is None:
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        user_id = account.user_id
        auth_service.touch_active(account.user_id)
    agent_request = AgentRequest(
        request_id=req.request_id or str(uuid4()),
        session_id=req.session_id,
        user_id=user_id,
        message=req.message,
        metadata=req.metadata or {},
    )
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
    return ChatResponse(
        session_id=resp.session_id,
        status=resp.status,
        mode=resp.mode,
        summary=resp.summary,
        follow_up_question=resp.follow_up_question,
        plan=resp.plan,
        draft=resp.draft,
        trace_id=resp.trace_id,
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
