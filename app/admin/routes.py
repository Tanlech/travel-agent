"""后台管理系统路由：认证（/auth/*） + 管理后台（/admin/*）

- 认证：注册 / 登录 / 登出 / 当前用户
- 用户管理：列表 / 详情 / 启停 / 删除
- 知识库管理：定时配置 / 手动重建 / 多知识库类型 / 地点·景点 CRUD
- 依赖：require_admin 鉴权由 server 主应用挂载本路由时统一生效
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.admin.account.schema import UserAccount
from app.admin.account.service import auth_service
from app.admin.knowledge_admin.manager import kb_admin_manager
from app.agent.domain.memory.manager import memory_manager
from app.agent.domain.memory.store import memory_store
from app.agent.domain.session.repository import redis_session_repository

admin_router = APIRouter()


class RegisterRequest(BaseModel):
    """注册请求体。"""

    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


class AdminStatusBody(BaseModel):
    """后台用户状态修改请求体。"""

    status: Literal["active", "disabled"]


class KbConfigBody(BaseModel):
    """景点知识库定时任务配置请求体。"""

    enabled: bool | None = None
    interval_minutes: int | None = None


class AttractionSpotBody(BaseModel):
    """景点知识库·新建景点请求体。"""

    city: str
    name: str
    province: str | None = None
    area: str | None = None
    duration: float | None = None
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)


# ===================== 认证依赖 =====================
def _extract_token(authorization: str | None) -> str | None:
    """从 Authorization 头解析 Bearer 令牌；缺失/格式错误返回 None"""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token else None


def _public_account(account: UserAccount) -> dict:
    """对外输出时剔除密码哈希"""
    data = account.model_dump()
    data.pop("password_hash", None)
    return data


def get_current_user(authorization: str | None = Header(default=None)) -> UserAccount:
    """解析当前登录用户；未登录/令牌过期/被禁用抛 401"""
    account = auth_service.authenticate(_extract_token(authorization))
    if account is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return account


def require_admin(user: UserAccount = Depends(get_current_user)) -> UserAccount:
    """管理后台鉴权：非 admin 角色抛 403"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ===================== 认证接口 =====================
@admin_router.post("/auth/register")
def register(req: RegisterRequest) -> dict:
    try:
        account = auth_service.register(req.username, req.password, req.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "user": _public_account(account)}


@admin_router.post("/auth/login")
def login(req: LoginRequest) -> dict:
    try:
        token, account = auth_service.login(req.username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return {"status": "ok", "token": token, "user": _public_account(account)}


@admin_router.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    auth_service.logout(_extract_token(authorization))
    return {"status": "ok"}


@admin_router.get("/auth/me")
def me(user: UserAccount = Depends(get_current_user)) -> dict:
    return {"status": "ok", "user": _public_account(user)}


# ===================== 管理后台：用户管理 =====================
@admin_router.get("/admin/users")
def admin_list_users(
    search: str | None = Query(default=None, description="按用户名/昵称模糊搜索"),
    status: str | None = Query(default=None, description="active / disabled"),
    sort: str = Query(default="created_at", description="created_at / last_active_at / username"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: UserAccount = Depends(require_admin),
) -> dict:
    """用户列表：过滤 + 排序 + 分页。"""
    accounts = auth_service.store.list_accounts()
    if status:
        accounts = [a for a in accounts if a.status == status]
    if search:
        kw = search.strip().lower()
        accounts = [
            a for a in accounts
            if kw in a.username.lower() or (a.display_name and kw in a.display_name.lower())
        ]
    if sort in ("username", "last_active_at"):
        accounts.sort(key=lambda a: (a.last_active_at or "") if sort == "last_active_at" else a.username)
    # created_at 默认倒序（store 已按 created_at DESC）
    total = len(accounts)
    start = (page - 1) * page_size
    page_accounts = accounts[start : start + page_size]
    users = []
    for a in page_accounts:
        item = _public_account(a)
        item["trips_count"] = memory_store.count_trip_memories(a.user_id)
        users.append(item)
    return {
        "status": "ok",
        "total": total,
        "page": page,
        "page_size": page_size,
        "users": users,
    }


@admin_router.get("/admin/users/{user_id}")
def admin_get_user(
    user_id: str,
    _: UserAccount = Depends(require_admin),
) -> dict:
    """用户详情：账号信息 + 偏好记忆 + 历史行程 + 会话列表。"""
    account = auth_service.store.load_by_id(user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    preferences = memory_manager.load_user_memory(user_id)
    trips = memory_manager.load_trip_memories(user_id)
    sessions = redis_session_repository.list_summaries(user_id=user_id)
    return {
        "status": "ok",
        "user": _public_account(account),
        "preferences": preferences.model_dump() if preferences else None,
        "trip_memories": [t.model_dump() for t in trips],
        "sessions": sessions,
    }


@admin_router.patch("/admin/users/{user_id}/status")
def admin_set_user_status(
    user_id: str,
    body: AdminStatusBody,
    _: UserAccount = Depends(require_admin),
) -> dict:
    """启用 / 禁用用户。"""
    account = auth_service.store.load_by_id(user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    updated = account.model_copy(update={"status": body.status})
    auth_service.store.save(updated)
    return {"status": "ok", "user": _public_account(updated)}


@admin_router.delete("/admin/users/{user_id}")
def admin_delete_user(
    user_id: str,
    admin: UserAccount = Depends(require_admin),
) -> dict:
    """删除用户：账号 + 令牌 + 记忆 + 全部会话。"""
    account = auth_service.store.load_by_id(user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if account.user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员账号")
    auth_service.store.delete(user_id)
    memory_store.delete_user_data(user_id)
    deleted_sessions = redis_session_repository.delete_by_user(user_id)
    return {"status": "ok", "deleted_sessions": deleted_sessions}


# ===================== RAG 景点知识库管理 =====================
@admin_router.get("/admin/kb/status")
def kb_status(_: UserAccount = Depends(require_admin)) -> dict:
    """知识库状态：定时配置 + 最近运行 + 历史 + 集合统计"""
    return {
        "config": kb_admin_manager.get_config(),
        "status": kb_admin_manager.get_status(),
        "history": kb_admin_manager.get_history(),
        "stats": kb_admin_manager.get_stats(),
    }


@admin_router.post("/admin/kb/reindex")
def kb_reindex(_: UserAccount = Depends(require_admin)) -> dict:
    """手动触发一次知识库重建"""
    return kb_admin_manager.run_now(trigger="manual")


@admin_router.patch("/admin/kb/config")
def kb_config(body: KbConfigBody, _: UserAccount = Depends(require_admin)) -> dict:
    """更新定时任务配置（开关 / 执行间隔）"""
    return {"config": kb_admin_manager.update_config(enabled=body.enabled, interval_minutes=body.interval_minutes)}


@admin_router.get("/admin/kb/bases")
def kb_bases(_: UserAccount = Depends(require_admin)) -> dict:
    """列出全部知识库类型及其数据量"""
    return {"bases": kb_admin_manager.get_bases()}


@admin_router.get("/admin/kb/attraction/cities")
def kb_attraction_cities(_: UserAccount = Depends(require_admin)) -> dict:
    """列出景点知识库的全部地点（城市）及景点数"""
    return {"cities": kb_admin_manager.list_cities()}


@admin_router.get("/admin/kb/attraction/spots")
def kb_attraction_spots(city: str = Query(...), _: UserAccount = Depends(require_admin)) -> dict:
    """查询某地点包含的全部景点"""
    return {"city": city, "spots": kb_admin_manager.list_spots(city)}


@admin_router.post("/admin/kb/attraction/spots")
def kb_attraction_create_spot(body: AttractionSpotBody, _: UserAccount = Depends(require_admin)) -> dict:
    """在某地点新建景点（写 json + 整体重导）"""
    ok = kb_admin_manager.create_spot(body.model_dump())
    return {
        "status": "ok" if ok else "skipped",
        "message": "景点已创建" if ok else "创建失败：城市/景点名不能为空，或已存在同名景点",
    }


@admin_router.delete("/admin/kb/attraction/spots/{city}/{name}")
def kb_attraction_delete_spot(city: str, name: str, _: UserAccount = Depends(require_admin)) -> dict:
    """删除某地点下的一个景点"""
    ok = kb_admin_manager.delete_spot(city, name)
    return {"status": "ok" if ok else "not_found", "message": "景点已删除" if ok else "景点不存在"}


@admin_router.delete("/admin/kb/attraction/cities/{city}")
def kb_attraction_delete_city(city: str, _: UserAccount = Depends(require_admin)) -> dict:
    """删除某地点（城市）及其全部景点数据"""
    ok = kb_admin_manager.delete_city(city)
    return {"status": "ok" if ok else "not_found", "message": "地点已删除" if ok else "地点不存在"}
