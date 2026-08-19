"""lodging_tool 的候选检索、过滤和排序测试。"""

from pathlib import Path

import pytest

from app.infrastructure.amap_client import amap_client
from app.tools.lodging import LodgingTool, lodging_tool
from app.tools.schema.lodging import LodgingInput


@pytest.fixture(autouse=True)
def _ensure_config():
    _load_amap_config()


def _load_amap_config() -> None:
    root = Path(__file__).resolve().parents[2]
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


def _poi(
    poi_id: str | None,
    name: str,
    *,
    lng: float | None = 116.1,
    lat: float | None = 39.9,
    address: str | None = "测试路 1 号",
    poi_type: str = "住宿服务;宾馆酒店;四星级宾馆",
    rating: str | None = "4.5",
    keytag: str | None = "四星级酒店",
) -> dict:
    return {
        "poi_id": poi_id,
        "name": name,
        "lng": lng,
        "lat": lat,
        "address": address,
        "type": poi_type,
        "business_area": "中心城区",
        "tel": "010-12345678",
        "rating": rating,
        "keytag": keytag,
    }


def _mock_amap(monkeypatch, pois: list[dict], coords: dict[str, dict] | None = None) -> None:
    monkeypatch.setattr(amap_client, "is_enabled", lambda: True)
    monkeypatch.setattr(amap_client, "search_pois", lambda **kwargs: pois)
    monkeypatch.setattr(
        amap_client,
        "geocode",
        lambda *, address, city: (coords or {}).get(address),
    )


def test_output_contains_only_candidate_basic_fields(monkeypatch):
    _mock_amap(monkeypatch, [_poi("hotel-1", "中心酒店")])

    result = LodgingTool().run(LodgingInput(destination="测试市"))

    assert result.model_dump().keys() == {"city", "candidates", "summary", "debug"}
    assert result.debug is not None
    assert result.debug["raw_candidate_count"] == 1
    assert result.debug["query_count"] >= 1
    assert result.candidates[0].model_dump().keys() == {
        "poi_id", "name", "area", "address", "tel", "rating", "keytag", "distance_to_spots_km",
    }


def test_ranks_by_multi_spot_location(monkeypatch):
    coords = {
        "景点甲": {"lng": 116.0, "lat": 39.0},
        "景点乙": {"lng": 116.2, "lat": 39.0},
    }
    pois = [
        _poi("central", "中心酒店", lng=116.1, lat=39.0, rating="4.8", keytag="四星级酒店"),
        _poi("far", "远郊酒店", lng=116.6, lat=39.0, rating="4.2", keytag="四星级酒店"),
        _poi("expensive", "昂贵酒店", lng=116.1, lat=39.0, rating="4.8", keytag="四星级酒店"),
    ]
    _mock_amap(monkeypatch, pois, coords)

    result = LodgingTool().run(
        LodgingInput(destination="测试市", spots=["景点甲", "景点乙"])
    )

    assert result.candidates[-1].name == "远郊酒店"  # 距景点最远，排最后
    assert "中心酒店" in [item.name for item in result.candidates]


def test_filters_non_lodging_and_avoid_keywords(monkeypatch):
    pois = [
        _poi("good", "可靠酒店"),
        _poi("avoid", "便宜招待所", poi_type="住宿服务;旅馆招待所"),
        _poi("museum", "城市博物馆", poi_type="科教文化服务;博物馆"),
    ]
    _mock_amap(monkeypatch, pois)

    result = LodgingTool().run(
        LodgingInput(destination="测试市", avoid_keywords=["招待所"])
    )

    assert [item.name for item in result.candidates] == ["可靠酒店"]


def test_filters_candidates_without_location_or_identity(monkeypatch):
    pois = [
        _poi("valid", "有效酒店"),
        _poi("no-location", "无坐标酒店", lng=None, lat=None),
        _poi(None, "无身份酒店", address=None),
    ]
    _mock_amap(monkeypatch, pois)

    result = LodgingTool().run(LodgingInput(destination="测试市"))

    assert [item.name for item in result.candidates] == ["有效酒店"]


def test_deduplicates_same_poi_across_queries(monkeypatch):
    hotel = _poi("same", "同一家酒店")
    _mock_amap(monkeypatch, [hotel])

    result = LodgingTool().run(
        LodgingInput(destination="测试市", preferences=["四星"], spots=["景点甲"])
    )

    assert [item.name for item in result.candidates] == ["同一家酒店"]


def test_run_does_not_mutate_input(monkeypatch):
    monkeypatch.setattr(amap_client, "is_enabled", lambda: False)
    lodging_input = LodgingInput(
        destination=" 北京 ",
        preferences=["四星", "四星"],
        avoid_keywords=["民宿", "民宿"],
        spots=["故宫", "故宫"],
    )

    result = LodgingTool().run(lodging_input)

    assert lodging_input.destination == " 北京 "
    assert lodging_input.preferences == ["四星", "四星"]
    assert lodging_input.avoid_keywords == ["民宿", "民宿"]
    assert lodging_input.spots == ["故宫", "故宫"]
    assert result.city == "北京"
    assert result.candidates == []


def test_result_is_limited_to_five_candidates(monkeypatch):
    pois = [_poi(f"hotel-{index}", f"测试酒店{index}") for index in range(10)]
    _mock_amap(monkeypatch, pois)

    result = LodgingTool().run(LodgingInput(destination="测试市"))

    assert len(result.candidates) == 5


def _run_integration_case(title: str, **kwargs) -> None:
    lodging_input = LodgingInput(**kwargs)
    result = lodging_tool.run(lodging_input)
    print(f"\n========== {title} ==========")
    print(
        f"输入: 目的地={lodging_input.destination} "
        f"偏好={lodging_input.preferences or '无'} "
        f"规避={lodging_input.avoid_keywords or '无'} "
        f"景点={lodging_input.spots or '无'}"
    )
    print(f"候选 {len(result.candidates)} 家:")
    for index, candidate in enumerate(result.candidates, 1):
        print(
            f"  {index}. {candidate.name} [{candidate.area or '区域未知'}] "
            f"评分={candidate.rating or '未知'} 档次={candidate.keytag or '未知'}"
        )
        if candidate.address:
            print(f"     地址: {candidate.address}")
        if candidate.tel:
            print(f"     电话: {candidate.tel}")
    print(f"摘要: {result.summary}")
    if result.debug:
        print(
            f"统计: 查询{result.debug['query_count']}条 → 原始{result.debug['raw_candidate_count']}家 "
            f"→ 过滤后{result.debug['filtered_candidate_count']}家 "
            f"(无评分{result.debug['no_rating_count']} 无档次{result.debug['no_keytag_count']})"
        )
    assert result.candidates, "候选为空"


@pytest.mark.integration
def test_lodging_beijing_high_end():
    _run_integration_case(
        "北京 五星/亲子 故宫+颐和园",
        destination="北京",
        preferences=["五星", "亲子"],
        avoid_keywords=["招待所"],
        spots=["故宫", "颐和园"],
    )


@pytest.mark.integration
def test_lodging_guangzhou_multi_spot():
    _run_integration_case(
        "广州 多景点",
        destination="广州",
        spots=["广州塔", "北京路", "白云山", "圣心大教堂"],
    )


@pytest.mark.integration
def test_lodging_chengdu_grade():
    _run_integration_case(
        "成都 四星 熊猫基地",
        destination="成都",
        preferences=["四星"],
        spots=["成都大熊猫繁育研究基地"],
        avoid_keywords=["酒吧"],
    )


@pytest.mark.integration
def test_lodging_default():
    _run_integration_case(
        "杭州 无偏好 西湖",
        destination="杭州",
        spots=["西湖"],
    )


@pytest.mark.integration
def test_lodging_manual():
    """手动测试：直接修改下方输入，运行查看输出结果"""
    # ===== 输入（在这里修改）=====
    _run_integration_case(
        "广州 四星 广州塔+北京路",
        destination="广州",
        preferences=["四星"],
        avoid_keywords=["招待所"],
        spots=["广州塔", "北京路"],
        top_n=5,
    )
    # ============================


@pytest.mark.integration
def test_lodging_theme():
    _run_integration_case(
        "上海 商务偏好 外滩+东方明珠",
        destination="上海",
        preferences=["商务"],
        spots=["外滩", "东方明珠"],
    )
