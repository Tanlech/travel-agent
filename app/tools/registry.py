"""工具注册层：将 4 个领域工具暴露为 OpenAI function calling 契约

每个工具提供：
- name / description：供 LLM 决定何时调用
- parameters：Pydantic Input 的 JSON Schema，供 LLM 填参
- execute：执行并返回可序列化结果
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from app.tools.attraction import attraction_tool
from app.tools.lodging import lodging_tool
from app.tools.schema.attraction import AttractionInput
from app.tools.schema.lodging import LodgingInput
from app.tools.schema.transport import TransportInput
from app.tools.schema.weather import WeatherInput
from app.tools.transport import transport_tool
from app.tools.weather import weather_tool


def _schema_of(model: type) -> dict[str, Any]:
    """Pydantic Input 的 JSON Schema（用于 OpenAI parameters）"""
    schema = model.model_json_schema()
    # 移除 pydantic 特有元数据，保留给 LLM 的纯净结构
    for key in ("title", "definitions", "$defs"):
        schema.pop(key, None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


def _dump(value: Any) -> Any:
    """把 Pydantic 模型（或模型列表）转可序列化结构，其余原样返回"""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [item.model_dump() if isinstance(item, BaseModel) else item for item in value]
    return value


def _execute(model: type, run: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """校验并执行工具，返回可序列化结果"""
    parsed = model(**args)
    result = run(parsed)
    return _dump(result)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "attraction_tool",
        "description": (
            "检索某城市的代表性景点。在需要为行程挑选景点时调用；"
            "输入城市、天数、必去/不去景点、偏好主题，返回候选景点列表（含区域、建议游玩时长、实体级别）。"
        ),
        "parameters": _schema_of(AttractionInput),
        "execute": lambda args: _execute(AttractionInput, attraction_tool.run, args),
    },
    {
        "name": "lodging_tool",
        "description": (
            "检索某城市符合偏好的真实住宿候选。当行程需要确定住宿或更换住宿时调用；"
            "输入城市、偏好（位置/档次）、规避关键词、行程景点（用于距离排序），"
            "返回候选住宿列表（含名称、区域、评分、档次、距景点距离、地址、电话）。"
        ),
        "parameters": _schema_of(LodgingInput),
        "execute": lambda args: _execute(LodgingInput, lodging_tool.run, args),
    },
    {
        "name": "transport_tool",
        "description": (
            "查询同城两点或多点之间的交通路线（步行/打车/公交）。"
            "当需要判断行程中两点间的通行可行性或时长时调用；"
            "输入城市、起点、终点和可选途经点，返回每段的三种交通方式对比。"
        ),
        "parameters": _schema_of(TransportInput),
        "execute": lambda args: _execute(TransportInput, transport_tool.run, args),
    },
    {
        "name": "weather_tool",
        "description": (
            "查询某城市某日期范围的逐日天气预报。当需要天气证据来安排户外/室内活动时调用；"
            "输入城市、开始日期、结束日期，返回逐日天气、温度范围、风力、湿度、降水。"
        ),
        "parameters": _schema_of(WeatherInput),
        "execute": lambda args: _execute(WeatherInput, weather_tool.run, args),
    },
]

TOOL_INDEX: dict[str, dict[str, Any]] = {item["name"]: item for item in TOOL_DEFINITIONS}


def build_openai_tools() -> list[dict[str, Any]]:
    """生成 OpenAI chat.completions 的 tools 参数"""
    return [
        {
            "type": "function",
            "function": {
                "name": item["name"],
                "description": item["description"],
                "parameters": item["parameters"],
            },
        }
        for item in TOOL_DEFINITIONS
    ]


def dispatch_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行一次工具调用并返回结果"""
    item = TOOL_INDEX.get(name)
    if not item:
        return {"error": f"未知工具: {name}"}
    try:
        return {"name": name, "result": item["execute"](arguments)}
    except Exception as exc:
        return {"name": name, "error": f"{type(exc).__name__}: {exc}"}
