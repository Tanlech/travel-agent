"""用户账号数据模型（MySQL 存储）"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

AccountRole = Literal["user", "admin"]
AccountStatus = Literal["active", "disabled"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserAccount(BaseModel):
    """注册用户账号（跨会话身份；密码只存 pbkdf2 哈希）"""

    user_id: str
    username: str  # 唯一，注册时归一为小写
    password_hash: str  # pbkdf2_sha256$iterations$salt$digest
    display_name: str | None = None
    role: AccountRole = "user"
    status: AccountStatus = "active"
    created_at: str = Field(default_factory=_utc_now_iso)
    last_active_at: str | None = None
