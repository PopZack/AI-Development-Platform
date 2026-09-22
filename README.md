# AI Dev Team ——  AI 软件开发团队协作平台

用户提交一个软件开发需求，AI 团队依次完成需求分析、技术设计、代码变更、测试与审查，最后由人工确认结果。

> **当前进度：Stage 1（基础 API + 业务数据）已完成。**
> 参见设计文档 §11 的分阶段实施计划。

---

## 这个项目要做什么（首期闭环）

```
用户 → 注册/登录 → 创建项目 → 提交需求 → 创建工作流
  → Product Agent(PRD) → Architect Agent(技术设计) → Developer Agent(变更计划/Patch)
  → Tool Gateway → Tester Agent → Reviewer Agent → 人工审批 → 交付物归档
```

首期的代码变更只做「生成 Patch + 保存说明 + 路径校验」，**不做**任意 Shell 执行、
自动 Git Push、自动部署。也就是说，它验证的是后端与 Agent 协作是否可靠，
而不是让模型直连生产。

核心原则（贯穿全部实现）：

| 原则 | 落地方式 |
|---|---|
| AI 负责理解与生成，后端负责边界 | 权限、状态、数据、工具白名单全在后端 |
| 不让 LLM 决定业务权限 | 工具调用一律过 Tool Gateway，模型只能「请求」 |
| Agent 输出必须可验证 | 模型输出先过 Pydantic Schema 校验，不合格不落库 |
| 事务与模型调用分离 | 先提交状态，再调外部服务，最后开新事务存结果 |
| Router 不承载业务逻辑 | Router 只做请求校验 + 转交 Service |
| 客户端不能改业务状态 | 状态只能由 Domain 规则推进，请求体里没有 status 字段 |

---

## 环境与依赖

| 项 | 选择 |
|---|---|
| Python | 3.12+（开发环境实测 3.13） |
| 包管理 | uv |
| API | FastAPI |
| ORM | SQLAlchemy 2.x（async + aiosqlite） |
| 数据库 | SQLite（首期） |
| 校验 | Pydantic 2.x |
| 密码哈希 | Argon2 |
| 测试 | pytest + pytest-asyncio + httpx |
| 代码规范 | Ruff |
| LLM | 火山方舟（通过 Provider 抽象接入，可替换） |

### 本地启动

```bash
uv sync                      # 安装依赖（含 dev 组）
cp .env.example .env         # 按需修改配置
uv run uvicorn app.main:app --reload
```

- Swagger：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

数据库与表在应用启动时自动创建（`data/ai_dev_team.db`）。**Alembic 迁移按设计文档
§11 留到 Stage 5**，所以现在改表结构直接删掉本地 `data/` 重建即可。

### 跑测试与规范检查

```bash
uv run pytest              # 73 个用例
uv run ruff check .        # lint
uv run ruff format .       # 格式化
```

---

## 分层结构

```
app/
├── api/              Router：接收 HTTP、请求校验、响应转换
│   ├── router.py     v1 路由汇总
│   └── v1/           users / projects / requirements
├── application/      Service：编排业务用例，持有事务边界
├── domain/           实体状态机与不变量（不依赖 FastAPI / SQLAlchemy / LLM）
├── repositories/     数据读写，不做业务判断
├── models/           SQLAlchemy ORM 表映射
├── schemas/          Pydantic 请求 / 响应模型
├── infrastructure/   数据库、密码哈希、LLM Provider、工作区、工具
├── agent/            Agent 角色与统一运行时（Stage 3）
├── workflow/         显式工作流状态机与编排（Stage 4）
└── common/           配置、异常、日志、request_id、DI
```

依赖方向单向：`api → application → domain / repositories → infrastructure`。
判断标准很简单 —— **Domain 目录里不允许出现 `import fastapi` 或任何 LLM 客户端**。

### 三个容易混的对象，刻意分开

- **SQLAlchemy Model**（`app/models/`）：数据库表映射
- **Domain 逻辑**（`app/domain/`）：业务状态与规则
- **Pydantic Schema**（`app/schemas/`）：请求与响应校验

小项目初期这三者结构相近，但不能合并成一个类，否则后面加权限和状态规则时会互相绊住。

---

## 已实现接口

统一前缀 `/api/v1`。

### 认证（Stage 2）

| Method | Path | 认证 | 说明 |
|---|---|---|---|
| POST | `/auth/register` | 否 | 注册。邮箱统一小写后落库 |
| POST | `/auth/login` | 否 | 登录，返回访问令牌 |
| GET | `/auth/me` | **是** | 当前用户 |
| POST | `/auth/logout` | **是** | 登出，撤销该用户**全部**会话 |
| POST | `/auth/password` | **是** | 改密码，并连带撤销全部会话 |

### 项目与需求（Stage 2，已接资源级权限）

| Method | Path | 要求 | 说明 |
|---|---|---|---|
| POST | `/projects` | 登录 | 创建者自动成为 Owner。**owner 取自令牌，请求体里没有这个字段** |
| GET | `/projects` | 登录 | 只返回自己参与的项目。**没有**「查看别人列表」的开关 |
| GET | `/projects/{id}` | 项目成员 | |
| PATCH | `/projects/{id}` | **OWNER** | |
| GET | `/projects/{id}/members` | 项目成员 | |
| POST | `/projects/{id}/members` | **OWNER** | |
| POST | `/projects/{id}/requirements` | **OWNER / DEVELOPER** | **created_by 取自令牌** |
| GET | `/projects/{id}/requirements` | 项目成员 | |
| GET | `/requirements/{id}` | 该需求所属项目的成员 | |
| PATCH | `/requirements/{id}` | **OWNER / DEVELOPER** | 内容变更会使 version 自增 |

### 用户（Stage 2，已接鉴权）

| Method | Path | 要求 | 说明 |
|---|---|---|---|
| GET | `/users` | 登录 | 任何登录用户可见 —— 添加项目成员前要能查到对方 id |
| GET | `/users/{id}` | 登录 | 同上 |
| PATCH | `/users/{id}` | **仅本人** | 只能改 `display_name`；账号状态不属于自助修改范围 |

不再有 `POST /users` —— 注册统一走 `/auth/register`。同一个业务动作保留两条入口，
两边的校验规则迟早会走偏（比如一边统一小写邮箱、另一边忘了）。

### 工作流（Stage 2，仅创建与查询）

| Method | Path | 要求 | 说明 |
|---|---|---|---|
| POST | `/requirements/{id}/runs` | **OWNER / DEVELOPER** | 创建工作流，必须带 `Idempotency-Key` 头 |
| GET | `/runs/{run_id}` | 该需求所属项目的成员 | 查询运行状态 |

```http
POST /api/v1/requirements/{requirement_id}/runs
Idempotency-Key: requirement-001-v1
Authorization: Bearer <access_token>
```

响应约定：

| 情况 | 状态码 | 说明 |
|---|---|---|
| 新建成功 | `201` | 返回这条工作流 |
| 同一个键重放 | `200` | 响应头带 `Idempotent-Replay: true`，返回**当初那条**（不是新记录） |
| 同一个键用在另一份需求 | `409` | `IDEMPOTENCY_KEY_CONFLICT` —— 这是客户端 bug，不是重试 |
| 没带 `Idempotency-Key` | `422` | 不带就不给建，否则「双击启动」会安静地产生两条 |

**为什么幂等键是必须的而不是可选的**：创建工作流是「要么跑一次、要么重试」的操作。
如果只靠应用层「先查再插」，并发下两个请求会同时读到「不存在」然后同时插入 ——
真正兜底的是 `idempotency_key` 上的 UNIQUE 约束，Service 会捕获 `IntegrityError`
并把已存在的那条读回来按重放返回（而不是把 500 抛给客户端）。

**只创建、不执行**：新建的运行停在 `status=CREATED`、`current_step=PENDING`，
`started_at` 为 `null`。文档 §7.4 里的 `start` / `pause` / `resume` / `cancel` 与
`artifacts` **刻意没有实现** —— 它们要驱动状态机、跑 Agent、产生交付物，分别属于
Stage 4 和 Stage 3。现在加上只会是一组点了没反应的接口，比不提供更糟：调用方会以为功能可用。

> 文档 §6.3 给 `workflow_runs` 定义了 `status` 和 `current_step` 两个 NOT NULL 字段，
> 但**没说它们的区别**。本项目的划分：`status` 是 §3.3 那台状态机（对外可见的阶段），
> `current_step` 是该阶段内部正在等哪个环节（排障粒度，取值见 `WorkflowStep`）。

### Agent 接口（Stage 3，会真的调模型）

| Method | Path | 要求 | 说明 |
|---|---|---|---|
| POST | `/requirements/{id}/analyze` | **OWNER / DEVELOPER** | Product Agent 生成 PRD。状态 `DRAFT → ANALYZING` |
| POST | `/requirements/{id}/plan` | **OWNER / DEVELOPER** | Architect Agent 生成技术设计。状态 `ANALYZING → DESIGNED`，**必须先 analyze** |
| GET | `/requirements/{id}/artifacts` | 项目成员（含 VIEWER） | 列出交付物，按版本升序，可用 `type=PRD/ARCHITECTURE` 过滤 |

**两个要提前知道的特性：**

- **慢**：同步接口，实测一次 20~40 秒（等模型返回）。文档 §13 的异步执行 + SSE 属 Stage 5。
  因此 `LLM_TIMEOUT_SECONDS` 默认已调到 180 —— 60s 会超时（504），需求会被标成 `FAILED`。
- **花钱**：每次几十到几千 token。VIEWER 能看结果但不能触发。

流程与失败语义：

```
DRAFT --analyze--> ANALYZING --plan--> DESIGNED
                     ↑                  │
                     └──── re-analyze ──┘   （需求改了要重跑）
任意非终态 --失败--> FAILED --analyze--> （可恢复，不会永久锁死）
```

- 产出写两处：`artifacts`（按版本递增，保留历史）+ `requirements.prd_json`（当前版本快照）。
  只有 PRD 有需求列可写；技术设计只落 `artifacts`，当前版本靠 version 最大的那条。
- 模型输出未通过 Schema 校验（重试 2 次仍不行）→ 需求变 `FAILED` 并返回 502，
  重新调用即可重试。**校验没过的内容绝不会写进交付物。**
- `agent_runs` 刻意不通过 API 暴露：那是运维排障数据（每次尝试的原文、token、耗时、失败原因）。

### 权限模型

只有 **项目级** 角色，没有全局管理员（设计文档只定义了 Owner / Developer）：

| 角色 | 读项目 | 读需求 | 建/改需求 | 改项目 · 管成员 |
|---|---|---|---|---|
| OWNER | ✅ | ✅ | ✅ | ✅ |
| DEVELOPER | ✅ | ✅ | ✅ | ❌ 403 |
| VIEWER | ✅ | ✅ | ❌ 403 | ❌ 403 |
| 非成员 | ❌ 403 | ❌ 403 | ❌ 403 | ❌ 403 |

两条值得说明的取舍：

- **越权返回 403，不伪装成 404。** 项目 ID 是 UUID 不可枚举，泄露「资源存在」的风险很低，
  而 403 对排查问题明显更有帮助。代价是调用方能区分「不存在」和「没权限」——
  如果哪天 ID 变成可枚举的，应该改成对外 404。
- **角色不足时，响应里带上实际角色和所需角色**（`details.actual_role` / `details.required_roles`）。
  前端因此不用靠猜，也不用去翻文档 —— 文档会和代码走偏，报错不会。

### 鉴权方式

```http
Authorization: Bearer <access_token>
```

令牌缺失、过期、被篡改、已被撤销分别返回 `AUTHENTICATION_REQUIRED` /
`TOKEN_EXPIRED` / `TOKEN_INVALID` / `TOKEN_REVOKED` 四个不同的 code，
客户端据此决定是「去登录」还是「报障」。这些差异不回给未认证的调用方（否则等于提示攻击者差在哪一步）。

### 会话撤销（为什么 `/auth/logout` 不是空转）

访问令牌是无状态 JWT，所以「登出」不可能是「删掉一条服务端会话记录」。
本项目的做法是在 `users.token_version` 上留一个整数版本号：

1. 签发令牌时，把当时的 `token_version` 写进载荷的 `ver`
2. 每次鉴权比对令牌里的 `ver` 与用户当前的 `token_version`，不一致即拒（`TOKEN_REVOKED`）
3. `/auth/logout` 和 `/auth/password` 都把 `token_version` 加一

选它而不是「`jti` 黑名单表」的关键原因：**鉴权本来就要读 users 这一行**（判断账号状态、
查项目成员关系），所以版本比对是零额外查询；黑名单方案则要给每个请求硬加一次查表或查 Redis，
等于拿鉴权主链路的开销去换一个低频操作的能力。

代价说清楚：**撤销粒度是「这个人的所有设备」**，做不到只踢掉某一台。要做单设备撤销需要改成
服务端会话表（短过期 Access Token + Refresh Token），届时 `/auth/login`、`/auth/logout` 的
路径与语义都不用变，只是把用户级撤销收紧成会话级。

顺带拿到的一个真实收益：**改密码会自动把攻击者踢出去**，而不是等他手上的令牌自然过期。

### 统一错误格式

所有错误，无论是业务异常、参数校验失败还是未捕获异常，响应体结构一致：

```json
{
  "error": {
    "code": "REQUIREMENT_NOT_FOUND",
    "message": "Requirement does not exist",
    "request_id": "req-d65035a5e6e1",
    "details": {}
  }
}
```

`request_id` 同时回写到响应头 `X-Request-ID`，全链路日志用同一个值串起来。
内部堆栈只进日志，不进响应体。

---

## LLM Provider 抽象（Stage 3）

```
app/infrastructure/llm/
├── base.py      LLMProvider 抽象 + LLMMessage / LLMRequest / LLMResponse / LLMUsage
├── errors.py    失败分类（关键：区分可重试 / 不可重试）
├── retry.py     传输层重试（指数退避，只重试可恢复的失败）
├── ark.py       火山方舟实现（唯一允许出现「方舟」的地方）
├── mock.py      Mock 实现（测试替身，长期保留）
└── factory.py   按 LLM_PROVIDER 装配 —— 全项目只有这里知道有哪些供应商
```

**Agent 层只依赖 `LLMProvider` 抽象，供应商 SDK 不渗透进去**（文档 §14.1）。
换供应商 = 新增一个实现类 + 改配置，Agent / Prompt / 校验层一行不动。

刻意**不引第三方 SDK**（openai / volcengine 等），只用 httpx：少一层依赖、
少一处版本漂移，而且各家 SDK 的异常类型不统一，抽象层反而更难做得干净。
Ark 的 `/api/v3/chat/completions` 本身就是 OpenAI 兼容协议。

### 两类「重试」必须分清

| | 传输层重试（`retry.py`） | 内容层重试（Stage 3 Agent Runtime） |
|---|---|---|
| 触发 | 超时、网络抖动、5xx、429 | 模型回的内容不是合法 JSON / 不合 Schema |
| 谁管 | 重试装饰器 | Agent Runtime |

混成一个「最多重试 N 次」会很糟：模型稳定地回错误 JSON 时传输层白等三轮退避，
而网络抖动时内容层又跑去重写 Prompt。**401 / 403 / 400 一次都不重试** ——
它们重试一万次也一样失败，只会把真正的配置问题埋进重试日志里。

### ⚠️ 火山方舟：三条路径，配错的表现是 401

方舟至少有三条 base URL，**key 的权限范围是按路径划分的**。
配错的表现是 `401 The API key or AK/SK in the request is missing or invalid.`
—— **报错信息不会告诉你是端点配错了**，只会让你以为密钥无效。

| | 平台端点 | Agent Plan | Coding Plan |
|---|---|---|---|
| Base URL | `.../api/v3` | `.../api/plan/v3` | `.../api/coding/v3` |
| 模型名 | 带日期后缀，如 `deepseek-v4-flash-260425`；或自建接入点的 `ep-xxxx` | 短名，如 `deepseek-v4-flash` | 短名 |
| Key | 平台 API Key（`ek-` 开头） | **Plan 专属 Key** | **另一个 Plan 专属 Key** |

`llm_api_key` / `llm_base_url` / `llm_model` 三者必须配成**同一套**。

本项目实测结论（2026-09-22，用同一个 Plan Key 打三条路径）：

| 路径 | `deepseek-v4-flash` | `doubao-seed-2-1-pro` |
|---|---|---|
| `/api/plan/v3` | ✅ 200 | `404 does not support the agent plan feature` |
| `/api/coding/v3` | `401`（key 不适用于这条 plan） | `404 does not support the coding plan feature` |
| `/api/v3` | `401` | `401` |

两条经验：

- **一条 plan 只覆盖部分模型。** 换模型要重新确认它在不在当前 plan 里，
  否则拿到的是 404 而不是「模型不存在」。
- **`401` 不完全等于「密钥无效」。** 先确认 `LLM_BASE_URL` 与 key 属于同一条路径。
  排查时最省事的做法是用同一个 key 打三条路径做对照 —— 403/404 会告诉你它认了哪条。

### Mock Provider 为什么长期保留

不是临时脚手架。有些分支用真实模型**根本没法稳定复现** —— 最典型的就是
「模型返回了非法 JSON 时，系统必须拒绝而不是把半成品落库」（文档 §14.3）。
你不可能靠反复真实调用来等模型输出坏 JSON。

```python
# 按脚本依次返回；脚本里可以混入异常，用来驱动重试与失败处理
MockLLMProvider(script=[LLMTimeoutError("boom"), '{"title": "ok"}'])

# 不传脚本 → 永远返回带醒目 _mock 标记的 JSON（本地手跑用，不会被误认成模型输出）
MockLLMProvider()
```

脚本用完会**报错**而不是静默回落到默认内容 —— 否则「测试少写了一条响应」
会变成一个看起来通过、其实没测到东西的用例。

### Ark 实现怎么在没有有效密钥时测试

`ArkLLMProvider` 支持注入 `transport`，所以能用 `httpx.MockTransport` 验证
**「我们到底发出了什么请求」**（URL / 认证头 / 请求体字段）以及**各类失败被映射成哪一种错误**。
Provider 抽象层最容易出的问题就是请求构造错了，而这类问题用真实调用只会看到一个
笼统的 400/401，极难定位。

密钥的**有效性与模型名归属**只能在真实调用里验证 —— 那是单独的连通性检查，不属于单元测试。

### 生产环境护栏

`APP_ENV=prod` 时，以下配置会让应用**直接启动失败**（而不是带着占位配置安静地跑）：

- `JWT_SECRET_KEY` 仍是默认值，或短于 32 字节
- `LLM_PROVIDER=mock`（生产上用 Mock 等于整个平台在演假戏，从响应上完全看不出来）
- `LLM_API_KEY` / `LLM_MODEL` 为空

本地/测试环境不受影响 —— 否则还没申请到密钥时开发会被拦死。

---

## Agent Runtime（Stage 3）

```
app/agent/
└── runtime.py   统一的「调模型 → 校验 → 落库」流程
```

**这个文件是设计文档 §14.3 那条硬线的落点**：

> AI 负责理解、生成和分析；后端负责权限、状态、数据和工具边界。
> 模型输出必须先通过 Pydantic 校验，不允许直接把模型输出写入数据库。

流程：`Prompt 组装 → provider.complete(json_mode=True) → JSON 解析 → Pydantic 校验 → 落库`

校验通过才写 `parsed_json`。**未通过校验的输出只留在 `output_json`（原文）+
`error_message`（为什么不合格）里**，没有任何「先写进去再校验」的变体。

### 内容层重试必须带上失败原因

重试不是把同一个 Prompt 再发一遍 —— 那样只是赌模型这次心情好。第二次会把
**上一次的原始输出**和**校验报错**一起发回去，让模型知道要改什么：

```
[system] 你是需求分析师…
[user]   需求原文：…
[assistant] {"priority": "P0"}                 ← 上一次的坏输出
[user]   你上一次的输出没有通过校验：输出不符合 _Plan：[{...}]
         请修正后重新输出。只输出一个符合 _Plan 结构的 JSON 对象。
```

这也是内容层重试**必须**和传输层重试分开的原因（见上文「两类重试必须分清」）。

### agent_runs 的粒度（文档没定义，这是本项目的选择）

**一行 = 一次 provider 调用**，不是「一次逻辑上的 Agent 执行」。

理由：内容层重试的价值恰恰在于「第一次模型回了坏 JSON、第二次改好了」，而这个信息
只有按次记录才能看到。一次执行只落一行的话，就只能看到最终结果，无法回答
「它是一次就成功，还是重试了三次才勉强成功」。

同一逻辑执行的多行共享 `execution_id`，`attempt` 从 1 递增。

| 字段 | 含义 |
|---|---|
| `execution_id` + `attempt` | 同一次执行的多次尝试可分组、可排序 |
| `output_json` | 模型**原始输出**，不加工。排障必须能看到原文 |
| `parsed_json` | **通过校验后**的结构化结果；`NULL` 表示没通过 |
| `status` | `SUCCEEDED` / `INVALID_OUTPUT` / `FAILED` |
| `model` | 记录**响应里**的 model。Provider 会把短名解析成具体 build（实测：请求 `deepseek-v4-flash`，返回 `deepseek-v4-flash-ga-260731`） |
| `latency_ms` / `*_tokens` | 成本与性能排查 |

`INVALID_OUTPUT` 与 `FAILED` 刻意分开：前者是**内容层**问题（模型不听话，值得重试或改
Prompt），后者是**传输层**问题（网络、限流、凭据）。混成一个状态，就没法回答
「这个 Agent 最近失败是因为模型不听话还是基础设施不稳」—— 两者处置方式完全不同。

### 传输层失败不在 Runtime 里再重试

Provider 抛出的网络/超时/限流错误已由 `RetryingLLMProvider` 处理过一轮。到这里还抛出来，
说明重试也没救，直接记为 `FAILED` 并向上抛 —— 再套一层会让退避时间成倍叠加。

---

## Tool Gateway（Stage 4）

```
app/infrastructure/tools/
├── paths.py       路径校验：解析为规范化路径后判断是否在授权根内
├── read_tools.py  L1 工具的真实实现（list_files / read_file / search_code）
└── gateway.py     唯一入口：查工具 → 权限判断 → 执行 → 落审计
```

**Gateway 是 Agent 调工具的唯一入口。** 权限分级、路径校验、「L1 允许并记录」
三条约束，只有在所有调用都过同一道门时才成立 —— 任何一个 Agent 自己直接
`open()` 文件，三条就同时失效，而且从代码上完全看不出来。

### 权限等级（文档 §14.1）

| 等级 | 含义 | 处置 |
|---|---|---|
| L0 | 读已授权上下文 | 允许 |
| L1 | 读代码 · 搜索 | 允许，**并记录** |
| L2 | 生成 Patch | 允许，要过路径与规则检查 |
| L3 | 运行测试和有限命令 | 允许，白名单 |
| L4 | 应用变更 | **需人工审批**（不是拒绝） |
| L5 | 部署 · 删数据 · 改权限 | **首期禁止** |

**L4 不是「被拒绝」，而是「需要人点头」。** 两者混成一个 DENIED，工作流就没法在
「等审批」这个状态停下来 —— 而人工审批恰恰是流程 B 里不可省略的一步。
对应地 `tool_calls.status` 里 `APPROVAL_REQUIRED` 与 `DENIED` 是两个独立取值，
审批流程要能从审计里筛出来，靠的就是这一条。

### 补丁工具与审批闭环（L2 / L4）

```
1. generate_patch (L2)  只生成 unified diff，不写盘 —— 让人先看到要改什么
2. apply_patch   (L4)   第一次调用不带 approval_id → 自动发起审批（409 返回 approval_id）
3. OWNER 批准
4. 再带 approval_id 重试 → 真正写盘
5. 再试一次             → 403 APPROVAL_ALREADY_USED（一条审批只换一次成功执行）
```

`apply_patch` 的审批校验拆成四个独立错误码，调用方要能分辨「该怎么办」：

| 错误码 | 含义 | 调用方该做什么 |
|---|---|---|
| `APPROVAL_NOT_APPROVED` | 还没批 / 已过期 | 等待或重新发起 |
| `APPROVAL_TOOL_MISMATCH` | 批的是别的工具 | 重新发起 |
| `APPROVAL_REQUIREMENT_MISMATCH` | 批的是别的需求 | 重新发起 |
| `APPROVAL_ALREADY_USED` | 已经用过一次 | 确认结果，不要再试 |

两个相关决定：

- **`changes` 用「目标内容」而不是 diff 文本**：应用时直接写目标内容，不用解析 diff
  （解析 diff 的边界情况多）；Agent 生成「这个文件最终长什么样」比生成
  「怎么从 A 改到 B」更不容易出错。
- **审批不是万能通行证**：路径校验在批准之后依然生效 —— 带着合法审批去写
  工作区外的路径，照样被拒。

### 人工审批（L4 的「人点头」）

L4 工具被 Gateway 拦下时，**审批记录是自动创建的**（`PENDING`），
`approval_id` 会随 `409 APPROVAL_REQUIRED` 的响应返回给调用方。
没有「手动发起审批」的接口 —— 系统知道有个工具被拦了，人不需要替系统记这件事。

| Method | Path | 权限 | 说明 |
|---|---|---|---|
| GET | `/requirements/{id}/approvals` | 项目成员 | 列表，可按 `status` 过滤 |
| GET | `/approvals/{id}` | 项目成员 | 详情 |
| POST | `/approvals/{id}/approve` | **仅 OWNER** | 批准 |
| POST | `/approvals/{id}/reject` | **仅 OWNER** | 驳回 |

规则：

- **只有项目 OWNER 能批。** Developer 可以触发 Agent（也就可能触发 L4 工具），
  但不能批自己的请求 —— 「我自己申请、我自己批准」等于没有审批。
  这是刻意的职责分离。
- **三个终态都不再有出边。** 批了又反悔要重新发起一次，历史必须原样保留。
- **过期是惰性判定，不是后台任务。** 读取或审批时发现 `PENDING` 已到期，
  先落成 `EXPIRED` 再拒绝。默认有效期 24 小时。
- **L4 工具必须挂在需求上下文里调用。** 脱离需求的「批准」没有意义 ——
  批的是「改这份需求的工作区」，不是「随便改点什么」。
  越界调用返回 `403 APPROVAL_CONTEXT_REQUIRED`。

### 路径校验：为什么不能用字符串前缀

文档 §14.1 原文：*「路径校验必须解析为规范化路径后判断是否在授权工作区根内，
不能用字符串前缀判断。」* 字符串前缀在这些情况会误判：

- `/workspace` 前缀匹配 `/workspace-evil/x` —— 两个完全不同的目录
- `/workspace/../etc/passwd` —— 前缀匹配，真实位置在 /etc
- 符号链接 `/workspace/link` 指向 `/etc` —— 字符串看着在里面

实现是 `(workspace_root / raw).resolve()` 之后 `is_relative_to(root)`，
相对路径一律相对**授权根**解析（不是相对进程当前目录）。测试里有
`workspace-evil` 这个真实反例。

### 审计记录

`tool_calls` 表记录**每一次**调用的四种结局：SUCCEEDED / FAILED / DENIED /
APPROVAL_REQUIRED，含入参、结果摘要、错误码、耗时。

- **完整输出不进库**，只存 500 字符摘要 —— 完整输出可能上千行，塞库既贵又没用
- **审计在 Gateway 自己的事务里提交**，不跟外层业务事务走：
  外层回滚时审计要留下来，出问题时你恰恰需要知道 Agent 做过什么

---

## 数据库

首期核心表（已建）：`users`、`projects`、`project_members`、`requirements`、`workflow_runs`、`agent_runs`、`artifacts`

按实现进度再增加：`approvals`（Stage 4），
以及 `tool_calls`、`code_changes`、`test_runs`、`review_findings`、`audit_logs`。

几条容易踩的约定：

- **时间戳统一存 naive UTC**，API 出口补 `Z`。SQLite 不保存时区，若写入 aware datetime，
  读回来变 naive，之后任何 aware/naive 比较都会抛 `TypeError`。
- **邮箱落库前统一小写**，否则 `UNIQUE(email)` 挡不住大小写不同的重复注册。
- **唯一约束是正确性保证，Service 里的查重只是友好提示** —— 并发下靠的是 DB 约束。
- `prd_json` 在 Stage 3 之前必须保持为 `NULL`，绝不能写入未经 Schema 校验的模型输出。
- **JWT 密钥必须 ≥ 32 字节**（RFC 7518 §3.2 对 HS256 的要求）。低于这个长度 PyJWT 会抛
  `InsecureKeyLengthWarning`，而一堆黄色警告的后果不是「更安全」，是所有人都学会无视警告。
  `APP_ENV=prod` 时密钥若仍是默认值或长度不足，应用会直接启动失败。

### 改表结构怎么办

首期没有 Alembic（按设计文档 §11 留到 Stage 5），建表靠 `Base.metadata.create_all()`。
所以**改了 ORM 模型后，已存在的本地库不会自动跟着变**，需要二选一：

- 删掉 `data/` 重建（开发阶段数据无所谓时）
- 手工 `ALTER TABLE`（数据要留着时）。例如 Stage 2 给 users 加 `token_version`：
  ```sql
  ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0;
  ```
- 或者把 `DATABASE_URL` 指向一个新文件，旧库留着当参考

这一步在接上 Alembic 之前是必须手动做的，忘了会看到 `no such column` 这类报错。

---

## 分阶段计划

| 阶段 | 目标 | 状态 |
|---|---|---|
| Stage 1 | 基础 API + 用户/项目/需求 CRUD + 统一错误 + pytest | ✅ 已完成 |
| Stage 2 | JWT 认证、密码哈希接入、Owner/Developer 角色、资源级权限、幂等键 | ✅ 已完成 |
| Stage 3 | LLM Provider 抽象、Product / Architect Agent、结构化输出校验、Agent Run 记录 | 🔄 进行中（Provider 抽象 + Agent Runtime + agent_runs/artifacts 已完成；Product/Architect Agent 与接口待做） |
| Stage 4 | 工作流状态机、Developer / Tester / Reviewer、Tool Gateway、审批、交付物汇总 | 待开始 |
| Stage 5 | Redis 限流、SSE、真实测试执行、Alembic、MySQL 兼容、简易 Web UI（可选增强） | 不阻塞交付 |

---

## 开工前需要拍板的几件事

1. **设计文档自身存在若干矛盾**，实现时已在代码里做了收敛，但文档还没同步：
   - §3.3 的状态机图含 `REJECTED` / `REVISION_REQUIRED`，但状态表没列这两个状态
     → 已在 `app/domain/enums.py` 补齐。
   - `REVIEWING` 缺少回到 `IMPLEMENTING` 的路径，而 §12.7 示例里 Reviewer 输出的正是
     `needs_revision` → 已在 `app/domain/workflow.py` 补上 `REVIEWING → REVISION_REQUIRED`。
   - §10 的目录结构里 ORM Model 出现两处（`app/models/`、`app/infrastructure/db/models.py`）
     → 采用 `app/models/`。
   - §10 结构含 `alembic.ini` + `migrations/`，但 §11 把 Alembic 划到 Stage 5
     → 首期用 `Base.metadata.create_all()` 建表。
   - `projects` / `workflow_runs` 缺 `created_at` / `updated_at`，与 `users` / `requirements`
     不一致 → 所有核心表统一审计字段。
   - `approvals.tool_call_id` 指向 `tool_calls`，而后者被划进「首期扩展表」，首期存在悬空外键。
   - `/auth/logout` 在无状态 Access Token 下语义悬空，Stage 2 需要明确会话撤销策略
     → 已用 `users.token_version` 解决（见上文「会话撤销」）。
2. **`requirements.status` 的取值集合文档未定义**（只在 §12.1 出现过一次 `DRAFT`）。
   已拆成 `RequirementStatus`（需求自身生命周期）与 `WorkflowStatus`（§3.3 的完整状态机）两个枚举。
3. **`requirements` 的 ORM Model 位置、密码哈希算法（Argon2 vs bcrypt）** 等细节在文档里
   是二选一或缺失的，当前实现选择写在代码注释里。

---

## 已知取舍（不是遗漏，是选择）

- **任何登录用户都能看到全部用户的邮箱**（`GET /users`）。这是为了让「添加项目成员」
  这条流程可用 —— 你得先能查到那个人的 id。在本地协作平台里可接受；若要对外，
  应收紧成「只能看到与你有共同项目的人」，或对其他人的 `email` 做脱敏。
- **账号停用（`UserStatus.DISABLED`）没有 API 入口**。设计文档只定义了项目级角色
  （Owner / Developer），没有全局管理员，所以不凭空造一个。`ACCOUNT_DISABLED` 分支
  保留在认证层（手工改库或将来有管理功能时立刻生效），测试通过直连改库构造该状态。
- **越权返回 403 而非 404**。理由见上文「权限模型」。
