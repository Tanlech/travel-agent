"""通用对话消息结构（intent / session 层共享，避免业务模型反向依赖）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# 消息角色（新增角色时同步更新）
ChatRole = Literal["user", "assistant", "system"]


class ChatMessage(BaseModel):
    """近期对话消息"""

    role: ChatRole
    content: str
