"""意图类型与改稿范围（intent / session 层共享，避免重复定义）"""

from enum import StrEnum
from typing import Literal


class IntentType(StrEnum):
    """意图类型（成员值即序列化值）"""

    NEW_PLAN = "new_plan"            # 新一轮规划（首次/重新给足信息）
    REVISE_PLAN = "revise_plan"      # 修改已有行程（换/调/改）
    CLARIFICATION = "clarification"  # 信息不齐，还需追问补全
    QA = "qa"                        # 闲聊/问答（问候、感谢、能力询问）
    CONFIRM = "confirm"              # 确认当前行程，收尾会话
    REJECT = "reject"                # 拒绝（不用了/算了）
    END_SESSION = "end_session"      # 结束会话（再见/拜拜）
    UNKNOWN = "unknown"              # 兜底：无法可靠判断


# 改动范围：局部块 / 单日 / 全局
RevisionScope = Literal["block_level", "day_level", "global"]
