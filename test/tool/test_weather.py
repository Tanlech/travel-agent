"""weather tool 手动测试：修改下方输入（城市 + 日期范围），运行后打印给 agent 的天气 JSON 输出"""

import json
from pathlib import Path

import pytest

from app.infrastructure.qweather_client import qweather_client
from app.tools.schema.weather import WeatherInput
from app.tools.weather import weather_tool


@pytest.fixture(autouse=True)
def _ensure_config():
    """补齐 QWEATHER 配置（pytest 工作目录变化时 .env 相对路径会落空）"""
    _load_qweather_config()


def _load_qweather_config() -> None:
    root = Path(__file__).resolve().parents[2]  # test/tool -> 项目根
    env_path = root / ".env"
    if not env_path.exists():
        return
    env = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    qweather_client.config.host = qweather_client.config.host or env.get("QWEATHER_HOST")
    qweather_client.config.api_key = qweather_client.config.api_key or env.get("QWEATHER_API_KEY")
    qweather_client.config.geo_api_key = qweather_client.config.geo_api_key or env.get("QWEATHER_GEO_API_KEY")


def test_weather_run():
    # ===== 输入（在这里修改）=====
    city = "广州"
    start_time = "2026-08-23"
    end_time = "2026-09-01"
    # ============================

    result = weather_tool.run(WeatherInput(city=city, start_time=start_time, end_time=end_time))

    print(f"\n输入: city={city}  {start_time} ~ {end_time}")
    print("输出（给 agent 的 JSON）:")
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    assert result.daily
