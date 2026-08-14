"""memory 层融合逻辑测试（使用 InMemoryStore 注入，不依赖 Redis）。"""

from app.agents.schema.planning import PlanningRequest
from app.domain.memory.manager import MemoryManager, _parse_preference_item
from app.domain.memory.store import InMemoryStore, MAX_TRIP_MEMORIES
from app.domain.memory.schema import TripMemory


def _manager() -> MemoryManager:
    return MemoryManager(store=InMemoryStore())


def _request(*preferences: str) -> PlanningRequest:
    return PlanningRequest(destination="北京", days=3, preferences=list(preferences))


# ---- 否定偏好提取与否定防御 ----

def test_negation_goes_to_disliked():
    ctx = _manager().build_user_context(_request("不要紧凑", "轻松"), user_id="u1")
    assert "紧凑" in ctx.disliked_styles
    assert ctx.pace_preference == "relaxed"  # "紧凑"未触发 dense


def test_negation_does_not_trigger_positive():
    ctx = _manager().build_user_context(_request("不轻松"), user_id="u1")
    assert "轻松" in ctx.disliked_styles
    assert ctx.pace_preference is None  # "不轻松"未触发 relaxed


def test_prefix_precedence():
    prefix, body = _parse_preference_item("不喜欢夜店")
    assert (prefix, body) == ("不喜欢", "夜店")


# ---- 正向信号 ----

def test_positive_keywords():
    ctx = _manager().build_user_context(_request("轻松", "乐园", "亲子"), user_id="u1")
    assert ctx.pace_preference == "relaxed"
    assert ctx.accept_theme_park is True
    assert ctx.family_friendly is True


def test_negation_writes_boolean_false():
    ctx = _manager().build_user_context(_request("不要乐园", "讨厌夜游"), user_id="u1")
    assert ctx.accept_theme_park is False
    assert ctx.accept_nightlife is False


def test_conflict_positive_wins():
    ctx = _manager().build_user_context(_request("乐园", "不要乐园"), user_id="u1")
    assert ctx.accept_theme_park is True  # 矛盾时正向优先


# ---- 存储融合 ----

def test_stored_merge_and_persist_roundtrip():
    m = _manager()
    # 第一轮：正向 + 负面偏好
    ctx1 = m.build_user_context(_request("美食", "不要夜游"), user_id="u1")
    m.persist_user_memory("u1", ctx1)
    # 第二轮：无新偏好，读回已持久化的记忆（含负面）
    ctx2 = m.build_user_context(_request(), user_id="u1")
    assert "美食" in ctx2.preferred_styles
    assert "夜游" in ctx2.disliked_styles


def test_negative_persisted_and_survives():
    m = _manager()
    m.build_user_context(_request("讨厌紧凑"), user_id="u1")  # 不持久化不生效
    ctx_before = m.build_user_context(_request(), user_id="u1")
    assert "紧凑" not in ctx_before.disliked_styles
    ctx = m.build_user_context(_request("讨厌紧凑"), user_id="u1")
    m.persist_user_memory("u1", ctx)
    ctx_after = m.build_user_context(_request(), user_id="u1")
    assert "紧凑" in ctx_after.disliked_styles


def test_anonymous_skipped():
    m = _manager()
    assert m.build_user_context(_request("轻松"), user_id=None).preferred_styles == ["轻松"]
    m.persist_user_memory(None, m.build_user_context(_request("轻松"), user_id=None))
    assert m.build_user_context(_request(), user_id=None).preferred_styles == []


# ---- 行程记忆上限 ----

def test_trip_memory_cap():
    store = InMemoryStore()
    m = MemoryManager(store=store)
    for i in range(MAX_TRIP_MEMORIES + 3):
        m.persist_trip_memory(
            "u1", PlanningRequest(destination=f"地{i}", days=2),
            accepted_spots=[], rejected_spots=[], summary=f"s{i}",
        )
    memories = store.load_trip_memories("u1")
    assert len(memories) == MAX_TRIP_MEMORIES
    assert memories[0].destination == "地3"  # 最旧 3 条被裁剪
    assert all(isinstance(t.created_at, str) and t.created_at for t in memories)


# ---- TripMemory 字段语义 ----

def test_trip_memory_response_mode():
    store = InMemoryStore()
    m = MemoryManager(store=store)
    m.persist_trip_memory("u1", _request(), [], [], "sum", response_mode="revise_plan")
    assert store.load_trip_memories("u1")[0].response_mode == "revise_plan"


def test_persist_trip_requires_user():
    store = InMemoryStore()
    MemoryManager(store=store).persist_trip_memory(None, _request(), [], [], "s")
    assert store.load_trip_memories(None) == []
