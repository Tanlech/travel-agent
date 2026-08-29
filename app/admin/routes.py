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
from app.agent.knowledge.attraction_kb import (
    ai_generate_city_spots,
    apply_quality,
    create_spot,
    create_spots_batch,
    delete_city,
    delete_spot,
    get_standard_tags,
    global_task_status,
    list_cities,
    list_spots,
    load_tag_library,
    quality_ai_scan,
    quality_check,
    add_tag,
    remove_tag,
    scrub_all_tags,
    scrub_city_tags,
    start_clean_all_tags,
    start_clean_city_tags,
    start_ai_generate,
    start_quality_ai_all,
    start_quality_ai_city,
    start_quality_ai_province,
    update_spot,
    update_tag,
    upgrade_tag_library,
)

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
    """知识库定时任务配置请求体。bases 形如 {collection: {enabled, interval_minutes}}。"""

    enabled: bool | None = None
    interval_minutes: int | None = None
    bases: dict[str, dict] | None = None


class ReindexBody(BaseModel):
    """重建目标请求体：target 为子库 collection、collection 列表；缺省表示全部子库"""

    target: str | list[str] | None = None


class AttractionSpotBody(BaseModel):
    """景点知识库·新建景点请求体。

    city 在单个新建时必填；批量导入时城市由外层 AttractionBatchBody.city 提供，
    单条 item 可不带 city（缺省为空串，不影响入库逻辑）。
    """

    city: str = ""
    name: str
    province: str | None = None
    area: str | None = None
    duration: float | None = None
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)


class AttractionAiBody(BaseModel):
    """景点知识库·AI 生成某城市一批景点请求体。"""

    city: str
    hint: str | None = None
    count: int | None = Field(default=None, ge=1, le=100)


class AttractionBatchBody(BaseModel):
    """景点知识库·批量新建景点请求体。"""

    city: str
    spots: list[AttractionSpotBody] = Field(default_factory=list)


class AttractionUpdateBody(BaseModel):
    """景点知识库·编辑景点请求体（name 缺省则不改名；area/省份/reason/tags 传空串表示清空）。"""

    name: str | None = None
    province: str | None = None
    area: str | None = None
    duration: float | None = None
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)


class QualityActionBody(BaseModel):
    """景点知识库·质量处理单条决策：merge=合并到主景点并删子，delete=删除子，keep=忽略"""

    city: str
    main: str = ""
    sub: str
    action: Literal["merge", "delete", "keep"]


class QualityApplyBody(BaseModel):
    """景点知识库·质量处理批量应用请求体"""

    actions: list[QualityActionBody] = Field(default_factory=list)


def _bg_task_response(started: bool, ok_msg: str) -> dict:
    """后台任务启动的统一返回：已启动 ok / 已在运行 running。"""
    return {"status": "ok" if started else "running", "running": started,
            "message": ok_msg if started else "已有任务正在运行"}


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
def kb_reindex(body: ReindexBody | None = None, _: UserAccount = Depends(require_admin)) -> dict:
    """手动触发知识库重建：target 指定子库或子库列表，缺省重建全部子库。每个子库只重建自己"""
    target = body.target if body else None
    return kb_admin_manager.run_now(target=target, trigger="manual")


@admin_router.patch("/admin/kb/config")
def kb_config(body: KbConfigBody, _: UserAccount = Depends(require_admin)) -> dict:
    """更新定时任务配置：按子库独立设置开关/间隔"""
    return {
        "config": kb_admin_manager.update_config(
            bases=body.bases,
            enabled=body.enabled,
            interval_minutes=body.interval_minutes,
        )
    }


@admin_router.get("/admin/kb/bases")
def kb_bases(_: UserAccount = Depends(require_admin)) -> dict:
    """列出全部知识库类型及其数据量"""
    return {"bases": kb_admin_manager.get_bases()}


@admin_router.get("/admin/kb/attraction/cities")
def kb_attraction_cities(_: UserAccount = Depends(require_admin)) -> dict:
    """列出景点知识库的全部地点（城市）及景点数"""
    return {"cities": list_cities()}


@admin_router.get("/admin/kb/attraction/spots")
def kb_attraction_spots(city: str = Query(...), _: UserAccount = Depends(require_admin)) -> dict:
    """查询某地点包含的全部景点"""
    return {"city": city, "spots": list_spots(city)}


@admin_router.post("/admin/kb/attraction/spots/ai-generate")
def kb_attraction_ai_generate(body: AttractionAiBody, _: UserAccount = Depends(require_admin)) -> dict:
    """后台启动用 AI 推荐某城市的一批高质量景点（清洗低质量项），结果经任务状态接口获取（刷新可恢复）"""
    started = start_ai_generate(body.city, body.hint or "", body.count)
    return _bg_task_response(started, f"已启动生成「{body.city}」的景点")


@admin_router.post("/admin/kb/attraction/spots/batch")
def kb_attraction_create_spots_batch(body: AttractionBatchBody, _: UserAccount = Depends(require_admin)) -> dict:
    """批量新建某地点景点（逐条写 json + 单点同步）"""
    return create_spots_batch(body.city, [s.model_dump() for s in body.spots])


@admin_router.post("/admin/kb/attraction/spots")
def kb_attraction_create_spot(body: AttractionSpotBody, _: UserAccount = Depends(require_admin)) -> dict:
    """在某地点新建景点（写 json + 单点同步）"""
    ok = create_spot(body.model_dump())
    return {
        "status": "ok" if ok else "skipped",
        "message": "景点已创建" if ok else "创建失败：城市/景点名不能为空，或已存在同名景点",
    }


@admin_router.put("/admin/kb/attraction/spots/{city}/{name}")
def kb_attraction_update_spot(city: str, name: str, body: AttractionUpdateBody, _: UserAccount = Depends(require_admin)) -> dict:
    """编辑某地点下某景点（写 json + 单点同步，支持改名）"""
    ok = update_spot(city, name, body.model_dump())
    return {
        "status": "ok" if ok else "not_found",
        "message": "编辑成功" if ok else "景点不存在，或新名称与其他景点冲突",
    }


@admin_router.delete("/admin/kb/attraction/spots/{city}/{name}")
def kb_attraction_delete_spot(city: str, name: str, _: UserAccount = Depends(require_admin)) -> dict:
    """删除某地点下的一个景点"""
    ok = delete_spot(city, name)
    return {"status": "ok" if ok else "not_found", "message": "景点已删除" if ok else "景点不存在"}


@admin_router.delete("/admin/kb/attraction/cities/{city}")
def kb_attraction_delete_city(city: str, _: UserAccount = Depends(require_admin)) -> dict:
    """删除某地点（城市）及其全部景点数据"""
    ok = delete_city(city)
    return {"status": "ok" if ok else "not_found", "message": "地点已删除" if ok else "地点不存在"}


@admin_router.get("/admin/kb/attraction/tags/library")
def kb_attraction_tags_library(_: UserAccount = Depends(require_admin)) -> dict:
    """获取标准标签库（标准标签 → 别名映射；含分类 categories）"""
    _tags = load_tag_library()
    return {
        "status": "ok",
        "tags": _tags["tags"],
        "categories": _tags.get("categories") or {},
        "locked": _tags.get("locked") or [],
        "standard": get_standard_tags(),
    }


@admin_router.post("/admin/kb/attraction/tags/clean")
def kb_attraction_tags_clean(city: str = Query(""), _: UserAccount = Depends(require_admin)) -> dict:
    """一键重刷景区标签：传 city 以后台任务方式只清洗该地点（状态可刷新恢复）；不传则同步清洗全部地点"""
    city = city.strip()
    if city:
        started = start_clean_city_tags(city)
        return _bg_task_response(started, f"已启动更新「{city}」标签")
    return scrub_all_tags()


@admin_router.post("/admin/kb/attraction/tags/clean-all")
def kb_attraction_tags_clean_all(_: UserAccount = Depends(require_admin)) -> dict:
    """后台启动『更新所有城市标签』（逐城市 LLM 打标，耗时较长）；通过任务状态接口查看进度"""
    return _bg_task_response(start_clean_all_tags(), "已启动更新所有城市标签")


@admin_router.post("/admin/kb/attraction/tags/upgrade")
def kb_attraction_tags_upgrade(_: UserAccount = Depends(require_admin)) -> dict:
    """AI 全自动升级标准标签库：让模型基于全库实际标签产出更完善的标准标签库，写入库文件并重刷全部景点"""
    return upgrade_tag_library()


class TagAddBody(BaseModel):
    tag: str = ""
    aliases: list[str] = Field(default_factory=list)
    category: str = "其他"


class TagDeleteBody(BaseModel):
    tag: str = ""


class TagUpdateBody(BaseModel):
    tag: str = ""
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    locked: bool | None = None


@admin_router.post("/admin/kb/attraction/tags/add")
def kb_attraction_tags_add(body: TagAddBody, _: UserAccount = Depends(require_admin)) -> dict:
    """新增一个标准标签（可带别名与分类）"""
    return add_tag(body.tag, body.aliases, body.category)


@admin_router.post("/admin/kb/attraction/tags/delete")
def kb_attraction_tags_delete(body: TagDeleteBody, _: UserAccount = Depends(require_admin)) -> dict:
    """删除一个标准标签"""
    return remove_tag(body.tag)


@admin_router.post("/admin/kb/attraction/tags/update")
def kb_attraction_tags_update(body: TagUpdateBody, _: UserAccount = Depends(require_admin)) -> dict:
    """编辑标准标签：改别名/分类/锁定状态"""
    return update_tag(body.tag, body.aliases, body.category, body.locked)


@admin_router.get("/admin/kb/attraction/quality-check")
def kb_attraction_quality_check(city: str = Query(...), _: UserAccount = Depends(require_admin)) -> dict:
    """扫描某城市景点，返回疑似『子景点/重复』配对（供人工确认）"""
    return {"status": "ok", "groups": quality_check(city)}


@admin_router.post("/admin/kb/attraction/quality-ai")
def kb_attraction_quality_ai(city: str = Query(...), _: UserAccount = Depends(require_admin)) -> dict:
    """后台启动对某城市**每个景点**的大模型逐条质检（单城市，状态可刷新恢复）；结果与进度经任务状态接口获取"""
    return _bg_task_response(start_quality_ai_city(city), f"已启动检测「{city}」城市景点")


@admin_router.post("/admin/kb/attraction/quality-ai-all")
def kb_attraction_quality_ai_all(_: UserAccount = Depends(require_admin)) -> dict:
    """后台启动『检测所有城市景点』（逐城市逐景点大模型质检，耗时较长）；结果与进度通过任务状态接口渐进获取"""
    return _bg_task_response(start_quality_ai_all(), "已启动检测所有城市景点")


@admin_router.get("/admin/kb/attraction/task")
def kb_attraction_task_status(_: UserAccount = Depends(require_admin)) -> dict:
    """返回后台长任务状态与进度（更新所有标签 / 检测所有景点），供前台刷新后恢复运行态并渐进展示结果"""
    return {"status": "ok", "tasks": global_task_status()}


@admin_router.post("/admin/kb/attraction/quality-ai-province")
def kb_attraction_quality_ai_province(province: str = Query(...), _: UserAccount = Depends(require_admin)) -> dict:
    """后台启动『检测某省份全部城市景点』（逐城市逐景点大模型质检，耗时较长）；结果与进度通过任务状态接口获取"""
    return _bg_task_response(start_quality_ai_province(province), f"已启动检测「{province}」城市景点")


@admin_router.post("/admin/kb/attraction/quality-apply")
def kb_attraction_quality_apply(body: QualityApplyBody, _: UserAccount = Depends(require_admin)) -> dict:
    """应用人工确认的质量处理：合并/删除/忽略"""
    return apply_quality([a.model_dump() for a in body.actions])
