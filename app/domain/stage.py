"""会话阶段定义
由 session / context / memory 三层共享，避免 context⇄session 循环依赖
"""

from typing import Literal

ConversationStage = Literal[
    "collecting_destination",   # 还在补目的地
    "collecting_dates",         # 目的地已知，但还在补游玩日期
    "collecting_requirements",  # 目的地和日期已知后，继续补其他信息
    "ready_to_plan",            # 关键字段齐了，准备进入规划
    "revise_collecting",        # 修改已有行程时，正在收集改稿信息
    "revise_ready",             # 改稿信息已基本齐备，准备进入修改
    "qa",                       # 当前是问答模式
    "completed",                # 当前会话目标已完成
    "closed",                   # 会话关闭
]
