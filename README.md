# Travel Agent

一个可真实运行的**旅行规划多 Agent 系统**：多轮对话收集需求 → 意图识别 → 状态累积 → 调用和风天气/高德/LLM 生成行程 → 审查修复 → 支持后续改稿。已接入和风天气、高德 POI、路径规划等真实外部能力。

## 一、核心能力

- **多轮对话闭环**：`意图识别 → 状态合并 → 阶段路由 → 规划/改稿/问答` 四分支
- **LLM 优先 + 规则兜底**：意图识别、行程审查（reflection）、修复（repair）均有可运行的 fallback，LLM 不可用时系统整体降级为规则路径，仍可跑通
- **真实 grounding**：和风天气每日预报、高德 POI 检索、地理编码、路径规划均已接入
- **结构化规划管线**：景点候选收集 → 区域聚类 → 每日骨架 → 住宿适配评估 → 定向交通验证 → 行程渲染 → 校验修复
- **改稿能力**：支持 block / day / global 三级改动范围，未受影响的天保持不动
- **记忆**：跨会话保留用户偏好（节奏/亲子/夜游等）与历史行程

## 二、架构总览

```text
app/
├── agents/                     # Agent 层
│   ├── orchestrator.py         # 对话编排入口：按阶段路由到 clarify/planning/revise/qa
│   ├── planning.py             # 规划 Agent：完整规划管线（聚类→骨架→渲染→修复）
│   ├── revise.py               # 改稿 Agent：block/day/global 三策略
│   ├── reflection.py           # 行程审查（LLM + 规则）
│   ├── repair.py               # 行程修复（LLM + 规则重建）
│   ├── prompt/                 # 各 Agent 的 system prompt
│   ├── schema/                 # 各 Agent 的输入输出契约
│   └── sparse/                 # 各 Agent 的 user prompt 拼装
├── budgets/                    # token 预算跟踪
├── domain/
│   ├── intent/                 # 意图层：只做"理解"（LLM 优先 + fallback 规则）
│   ├── session/                # 会话层：只做"累积"（patch 合并 + 阶段状态机 + Redis 持久化）
│   ├── memory/                 # 用户偏好 / 行程记忆
│   └── context/                # PlanningContext 等上下文模型
├── infrastructure/
│   ├── llm/                    # OpenAI 兼容 LLM 客户端（结构化 JSON 生成 + 重试）
│   ├── amap/                   # 高德地图客户端（POI/geocode/route/weather）
│   ├── config/                 # pydantic-settings 配置
│   └── redis_client.py
├── observability/              # 结构化日志 + 指标
├── tools/                      # 工具层：attraction / weather / lodging / transport
└── server.py                   # FastAPI 入口：/chat /session /health + 静态前端

main.py                         # uvicorn 启动
static/index.html               # 前端单页应用
test/                           # 测试
```

### 分层职责

| 层 | 职责 | 关键约束 |
|---|---|---|
| 意图层 | 判定意图 + 提取**本轮新增字段**（patch） | patch-only，不碰会话状态 |
| 会话层 | 白名单合并 patch + 状态机推进阶段 + 乐观并发持久化 | 字段单一来源，杜绝覆盖已确认需求 |
| Agent 层 | 规划 / 改稿 / 审查 / 修复 | 输入输出均有 pydantic 契约校验 |
| 工具层 | 对接和风/高德与 LLM，产出结构化证据 | 失败静默降级，不阻断主链路 |

## 三、一次对话的完整链路

```text
POST /chat
  └─ TravelOrchestrator.handle
      ├─ 1. 加载会话（Redis 乐观锁）+ 填充行程摘要 + 构建用户偏好
      ├─ 2. IntentSessionPipeline.run（意图识别 + patch 合并 + 状态机）
      ├─ 3. stage → 执行模式
      │     ├─ clarify   缺字段 → 追问补全
      │     ├─ planning  → planning_agent.run_pipeline
      │     ├─ revise    → revise_agent.run
      │     └─ qa        → LLM 自由对话（携带行程摘要）
      └─ 4. 乐观保存会话（冲突自动重试）
```

### 规划管线（planning_agent）

```text
字段校验 → 工具收集(attraction/weather/lodging)
→ LLM 区域聚类(cluster plan) → 必要时补充景点
→ LLM 每日骨架(skeleton，校验选点∈候选池)
→ 住宿适配评估/刷新 → 定向交通验证
→ LLM 行程渲染(draft) → 轻量校验修复(时间块/必去覆盖/结束过早)
→ 组装 TripPlan → 持久化用户/行程记忆
```

### 改稿管线（revise_agent）

```text
改稿意图归一 → 影响范围分析(impact)
→ 按需刷新工具证据(weather/attraction/lodging/transport)
→ 按 scope 走 block/day/global 策略（未受影响的天原样保留）
→ 行程审查(reflection) → 需要时修复(repair) → 更新产物
```

## 四、运行方式

### 环境要求

- Python 3.11+
- Redis（本地或 Docker）
- 高德 Key（可选，缺省时 POI/交通降级）
- 和风天气 Key（可选，缺省时天气降级）
- LLM Key（可选，缺省时走规则 fallback）

### 1. 本地启动

```bash
# 准备环境变量
cp .env.example .env   # 填入 AMAP_KEY / OPENAI_API_KEY 等

# 安装依赖
pip install -e .

# 启动服务（需本地 Redis）
python main.py
```

访问 `http://localhost:8000` 打开对话界面，`http://localhost:8000/docs` 查看接口。

### 2. Docker 启动

```bash
docker compose up --build
```

自带 Redis 服务，无需本地安装。

### 3. 运行测试

```bash
pip install -e ".[dev]"
pytest
```

测试通过 monkeypatch 禁用 LLM，固定走意图识别 fallback 路径，不依赖外部 Key。

## 五、环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `AMAP_KEY` | 高德 Web 服务 Key（POI/路径规划） | 空（POI/交通降级） |
| `QWEATHER_API_KEY` | 和风天气 API Key（X-QW-Api-Key 认证） | 空（天气降级） |
| `QWEATHER_HOST` | 和风项目自定义域名，如 `xxx.re.qweatherapi.com` | 空（天气降级） |
| `QWEATHER_FORECAST_DAYS` | 订阅支持的最大预报天数（weather 按行程日期自动选 3/7/10/15/30d 端点，不超过此上限） | `30` |
| `QWEATHER_TIMEOUT_SECONDS` | 和风请求超时 | `10.0` |
| `REDIS_URL` | Redis 连接串 | 空（需配置） |
| `LLM_PROVIDER` | `qwen` / `openai` / `mock` | `mock` |
| `OPENAI_API_KEY` | OpenAI 兼容 Key | 空 |
| `OPENAI_BASE_URL` | 兼容端点，如通义 `https://dashscope.aliyuncs.com/compatible-mode/v1` | 空 |
| `OPENAI_MODEL` | 模型名 | `qwen-plus` |
| `ENABLE_MOCK_LLM` | 为 `true` 时强制走规则路径，不调 LLM | `true` |
| `REDIS_TTL_SECONDS` | 会话 TTL | 86400 |

## 六、核心设计

- **patch-only 单向累计**：意图层只输出本轮新增字段，会话层白名单合并，避免用户已确认的信息被覆盖
- **乐观并发**：Redis `WATCH + version` 保证同一会话并发请求不互相覆盖；`save_with_artifacts` 将会话状态与行程产物原子提交，杜绝"新行程 + 旧摘要"错位
- **字段单一来源**：必填字段、patch 白名单从 schema 派生，多处引用同一常量，避免口径漂移
- **LLM 输出消毒**：结构化输出经 pydantic validator 白名单过滤、类型归一、非法值丢弃并留痕
- **日期归一**：统一 `YYYY-MM-DD`，支持跨年进位、区间方向修正、日期与天数一致性推导

## 七、当前边界与后续方向

**已成熟**：多轮对话闭环、意图/会话分层、规划/改稿管线、高德 grounding、降级策略。

**仍偏原型**：
- 规则修复（repair）较机械，与 LLM 渲染风格存在差异
- 改稿的交通证据只覆盖前几对关键转场
- 缺少 lodging / budget / dining 等更多审查维度

**建议方向**：补全审查维度、增强规则修复与 LLM 渲染的一致性、扩充测试覆盖（会话合并 / 并发 / 编排分支）。
