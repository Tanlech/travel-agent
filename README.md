# Travel Agent

一个可真实运行的**旅行规划多 Agent 系统**：多轮对话收集需求 → 意图识别 → 状态累积 → 调用和风天气/高德/LLM 生成行程 → 审查修复 → 支持后续改稿。已接入和风天气、高德 POI、地理编码与路径规划等真实外部能力，并带有景点知识库（Qdrant + RAG）与 Web 管理后台。

## 一、核心能力

- **多轮对话闭环**：`意图识别 → 状态合并 → 阶段路由 → 规划/改稿/问答` 四分支，可在一次会话内连续追问、改稿
- **LLM 优先 + 规则兜底**：意图识别、行程审查（reflection）、修复（repair）均有可运行 fallback；LLM 不可用时整体降级为规则路径，仍能跑通
- **真实 grounding**：和风天气每日预报、高德 POI/地理编码/路径规划均已接入
- **结构化规划管线**：景点候选收集 → 区域聚类 → 每日骨架 → 住宿适配评估 → 定向交通验证 → 行程渲染（多天并行）→ 校验修复
- **工具调用**：Agent 按需自主决定调用哪些工具、调用几次与参数（LLM function-calling），交通/餐饮按景点分布独立多次检索候选池
- **真实交通**：转场路线三种方式（步行/公交/驾车）并行拉取高德真实轨迹与耗时，耗时向上取 5 分钟整数，后开路线用真实耗时重排时间槽
- **地图可视化**：行程卡片可打开地图，按时间线连线显示景点/餐饮/交通点（不同图标样式），并绘制真实路径轨迹
- **知识库 RAG**：景点知识库（Qdrant 向量检索 + fastembed 交叉重排）为渲染注入上下文
- **改稿能力**：支持 block / day / global 三级改动范围，未受影响的天保持不动
- **记忆**：跨会话保留用户偏好（节奏/亲子/夜游等）与历史行程
- **Web 后台**：账号体系（MySQL）+ 管理后台（知识库 / 调度排程），携带 SSE 流式对话

## 二、架构总览

```text
app/
├── server.py                  # FastAPI 入口：/chat /chat/stream(SSE) /session /memory /health + admin
├── admin/                     # 管理后台
│   ├── account/               #   用户账号（JWT 登录 + 管理员种子账号）
│   ├── knowledge_admin/       #   知识库管理（guide 构建 / 调度）
│   └── routes.py
├── agent/                     # Agent 层
│   ├── agents/
│   │   ├── orchestrator.py    #   对话编排：按阶段路由 clarify/planning/revise/qa
│   │   ├── planning.py        #   规划：完整管线（聚类→骨架→渲染→校验→路线）
│   │   ├── revise.py          #   改稿：block/day/global 三策略
│   │   ├── reflection.py      #   行程审查（规则 + LLM）
│   │   ├── repair.py          #   行程修复（规则 + LLM 重建）
│   │   ├── prompt/  schema/  sparse/   # 各 Agent 的 system prompt / 契约 / user prompt
│   ├── domain/
│   │   ├── intent/            #   意图层：只做"理解"（LLM 优先 + fallback 规则）
│   │   ├── session/           #   会话层：只做"累积"（patch 合并 + 状态机 + Redis 持久化）
│   │   ├── memory/            #   用户偏好 / 行程记忆
│   │   ├── context/           #   PlanningContext 等上下文模型
│   │   └── common/            #   日期/时间/意图类型/行程/交通等公共模型
│   ├── knowledge/             # 知识库：ingest(chat/qa 增量) + 分块/向量/重排/检索/存储（Qdrant）
│   └── tools/                 # 工具层：attraction / weather / lodging / meal / transport + registry
├── infrastructure/            # 基础设施（单文件实现）
│   ├── llm_client.py          #   OpenAI 兼容 LLM（结构化 JSON + 重试 + 工具调用）
│   ├── amap_client.py         #   高德：POI / 地理编码 / 路径规划（driving/transit/walking）
│   ├── qweather_client.py     #   和风天气：城市搜索 + 逐日预报（3/7/10/15/30d）
│   ├── redis_client.py        #   会话 / 缓存
│   ├── mysql_client.py        #   账号体系
│   ├── settings.py            #   pydantic-settings 配置
│   └── conversions.py
├── observability/             # 结构化日志 + token 预算 + tracing
└── server.py

frontend/                      # Vue 3 + Vite 单页应用
  src/views/  ChatView / AdminView / LoginView
  src/components/  PlanCard(行程卡片) / PlanMapModal(地图) / KnowledgeAdmin / SchedulerAdmin
  static/                        # 构建产物，由后端直接托管

main.py                         # uvicorn 启动（host=0.0.0.0 port=8001）
```

### 分层职责

| 层 | 职责 | 关键约束 |
|---|---|---|
| 意图层 | 判定意图 + 提取**本轮新增字段**（patch） | patch-only，不碰会话状态 |
| 会话层 | 白名单合并 patch + 状态机推进 + 乐观并发持久化 | 字段单一来源，杜绝覆盖已确认需求 |
| Agent 层 | 规划 / 改稿 / 审查 / 修复 | 输入输出均有 pydantic 契约校验 |
| 工具层 | 对接和风/高德与 LLM，产出结构化证据 | 失败静默降级，不阻断主链路 |

## 三、一次对话的完整链路

```text
POST /chat  (或 /chat/stream SSE 流式)
  └─ TravelOrchestrator.handle
      ├─ 1. 加载会话（Redis 乐观锁）+ 填充行程摘要 + 构建用户偏好
      ├─ 2. IntentSessionPipeline.run（意图识别 + patch 合并 + 状态机）
      ├─ 3. stage → 执行模式
      │     ├─ clarify  缺字段 → 追问补全
      │     ├─ planning → planning_agent.run_pipeline
      │     ├─ revise   → revise_agent.run
      │     └─ qa       → LLM 自由对话（携带行程摘要）
      └─ 4. 乐观保存会话（冲突自动重试）
```

### 规划管线（planning_agent）

```text
字段校验 → 工具收集(attraction/weather/lodging)
→ LLM 区域聚类(cluster plan) → 必要时补充景点
→ LLM 每日骨架(skeleton，校验选点∈候选池)
→ 住宿适配评估/刷新 → LLM 工具循环自主检索交通/餐饮候选
→ LLM 行程渲染(draft，多天 ThreadPool 并行) → 校验修复(时间块/必去/晚餐/交通)
→ 并行拉取转场真实路线(driving/transit/walking) → 按真实耗时重排时间槽
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

- Python 3.11+、Node 18+（仅改前端时需要构建）
- Redis、MySQL（账号体系）、Qdrant（知识库）
- 高德 Key（可选，缺省时 POI/交通降级）
- 和风天气 Key（天气 + 城市搜索两个 key）
- LLM Key（可选，缺省走规则 fallback）

### 1. 本地启动（后端）

```bash
# 准备环境变量
cp .env.example .env   # 填入 AMAP_KEY / QWEATHER_* / OPENAI_API_KEY / MYSQL_* / REDIS_URL 等

# 安装依赖
pip install -e .

# 启动服务（需 Redis；MySQL/Qdrant 按需启用）
python main.py
```

访问 `http://localhost:8001/docs` 查看接口；聊天界面 `http://localhost:8001/`。

> 注意实际监听端口为 **8001**（见 `main.py`）。

### 2. Docker 启动

```bash
docker compose up --build
```

自带 Redis / MySQL 等服务，无需本地安装。

### 3. 前端构建（可选）

后端托管的是 `frontend/static` 下的构建产物。改动前端源码后需重新构建：

```bash
cd frontend
npm install
npm run build    # 产物输出到 frontend/static
```

开发时可 `npm run dev` 配合后端 CORS（已放开）联调。

### 4. 运行测试

```bash
pip install -e ".[dev]"
pytest
```

测试通过 monkeypatch 禁用 LLM，固定走意图识别 fallback 路径，不依赖外部 Key。

## 五、环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `AMAP_KEY` | 高德 Web 服务 Key（POI/路径规划） | 空（POI/交通降级） |
| `AMAP_BASE_URL` / `AMAP_TIMEOUT_SECONDS` | 高德端点与超时 | `https://restapi.amap.com` / `10.0` |
| `QWEATHER_API_KEY` | 和风天气**预报** API Key | 空（天气降级） |
| `QWEATHER_GEO_API_KEY` | 和风天气**城市搜索** API Key | 空（天气降级） |
| `QWEATHER_HOST` | 和风项目自定义域名，如 `xxx.re.qweatherapi.com` | 空（天气降级） |
| `QWEATHER_FORECAST_DAYS` | 订阅支持的最大预报天数（自动选 3/7/10/15/30d 端点） | `30` |
| `QWEATHER_TIMEOUT_SECONDS` | 和风请求超时 | `10.0` |
| `REDIS_URL` / `REDIS_TTL_SECONDS` | Redis 连接串与会话 TTL | `redis://localhost:6379/0` / `604800` |
| `MYSQL_HOST` / `PORT` / `USER` / `PASSWORD` / `DATABASE` | 账号体系 MySQL 连接 | travel / travel / travel_agent |
| `AUTH_TOKEN_TTL_SECONDS` | 登录令牌有效期 | `604800` |
| `ADMIN_SEED_USERNAME` / `ADMIN_SEED_PASSWORD` | 首次启动自动创建的管理员账号 | admin / 空 |
| `LLM_PROVIDER` | `qwen` / `openai` / `mock` | `qwen` |
| `OPENAI_API_KEY` / `BASE_URL` / `MODEL` | OpenAI 兼容端点与模型 | `qwen-plus` |
| `LLM_TEMPERATURE` / `LLM_TIMEOUT` | 生成温度与超时 | `0.2` / `60` |
| `ENABLE_MOCK_LLM` | 为 `true` 强制走规则路径，不调 LLM | `false` |
| `ATTRACTION_PERSIST_ENABLED` | 确认的主要景点写回 json 并同步 Qdrant（后台异步） | `true` |
| `QDRANT_URL` | 知识库向量库地址 | `http://localhost:6333` |
| `RERANK_MODEL` | RAG 交叉重排模型（默认关闭，注释即启用） | 空 |
| `RETRIEVAL_CANDIDATE_K` | 检索候选数量 | — |

## 六、核心设计

- **patch-only 单向累计**：意图层只输出本轮新增字段，会话层白名单合并，避免用户已确认信息被覆盖
- **乐观并发**：Redis `WATCH + version` 保证同一会话并发请求不互相覆盖；`save_with_artifacts` 将会话与行程产物原子提交
- **字段单一来源**：必填字段、patch 白名单从 schema 派生，多处引用同一常量
- **LLM 输出消毒**：结构化输出经 pydantic validator 白名单过滤、类型归一、非法值丢弃并留痕
- **LLM 工具循环**：规划充分信任 LLM 自主决定工具调用（次数与参数），降低固定脚本僵化，但以 `max_rounds` 收束避免失控
- **并行化**：多天行程渲染、转场多方式路线、跨天交通抓取均用 `ThreadPoolExecutor` 并行执行；LLM/高德客户端线程安全
- **真实交通落地**：路线耗时向上取 5 分钟整数（6→10、13→15），并以其真实耗时重排交通/返程时间槽，避免与后续活动重叠
- **规则优先校验**：校验修复阶段先用规则复检（零 LLM）把关，行程满足全部不变式时直接放行，大幅压缩 LLM 评审耗时
- **日期归一**：统一 `YYYY-MM-DD`，支持跨年进位、区间方向修正、日期与天数一致性推导

## 七、当前边界与后续方向

**已成熟**：多轮对话闭环、意图/会话分层、规划/改稿管线、高德/和风 grounding、知识库 RAG、并行渲染与并行交通、降级策略、账号与后台。

**仍偏原型**：
- 规则修复（repair）较机械，与 LLM 渲染风格存在差异
- 高德部分路线偶发失败时交通块只保留标题、不展示路线详情
- 改稿的交通证据只覆盖前几对关键转场

**建议方向**：增强规则修复与 LLM 渲染的一致性、扩充审查维度（lodging / budget / dining）、提升路线失败的重试/坐标重建兜底、扩充测试覆盖（会话合并 / 并发 / 编排分支）。