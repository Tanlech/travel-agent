from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.agents.orchestrator import travel_orchestrator
from app.agents.schema.orchestrator import AgentRequest
from app.domain.session.repository import redis_session_repository
from app.infrastructure.redis_client import get_redis


class ChatRequest(BaseModel):
    """对话入口请求体。"""

    session_id: str | None = None
    message: str
    user_id: str | None = None
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


@app.get("/health")
def health() -> dict:
    try:
        get_redis().ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}


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
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    agent_request = AgentRequest(
        request_id=str(uuid4()),
        session_id=req.session_id,
        user_id=req.user_id,
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
    """返回前端单页应用。"""
    html_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
