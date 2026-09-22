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

### 业务资源（Stage 1，尚未强制认证）

> ⚠️ 这一组接口目前**还没有接认证与资源级权限** —— 给它们加上鉴权是 Stage 2 的下一个提交。
> 在那之前不要把服务部署到任何共享环境。

| Method | Path | 说明 |
|---|---|---|
| GET | `/users` | 用户列表 |
| GET | `/users/{user_id}` | 用户详情 |
| PATCH | `/users/{user_id}` | 修改用户 |
| POST | `/projects` | 创建项目（创建者自动成为 Owner） |
| GET | `/projects` | 项目列表（可按 `owner_id` 过滤） |
| GET | `/projects/{project_id}` | 项目详情 |
| PATCH | `/projects/{project_id}` | 修改项目 |
| GET | `/projects/{project_id}/members` | 成员列表 |
| POST | `/projects/{project_id}/members` | 添加成员 |
| POST | `/projects/{project_id}/requirements` | 创建需求 |
| GET | `/projects/{project_id}/requirements` | 项目下的需求列表 |
| GET | `/requirements/{requirement_id}` | 需求详情 |
| PATCH | `/requirements/{requirement_id}` | 修改需求（内容变更会使 version 自增） |

`POST /users` 已在 Stage 2 下线，注册统一走 `/auth/register` —— 同一个业务动作保留两条
入口，两边的校验规则迟早会走偏（比如一边统一小写邮箱、另一边忘了）。

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

## 数据库

首期核心表（已建）：`users`、`projects`、`project_members`、`requirements`

按实现进度再增加：`workflow_runs`、`agent_runs`、`artifacts`、`approvals`（Stage 3/4），
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
| Stage 2 | JWT 认证、密码哈希接入、Owner/Developer 角色、资源级权限、幂等键 | 🔄 进行中（认证与会话撤销已完成；资源级权限、幂等键待做） |
| Stage 3 | LLM Provider 抽象、Product / Architect Agent、结构化输出校验、Agent Run 记录 | 待开始 |
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

## 尚未收掉的短板

- **业务资源接口还没有鉴权**：`/users`、`/projects`、`/requirements` 三个模块
  目前不要求登录，`POST /projects` 和 `POST /requirements` 的 `owner_id` / `created_by`
  仍由客户端指定，`GET /projects` 不带参数会列出全部项目。
  给它们加上认证 + 资源级权限是 **Stage 2 的下一个提交**。在那之前不要部署到共享环境。
- `POST /users` 已下线，注册统一走 `/auth/register`（避免同一个动作有两条入口）。
