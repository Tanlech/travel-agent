from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

"""响应裁剪配置（决定最终行程输出包含哪些部分，planning agent 输出阶段消费）"""

class ResponseContext(BaseModel):
    response_mode: Literal["final_plan", "follow_up"] = "final_plan"
    include_alternatives: bool = True  # 备选方案
    include_summary: bool = True  # 行程摘要
    include_daily_plan: bool = True  # 逐日安排
    include_stay_recommendation: bool = True  # 住宿推荐
    include_transport_plan: bool = True  # 交通方案
    include_weather_notes: bool = True  # 天气提示
