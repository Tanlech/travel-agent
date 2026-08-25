"""transport_tool 的路线查询测试：单元测试（mock 高德）+ 集成测试（真实高德 API，可自定义输入）"""

import json
from pathlib import Path

import pytest

from app.infrastructure.amap_client import amap_client
from app.agent.tools.schema.transport import TransitRouteStep, TransportInput
from app.agent.tools.transport import TransportTool, transport_tool


@pytest.fixture(autouse=True)
def _ensure_config():
    """补齐高德配置（pytest 工作目录变化时 .env 相对路径会落空）"""
    _load_amap_config()


def _load_amap_config() -> None:
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
    amap_client.config.api_key = amap_client.config.api_key or env.get("AMAP_KEY")


# ---------------------------------------------------------------------------
# mock 数据构造
# ---------------------------------------------------------------------------


def _poi(name: str, lng: float = 116.397, lat: float = 39.909) -> dict:
    return {
        "poi_id": f"poi-{name}",
        "name": name,
        "lng": lng,
        "lat": lat,
        "address": f"{name}地址",
        "cityname": "北京市",
        "adname": "东城区",
    }


def _walk_result() -> dict:
    return {"mode": "walking", "distance_meters": 800, "duration_seconds": 600}


def _taxi_result() -> dict:
    return {"mode": "driving", "distance_meters": 3000, "duration_seconds": 900, "traffic_lights": 5, "cost": 20.5}


def _transit_result() -> dict:
    return {
        "mode": "transit",
        "distance_meters": 5000,
        "duration_seconds": 1500,
        "price": 4.0,
        "walking_distance": 300,
        "transits": [
            {
                "cost": "4",
                "duration": "1800",
                "distance": "8000",
                "walking_distance": "300",
                "segments": [
                    {
                        "walking": {"distance": "200", "duration": "180"},
                        "bus": {
                            "buslines": [
                                {
                                    "name": "地铁1号线",
                                    "type": "地铁",
                                    "distance": "5000",
                                    "duration": "1200",
                                    "departure_stop": {"name": "国贸站"},
                                    "arrival_stop": {"name": "西单站"},
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


_UNSET = object()


def _mock_amap(monkeypatch, *, pois=None, geocoded=None, walk=_UNSET, taxi=_UNSET, transit=_UNSET) -> None:
    monkeypatch.setattr(amap_client, "is_enabled", lambda: True)
    monkeypatch.setattr(amap_client, "search_pois", lambda **kwargs: pois or [])
    monkeypatch.setattr(amap_client, "geocode", lambda *, address, city: geocoded)
    monkeypatch.setattr(
        amap_client, "plan_walking",
        lambda **kwargs: _walk_result() if walk is _UNSET else walk,
    )
    monkeypatch.setattr(
        amap_client, "plan_driving",
        lambda **kwargs: _taxi_result() if taxi is _UNSET else taxi,
    )
    monkeypatch.setattr(
        amap_client, "plan_transit",
        lambda **kwargs: _transit_result() if transit is _UNSET else transit,
    )


# ---------------------------------------------------------------------------
# 单元测试（mock，不依赖真实 API）
# ---------------------------------------------------------------------------


def test_single_segment_returns_one_result(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")])

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门"))

    assert len(results) == 1
    result = results[0]
    assert result.from_name == "故宫"
    assert result.to_name == "天安门"
    assert result.walk is not None
    assert result.taxi is not None
    assert result.transit is not None
    assert result.error is None


def test_multi_waypoints_splits_into_adjacent_segments(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("王府井"), _poi("天安门")])

    results = transport_tool.run(
        TransportInput(city="北京", from_name="故宫", to_name="天安门", waypoints=["王府井"]),
    )

    assert len(results) == 2
    assert (results[0].from_name, results[0].to_name) == ("故宫", "王府井")
    assert (results[1].from_name, results[1].to_name) == ("王府井", "天安门")


def test_waypoints_with_tail_from_to(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("A"), _poi("B"), _poi("C"), _poi("D")])

    results = transport_tool.run(TransportInput(city="北京", from_name="A", to_name="D", waypoints=["B", "C"]))

    assert [(r.from_name, r.to_name) for r in results] == [("A", "B"), ("B", "C"), ("C", "D")]


def test_route_points_filters_empty_and_adjacent_duplicates(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")])

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门", waypoints=["", "天安门"]))

    assert len(results) == 1
    assert (results[0].from_name, results[0].to_name) == ("故宫", "天安门")


def test_insufficient_points_returns_error():
    results = transport_tool.run(TransportInput(city="北京", from_name="故宫"))

    assert len(results) == 1
    assert "至少需要起点和终点" in (results[0].error or "")


def test_poi_resolution_failure_returns_error(monkeypatch):
    _mock_amap(monkeypatch, pois=[], geocoded=None)

    results = transport_tool.run(TransportInput(city="北京", from_name="不存在的点", to_name="天安门"))

    assert len(results) == 1
    assert "起点未能解析（不存在的点）" in (results[0].error or "")


def test_transit_steps_contain_structured_fields(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")])

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门"))
    steps = results[0].transit.steps

    walk_steps = [step for step in steps if step.mode == "walk"]
    ride_steps = [step for step in steps if step.mode in {"metro", "bus"}]
    assert walk_steps
    assert ride_steps
    assert ride_steps[0].name == "地铁1号线"
    assert ride_steps[0].from_station == "国贸站"
    assert ride_steps[0].to_station == "西单站"
    assert walk_steps[0].distance_meters is not None
    assert walk_steps[0].duration_minutes is not None


def test_choose_best_transit_option_prefers_shortest_duration():
    tool = TransportTool()
    options = [
        {"duration_minutes": 60, "walking_distance_meters": 500, "transfer_count": 2, "cost": 3.0},
        {"duration_minutes": 40, "walking_distance_meters": 1000, "transfer_count": 1, "cost": 4.0},
        {"duration_minutes": 40, "walking_distance_meters": 300, "transfer_count": 1, "cost": 5.0},
    ]
    # 直接构造 schema 对象避免依赖 _summarize_transit_option
    from app.agent.tools.schema.transport import TransitOptionSummary

    option_models = [TransitOptionSummary(**item) for item in options]
    best = tool._choose_best_transit_option(option_models)
    assert best.duration_minutes == 40
    assert best.walking_distance_meters == 300  # 耗时相同取步行更少


def test_normalize_transit_mode():
    tool = TransportTool()
    assert tool._normalize_transit_mode("地铁") == "metro"
    assert tool._normalize_transit_mode("公交") == "bus"
    assert tool._normalize_transit_mode("无轨电车") == "bus"
    assert tool._normalize_transit_mode(None) is None


def test_taxi_cost_fallback_when_api_missing_cost(monkeypatch):
    taxi = {"mode": "driving", "distance_meters": 5000, "duration_seconds": 900, "cost": None}
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")], taxi=taxi)

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门"))

    # 5000 米 → 14 + 5 * 2.8 = 28.0
    assert results[0].taxi.cost == 28.0


def test_partial_failure_keeps_other_modes(monkeypatch):
    """公交接口异常时，步行/打车结果仍应保留"""
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")], transit=None)

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门"))

    assert results[0].walk is not None
    assert results[0].taxi is not None
    assert results[0].transit is None


def test_model_dump_contains_structured_steps(monkeypatch):
    _mock_amap(monkeypatch, pois=[_poi("故宫"), _poi("天安门")])

    results = transport_tool.run(TransportInput(city="北京", from_name="故宫", to_name="天安门"))
    dump = results[0].model_dump()
    step = dump["transit"]["steps"][0]
    assert step["mode"] is not None
    assert step["distance_meters"] is not None
    assert "description" not in step  # 不额外添加自然语言描述


def test_waypoints_exceeding_limit_gets_truncated_with_note(monkeypatch):
    """超过 _MAX_SEGMENTS 时截断并附带 note 提示"""
    _mock_amap(monkeypatch, pois=[_poi(f"点{index}") for index in range(10)])

    waypoints = [f"点{index}" for index in range(8)]
    results = transport_tool.run(TransportInput(city="北京", from_name="点起点", to_name="点终点", waypoints=waypoints))

    assert len(results) == TransportTool._MAX_SEGMENTS
    assert results[0].note is not None
    assert "截断" in results[0].note


# ---------------------------------------------------------------------------
# 集成测试（真实高德 API，可自定义输入）
# ---------------------------------------------------------------------------


def _run_integration_case(title: str, city: str, from_name: str, to_name: str, waypoints: list[str] | None = None) -> None:
    print(f"\n========== {title} ==========")
    print(f"输入: {from_name} → {' → '.join(waypoints) if waypoints else ''} → {to_name}（{city}）")
    results = transport_tool.run(TransportInput(city=city, from_name=from_name, to_name=to_name, waypoints=waypoints or []))
    print("输出（给 agent 的 JSON）:")
    for index, segment in enumerate(results, 1):
        print(f"\n  [{index}/{len(results)} 段]")
        print(json.dumps(segment.model_dump(), indent=2, ensure_ascii=False))
    assert results, "路线结果为空"


@pytest.mark.integration
def test_transport_beijing_single():
    # ===== 输入（在这里修改）=====
    _run_integration_case("北京 单段 故宫→天安门", city="北京", from_name="故宫", to_name="天安门")


@pytest.mark.integration
def test_transport_beijing_multi_waypoints():
    # ===== 输入（在这里修改）=====
    _run_integration_case(
        title='',
        city="广州",
        from_name="广州塔",
        to_name="孙中山纪念堂",
        waypoints=[],
    )


@pytest.mark.integration
def test_transport_shanghai_long_distance():
    # ===== 输入（在这里修改）=====
    _run_integration_case(
        "上海 远距离 外滩→浦东机场",
        city="上海",
        from_name="外滩",
        to_name="上海浦东国际机场",
    )
