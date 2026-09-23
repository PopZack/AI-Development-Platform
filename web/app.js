/* ============================================================================
   AI Dev Team · Web UI 主逻辑
   ----------------------------------------------------------------------------
   刻意不用框架、不用构建步骤：
   - 静态文件由 FastAPI 直接托管，「部署给多人用」时不需要 node/打包
   - 状态少、交互简单，一个 render 函数比一堆组件生命周期更好读

   三条实现纪律：
   1. **所有来自后端的内容一律 escape 后再进 DOM**。PRD / 架构 / diff 都是模型
      生成的任意文本，直接 innerHTML 等于给自己开一个 XSS。
   2. **SSE 用 fetch 流式读**，不用 EventSource —— 后者带不了 Authorization 头，
      只能把令牌放进 URL（会进访问日志和浏览器历史）。
   3. **长请求要有进度感**：start/resume 会真实调模型（20~40 秒），
      静默的按钮会让人以为点坏了。
   ========================================================================== */

const API = "/api/v1";

/* ------------------------------------------------------------------ 状态 */

const state = {
  token: localStorage.getItem("aidt.token") || "",
  user: null,
  route: { name: "login" },
  projects: [],
  project: null,
  members: [],
  users: [],
  requirements: [],
  requirement: null,
  artifacts: [],
  approvals: [],
  summary: null,
  run: null,
  events: [],
  sse: null,
  sseStatus: "idle",
  artifactTab: null,
  busy: null,          // { label, startedAt } —— 长请求进行中
  timer: null,
  modal: null,
};

const STEPS = [
  ["ANALYZING", "需求分析"],
  ["PLANNING", "架构设计"],
  ["IMPLEMENTING", "编码实现"],
  ["TESTING", "测试核对"],
  ["REVIEWING", "代码审查"],
  ["WAITING_APPROVAL", "人工审批"],
  ["COMPLETED", "完成"],
];

const STATUS_META = {
  CREATED: ["待启动", "info"],
  RUNNING: ["执行中", "run"],
  ANALYZING: ["需求分析", "run"],
  PLANNING: ["架构设计", "run"],
  IMPLEMENTING: ["编码实现", "run"],
  TESTING: ["测试核对", "run"],
  REVIEWING: ["代码审查", "run"],
  WAITING_APPROVAL: ["等待审批", "warn"],
  APPROVED: ["已批准", "ok"],
  COMPLETED: ["已完成", "ok"],
  REJECTED: ["已驳回", "bad"],
  REVISION_REQUIRED: ["待返工", "warn"],
  BLOCKED: ["阻塞", "warn"],
  FAILED: ["失败", "bad"],
  CANCELLED: ["已取消", "bad"],
  // 需求自身状态
  DRAFT: ["草稿", "info"],
  DESIGNED: ["已设计", "ok"],
};

const REQUIREMENT_STATUS_META = {
  DRAFT: ["草稿", "info"],
  ANALYZING: ["分析完成", "run"],
  DESIGNED: ["已设计", "ok"],
  COMPLETED: ["已完成", "ok"],
  FAILED: ["失败", "bad"],
  CANCELLED: ["已取消", "bad"],
};

const ARTIFACT_LABEL = {
  PRD: "PRD",
  ARCHITECTURE: "技术设计",
  PATCH: "代码变更",
  TEST_REPORT: "测试报告",
  REVIEW: "审查意见",
};

/* ------------------------------------------------------------------ 工具 */

const $ = (sel) => document.querySelector(sel);

/** 一律先转义再进 DOM —— 交付物内容是模型自由文本。 */
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shortId(id) {
  return id ? String(id).slice(0, 8) : "—";
}

function fmtTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtClock(value) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function statusBadge(status, meta = STATUS_META) {
  const [label, kind] = meta[status] || [status, "info"];
  const dot = kind === "run" ? '<i class="dot"></i>' : "";
  return `<span class="badge badge--${kind}">${dot}${esc(label)}</span>`;
}

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast toast--${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 8000 : 4500);
}

/** 统一请求：解析我们的统一错误体，401 直接登出。 */
async function api(method, path, body, { idempotencyKey } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const response = await fetch(`${API}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return null;

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const err = payload?.error || { code: "HTTP_ERROR", message: `HTTP ${response.status}` };
    if (response.status === 401) {
      doLogout("登录已过期，请重新登录");
    }
    if (response.status === 429) {
      const wait = response.headers.get("Retry-After") || "?";
      throw Object.assign(new Error(`请求过于频繁（${wait}s 后可重试）`), { code: err.code, raw: err });
    }
    throw Object.assign(new Error(err.message || "请求失败"), { code: err.code, details: err.details });
  }
  return payload;
}

/** 包一层长请求：显示进度条并记录耗时（start/resume 要几十秒）。 */
async function longTask(label, fn) {
  state.busy = { label, startedAt: Date.now() };
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    const strip = $("#busy-strip");
    if (strip) {
      strip.querySelector(".elapsed").textContent = `${((Date.now() - state.busy.startedAt) / 1000).toFixed(0)}s`;
    }
  }, 500);
  render();
  try {
    return await fn();
  } finally {
    state.busy = null;
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    render();
  }
}

/* ------------------------------------------------------------------ 登录 */

function renderLogin() {
  const tabs = ["login", "register"];
  return `
  <div class="login">
    <div class="login__hero">
      <div class="brand">
        <div class="brand__mark">AI</div>
        <div>
          <div class="brand__name">AI Dev Team</div>
          <div class="brand__sub">local ai delivery platform</div>
        </div>
      </div>
      <h1>把一句需求<br /><span>变成可交付的代码</span></h1>
      <p>
        Product / Architect / Developer / Tester / Reviewer 五个 Agent 协作，
        显式工作流状态机驱动，所有代码写入前必须经过人工审批。
      </p>
      <div class="hero-badges">
        <span class="badge badge--info">结构化输出校验</span>
        <span class="badge badge--info">工具权限分级 L0~L5</span>
        <span class="badge badge--info">工作区路径隔离</span>
        <span class="badge badge--info">实时事件流</span>
      </div>
    </div>

    <div class="login__card">
      <div class="tabs">
        ${tabs
          .map(
            (t) => `<button data-act="login-tab" data-tab="${t}" class="${state.loginTab === t || (!state.loginTab && t === "login") ? "is-active" : ""}">
              ${t === "login" ? "登录" : "注册"}</button>`,
          )
          .join("")}
      </div>
      <form data-act="auth-submit" style="margin-top:16px">
        <div class="field">
          <label>邮箱</label>
          <input name="email" type="email" required placeholder="you@example.com" autocomplete="username" />
        </div>
        <div class="field">
          <label>密码</label>
          <input name="password" type="password" required minlength="8" placeholder="至少 8 位，含大小写与数字" autocomplete="current-password" />
        </div>
        <div class="field" data-register-only style="${(state.loginTab || "login") === "register" ? "" : "display:none"}">
          <label>显示名</label>
          <input name="display_name" placeholder="比如：智全" />
        </div>
        <button class="btn btn--primary btn--block" style="margin-top:20px" type="submit">
          ${(state.loginTab || "login") === "register" ? "注册并进入" : "登录"}
        </button>
      </form>
      <p class="faint" style="margin-top:14px;font-size:12px">
        注册接口对所有人开放；进入项目后按成员角色（OWNER / DEVELOPER / VIEWER）控制权限。
      </p>
    </div>
  </div>`;
}

async function submitAuth(form) {
  const data = Object.fromEntries(new FormData(form));
  const isRegister = (state.loginTab || "login") === "register";
  const path = isRegister ? "/auth/register" : "/auth/login";
  const body = isRegister
    ? { email: data.email, password: data.password, display_name: data.display_name || data.email.split("@")[0] }
    : { email: data.email, password: data.password };

  await api("POST", path, body);
  if (isRegister) {
    toast("注册成功，正在登录…", "ok");
  }
  const session = await api("POST", "/auth/login", { email: data.email, password: data.password });
  state.token = session.access_token;
  localStorage.setItem("aidt.token", state.token);
  await bootstrapSession();
}

async function bootstrapSession() {
  state.user = await api("GET", "/auth/me");
  await loadProjects();
  state.route = { name: "projects" };
  render();
}

function doLogout(message) {
  state.token = "";
  state.user = null;
  localStorage.removeItem("aidt.token");
  closeStream();
  state.route = { name: "login" };
  if (message) toast(message, "warn");
  render();
}

/* ------------------------------------------------------------------ 数据加载 */

async function loadProjects() {
  const data = await api("GET", "/projects?limit=100");
  state.projects = data.items || [];
}

async function openProject(projectId) {
  state.project = state.projects.find((p) => p.id === projectId) || (await api("GET", `/projects/${projectId}`));
  const [members, requirements] = await Promise.all([
    api("GET", `/projects/${projectId}/members`),
    api("GET", `/projects/${projectId}/requirements?limit=100`),
  ]);
  state.members = members.items || [];
  state.requirements = requirements.items || [];
  state.route = { name: "project", id: projectId };
  render();
}

async function openRequirement(requirementId) {
  const requirement = await api("GET", `/requirements/${requirementId}`);
  state.requirement = requirement;
  state.run = null;
  state.artifacts = [];
  state.approvals = [];
  state.summary = null;
  state.events = [];
  state.artifactTab = null;
  state.route = { name: "requirement", id: requirementId };
  render();
  await refreshRequirement();
  openStream(requirementId);
}

/** 拉取需求的全部派生数据。刷新按钮与每个动作之后都会调用。 */
async function refreshRequirement() {
  const id = state.route.id;
  const [artifacts, approvals, summary] = await Promise.all([
    api("GET", `/requirements/${id}/artifacts?limit=100`),
    api("GET", `/requirements/${id}/approvals`),
    api("GET", `/requirements/${id}/deliverables`),
  ]);
  state.artifacts = artifacts.items || [];
  state.approvals = approvals.items || [];
  state.summary = summary;
  // 需求状态可能被工作流推进过，顺手刷新（prd 等字段也在这里）
  if (!state.project || state.project.id === summary.requirement.id) {
    state.requirement = await api("GET", `/requirements/${id}`);
  } else {
    state.requirement = await api("GET", `/requirements/${id}`);
  }
  state.run = summary.latest_run || state.run;
  if (!state.artifactTab && state.artifacts.length) {
    const latest = state.artifacts[state.artifacts.length - 1];
    state.artifactTab = latest.type;
  }
  render();
}

/* ------------------------------------------------------------------ SSE */

function closeStream() {
  if (state.sse) {
    state.sse.abort();
    state.sse = null;
  }
  state.sseStatus = "idle";
}

/**
 * 用 fetch 流式读 SSE。EventSource 不能带 Authorization 头，
 * 只能把令牌塞进 URL（会进访问日志），所以不用它。
 */
function openStream(requirementId) {
  closeStream();
  const controller = new AbortController();
  state.sse = controller;
  state.sseStatus = "connecting";
  paintStreamStatus();

  (async () => {
    try {
      const response = await fetch(`${API}/requirements/${requirementId}/events`, {
        headers: { Authorization: `Bearer ${state.token}` },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        state.sseStatus = "error";
        paintStreamStatus();
        return;
      }
      state.sseStatus = "live";
      paintStreamStatus();

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          handleFrame(chunk);
        }
      }
      state.sseStatus = "closed";
      paintStreamStatus();
    } catch (err) {
      if (err.name !== "AbortError") {
        state.sseStatus = "error";
        paintStreamStatus();
      }
    }
  })();
}

function handleFrame(chunk) {
  let type = "message";
  let data = "";
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return; // 心跳帧（以 : 开头）没有 data

  let payload = {};
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }
  state.events.push({ type, at: payload.at || new Date().toISOString(), payload: payload.payload || {} });
  if (state.events.length > 120) state.events.splice(0, state.events.length - 120);

  // 工作流状态变化：更新头部状态并异步刷新派生数据（交付物可能已新增）
  if (type === "workflow.status" || type === "workflow.created") {
    if (payload.payload?.status) {
      state.run = { ...(state.run || {}), ...payload.payload };
      const head = $("#run-status");
      if (head) head.innerHTML = statusBadge(payload.payload.status);
    }
    if (type === "workflow.status") {
      setTimeout(() => refreshRequirement().catch(() => {}), 400);
    }
  }
  if (type.startsWith("approval.")) {
    setTimeout(() => refreshRequirement().catch(() => {}), 300);
  }
  appendEvent(payload, type);
}

function appendEvent(payload, type) {
  const list = $("#event-list");
  if (!list) return;
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "event";
  row.innerHTML = `
    <div class="event__time">${esc(fmtClock(payload.at))}</div>
    <div>
      <div class="event__type">${esc(type)}</div>
      <div class="event__body">${esc(JSON.stringify(payload.payload || {}))}</div>
    </div>`;
  list.appendChild(row);
  list.scrollTop = list.scrollHeight;
}

function paintStreamStatus() {
  const el = $("#sse-status");
  if (!el) return;
  const map = {
    idle: ["未连接", "info"],
    connecting: ["连接中…", "warn"],
    live: ["实时", "ok"],
    closed: ["已断开", "info"],
    error: ["连接失败", "bad"],
  };
  const [label, kind] = map[state.sseStatus] || map.idle;
  el.innerHTML = `<span class="badge badge--${kind}"><i class="dot"></i>${label}</span>`;
}

/* ------------------------------------------------------------------ 应用外壳 */

function renderShell(main) {
  const projects = state.projects
    .map(
      (p) => `
      <button class="nav__item ${state.route.name === "project" && state.route.id === p.id ? "is-active" : ""}"
              data-act="open-project" data-id="${p.id}">
        <span>▦</span><span class="truncate">${esc(p.name)}</span>
      </button>`,
    )
    .join("");

  return `
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand__mark">AI</div>
        <div>
          <div class="brand__name">AI Dev Team</div>
          <div class="brand__sub">ai delivery platform</div>
        </div>
      </div>
      <nav class="nav">
        <div class="nav__label">导航</div>
        <button class="nav__item ${state.route.name === "projects" ? "is-active" : ""}" data-act="goto-projects">
          <span>◎</span><span>全部项目</span>
        </button>
      </nav>
      <nav class="nav" style="flex:1;overflow:auto">
        <div class="nav__label">我的项目</div>
        ${projects || '<div class="faint" style="padding:0 12px;font-size:12px">还没有项目</div>'}
      </nav>
      <button class="btn btn--primary btn--block" data-act="new-project">＋ 新建项目</button>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="crumbs">${crumbs()}</div>
        <div class="topbar__right">
          <span id="sse-status"></span>
          <button class="btn btn--ghost btn--sm" data-act="refresh" title="刷新">⟳</button>
          <div class="who">
            <div class="avatar">${esc((state.user?.display_name || "U").slice(0, 1).toUpperCase())}</div>
            <div>
              <div style="font-size:12.5px;font-weight:600">${esc(state.user?.display_name || "")}</div>
              <small>${esc(state.user?.email || "")}</small>
            </div>
          </div>
          <button class="btn btn--ghost btn--sm" data-act="logout">退出</button>
        </div>
      </header>
      <div class="content">${busyStrip()}${main}</div>
    </div>
  </div>`;
}

function crumbs() {
  const parts = [`<span>AI Dev Team</span>`];
  if (state.route.name === "projects") parts.push(`<span class="sep">/</span><b>全部项目</b>`);
  if (state.route.name === "project") {
    parts.push(`<span class="sep">/</span><span data-act="goto-projects" style="cursor:pointer">项目</span>`);
    parts.push(`<span class="sep">/</span><b>${esc(state.project?.name || "")}</b>`);
  }
  if (state.route.name === "requirement") {
    parts.push(
      `<span class="sep">/</span><span data-act="open-project" data-id="${state.project?.id}" style="cursor:pointer">${esc(state.project?.name || "")}</span>`,
    );
    parts.push(`<span class="sep">/</span><b>${esc(state.requirement?.title || "")}</b>`);
  }
  return parts.join(" ");
}

function busyStrip() {
  if (!state.busy) return "";
  return `
  <div class="running-strip" id="busy-strip">
    <div class="spinner spinner--dark"></div>
    <div><b>${esc(state.busy.label)}</b> —— 正在调用模型，一次 20~40 秒，请勿关闭页面</div>
    <div class="mono elapsed" style="margin-left:auto">0s</div>
  </div>`;
}

/* ------------------------------------------------------------------ 项目视图 */

function renderProjects() {
  const cards = state.projects
    .map(
      (p) => `
      <div class="card card--hover card--accent" data-act="open-project" data-id="${p.id}" style="cursor:pointer">
        <div class="spread">
          <h3 style="margin:0;font-size:16px">${esc(p.name)}</h3>
          <span class="badge badge--ok">${esc(p.status)}</span>
        </div>
        <p class="muted" style="min-height:44px">${esc(p.description || "（无描述）")}</p>
        <div class="spread faint" style="font-size:12px">
          <span class="mono">${esc(p.slug)}</span>
          <span>${esc(fmtTime(p.created_at))}</span>
        </div>
      </div>`,
    )
    .join("");

  return `
  <div class="spread">
    <div>
      <h2 style="margin:0 0 4px">全部项目</h2>
      <div class="muted" style="font-size:13px">每个项目有独立的成员与需求；工作区按需求隔离。</div>
    </div>
    <button class="btn btn--primary" data-act="new-project">＋ 新建项目</button>
  </div>
  ${
    state.projects.length
      ? `<div class="grid grid--cards">${cards}</div>`
      : `<div class="card"><div class="empty"><div class="empty__icon">◎</div>
         还没有项目。建一个，然后加成员一起干。<br /><br />
         <button class="btn btn--primary" data-act="new-project">＋ 新建项目</button></div></div>`
  }`;
}

function renderProject() {
  const myRole = roleOf(state.user?.id);
  const reqCards = state.requirements
    .map(
      (r) => `
      <div class="card card--hover" data-act="open-requirement" data-id="${r.id}" style="cursor:pointer">
        <div class="spread">
          <h3 style="margin:0;font-size:15px">${esc(r.title)}</h3>
          ${statusBadge(r.status, REQUIREMENT_STATUS_META)}
        </div>
        <p class="muted" style="margin:8px 0 10px;max-height:44px;overflow:hidden">${esc(r.description)}</p>
        <div class="spread faint" style="font-size:12px">
          <span>${esc(r.priority)}</span>
          <span>v${esc(r.version)} · ${esc(fmtTime(r.created_at))}</span>
        </div>
      </div>`,
    )
    .join("");

  const members = state.members
    .map(
      (m) => `
      <div class="spread" style="padding:8px 0;border-bottom:1px solid var(--border)">
        <span class="mono">${esc(shortId(m.user_id))}…</span>
        <span class="badge badge--${m.role === "OWNER" ? "ok" : "info"}">${esc(m.role)}</span>
      </div>`,
    )
    .join("");

  return `
  <div class="grid grid--split">
    <div class="stack">
      <div class="card">
        <div class="spread">
          <div>
            <h2 style="margin:0 0 4px">${esc(state.project.name)}</h2>
            <div class="muted" style="font-size:13px">${esc(state.project.description || "（无描述）")}</div>
          </div>
          <div class="row">
            ${myRole ? `<span class="badge badge--info">我的角色 ${esc(myRole)}</span>` : ""}
            <button class="btn btn--primary" data-act="new-requirement">＋ 新建需求</button>
          </div>
        </div>
      </div>

      ${
        state.requirements.length
          ? `<div class="grid grid--cards">${reqCards}</div>`
          : `<div class="card"><div class="empty"><div class="empty__icon">✎</div>还没有需求
             <br /><br /><button class="btn btn--primary" data-act="new-requirement">＋ 新建需求</button></div></div>`
      }
    </div>

    <div class="stack">
      <div class="card">
        <div class="card__title">
          <h3>项目成员</h3>
          ${myRole === "OWNER" ? `<button class="btn btn--sm" data-act="add-member">＋ 添加</button>` : ""}
        </div>
        ${members || '<div class="faint">—</div>'}
        <p class="faint" style="font-size:12px;margin:12px 0 0">
          只有 OWNER 能批准「写盘」与「最终交付」；DEVELOPER 可以触发 Agent；VIEWER 只读。
        </p>
      </div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ 需求视图 */

function roleOf(userId) {
  const found = state.members.find((m) => m.user_id === userId);
  return found ? found.role : null;
}

function railHtml() {
  const status = state.run?.status;
  const order = STEPS.map(([key]) => key);
  const currentIndex = order.indexOf(status);
  const done = ["COMPLETED", "APPROVED"].includes(status) ? order.length : currentIndex;

  return `<div class="rail">${STEPS.map(([key, label], index) => {
    const cls = index < done ? "is-done" : index === currentIndex ? "is-active" : "";
    return `<div class="rail__step ${cls}">
      <div class="rail__label">${esc(label)}</div><div class="rail__bar"></div></div>`;
  }).join("")}</div>`;
}

function renderRequirement() {
  const r = state.requirement;
  const role = roleOf(state.user?.id);
  const canWrite = role === "OWNER" || role === "DEVELOPER";
  const isOwner = role === "OWNER";
  const run = state.run;
  const runStatus = run?.status;

  return `
  <div class="stack">
    <div class="card card--accent">
      <div class="spread">
        <div style="min-width:0">
          <div class="row">
            <h2 style="margin:0;font-size:19px">${esc(r.title)}</h2>
            ${statusBadge(r.status, REQUIREMENT_STATUS_META)}
            <span id="run-status">${run ? statusBadge(runStatus) : '<span class="badge">未启动</span>'}</span>
          </div>
          <div class="faint" style="font-size:12px;margin-top:6px">
            <span class="mono">${esc(shortId(r.id))}</span> · 优先级 ${esc(r.priority)} · v${esc(r.version)} · 角色 ${esc(role || "—")}
          </div>
        </div>
        <div class="row">
          <button class="btn btn--sm" data-act="refresh">刷新</button>
        </div>
      </div>
      ${railHtml()}
    </div>

    <div class="grid grid--split">
      <div class="stack">
        ${renderWorkflowCard(canWrite, isOwner, run)}
        ${renderArtifactsCard()}
        <div class="card">
          <div class="card__title"><h3>需求原文</h3></div>
          <p class="muted" style="white-space:pre-wrap;margin:0">${esc(r.description)}</p>
          ${
            (r.acceptance_criteria || []).length
              ? `<hr class="sep" /><div class="faint" style="font-size:12px;margin-bottom:6px">需求方写明的验收标准</div>
                 <ul class="clean">${r.acceptance_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
              : ""
          }
        </div>
      </div>

      <div class="stack">
        ${renderApprovalsCard(isOwner)}
        <div class="card">
          <div class="card__title">
            <h3>实时事件</h3>
            <span id="sse-status-inline"></span>
          </div>
          <div class="events" id="event-list">
            ${
              state.events.length
                ? state.events
                    .map(
                      (e) => `<div class="event">
                        <div class="event__time">${esc(fmtClock(e.at))}</div>
                        <div><div class="event__type">${esc(e.type)}</div>
                        <div class="event__body">${esc(JSON.stringify(e.payload))}</div></div></div>`,
                    )
                    .join("")
                : '<div class="empty"><div class="empty__icon">◌</div>等待事件…<br /><span style="font-size:12px">启动工作流后能看到每一步进展</span></div>'
            }
          </div>
        </div>
        ${renderWorkspaceCard()}
      </div>
    </div>
  </div>`;
}

function renderWorkflowCard(canWrite, isOwner, run) {
  const status = run?.status;

  let actions = "";
  if (!run) {
    actions = `<button class="btn btn--primary" ${canWrite ? "" : "disabled"} data-act="create-run">创建工作流</button>
      <span class="faint" style="font-size:12px">全流程：分析 → 设计 → 编码 → 测试 → 审查 → 人工审批</span>`;
  } else if (status === "CREATED") {
    actions = `<button class="btn btn--primary" ${canWrite ? "" : "disabled"} data-act="start-run">▶ 启动执行</button>
      <button class="btn btn--bad btn--sm" ${canWrite ? "" : "disabled"} data-act="cancel-run">取消</button>`;
  } else if (status === "IMPLEMENTING" && run.current_step === "TOOL_GATEWAY") {
    const pending = state.approvals.find((a) => a.status === "PENDING" && a.tool_name === "apply_patch");
    actions = `
      ${pending ? `<span class="badge badge--warn">等待补丁审批</span>` : ""}
      <button class="btn btn--primary" ${canWrite ? "" : "disabled"} data-act="resume-run">${pending ? "批准后继续执行" : "继续执行"}</button>
      <button class="btn btn--bad btn--sm" ${canWrite ? "" : "disabled"} data-act="cancel-run">取消</button>
      <span class="faint" style="font-size:12px">先在上方审批卡片里批准，再点这里继续</span>`;
  } else if (status === "WAITING_APPROVAL" && run.current_step === "APPROVAL") {
    actions = `
      <button class="btn btn--ok" ${isOwner ? "" : "disabled"} data-act="approve-run">✓ 最终批准交付</button>
      <button class="btn btn--bad" ${isOwner ? "" : "disabled"} data-act="reject-run">✕ 驳回</button>
      ${isOwner ? "" : '<span class="faint" style="font-size:12px">只有项目 OWNER 能做最终决定</span>'}`;
  } else if (status === "REJECTED" || status === "REVISION_REQUIRED") {
    actions = `<button class="btn btn--primary" ${canWrite ? "" : "disabled"} data-act="resume-run">↻ 让 Developer 返工</button>
      <span class="faint" style="font-size:12px">会带着审查意见重新出补丁，并再次进入审批</span>`;
  } else if (["COMPLETED", "FAILED", "CANCELLED"].includes(status)) {
    actions = `<span class="badge badge--${status === "COMPLETED" ? "ok" : "bad"}">流程已结束</span>
      <button class="btn btn--sm" ${canWrite ? "" : "disabled"} data-act="create-run">再开一次工作流</button>`;
  } else {
    // 运行中（正在调模型）：此时页面通常正在长请求中
    actions = `<span class="badge badge--run"><i class="dot"></i>执行中，请稍候…</span>
      ${canWrite ? '<button class="btn btn--bad btn--sm" data-act="cancel-run">取消</button>' : ""}`;
  }

  const err = run?.error_code
    ? `<div class="notice notice--bad" style="margin-top:12px">失败：<b>${esc(run.error_code)}</b> —— ${esc(run.error_message || "")}
       <br /><span class="faint">细节在 agent_runs 表；直接再启动一次即可重试</span></div>`
    : "";

  return `
  <div class="card">
    <div class="card__title">
      <h3>工作流</h3>
      <span class="card__hint">${run ? `当前步骤 ${esc(run.current_step)}` : "尚未创建"}</span>
    </div>
    <div class="row">${actions}</div>
    <dl class="kv" style="margin-top:16px">
      <dt>运行 ID</dt><dd class="mono">${esc(shortId(run?.id))}</dd>
      <dt>状态</dt><dd>${run ? statusBadge(status) : "—"}</dd>
      <dt>开始时间</dt><dd>${esc(fmtTime(run?.started_at))}</dd>
      <dt>结束时间</dt><dd>${esc(fmtTime(run?.finished_at))}</dd>
    </dl>
    ${err}
  </div>`;
}

function renderArtifactsCard() {
  const types = ["PRD", "ARCHITECTURE", "PATCH", "TEST_REPORT", "REVIEW"];
  const available = types.filter((t) => state.artifacts.some((a) => a.type === t));
  const tab = state.artifactTab && available.includes(state.artifactTab) ? state.artifactTab : available[0];

  if (!available.length) {
    return `<div class="card"><div class="card__title"><h3>交付物</h3></div>
      <div class="empty"><div class="empty__icon">▤</div>还没有交付物<br />
      <span style="font-size:12px">启动工作流后，每一步的产出都会归档到这里</span></div></div>`;
  }

  const versions = state.artifacts.filter((a) => a.type === tab);
  const latest = versions[versions.length - 1];

  return `
  <div class="card">
    <div class="card__title">
      <h3>交付物</h3>
      <span class="card__hint">共 ${state.artifacts.length} 条 · 按版本保留历史</span>
    </div>
    <div class="tabs-line">
      ${available
        .map(
          (t) => `<button data-act="artifact-tab" data-type="${t}" class="${t === tab ? "is-active" : ""}">
          ${esc(ARTIFACT_LABEL[t] || t)}
          <span class="faint">×${state.artifacts.filter((a) => a.type === t).length}</span></button>`,
        )
        .join("")}
    </div>
    <div class="spread faint" style="font-size:12px;margin-bottom:10px">
      <span>最新版本 v${esc(latest.version)} · ${esc(fmtTime(latest.created_at))}</span>
      <span class="mono">${esc(shortId(latest.agent_run_id))}</span>
    </div>
    ${renderArtifactBody(latest)}
  </div>`;
}

function renderArtifactBody(artifact) {
  const c = artifact.content || {};
  switch (artifact.type) {
    case "PRD":
      return `
        <h4 style="margin:0 0 6px">${esc(c.title || "")}</h4>
        <p class="muted" style="margin:0 0 12px">${esc(c.summary || "")}</p>
        ${listBlock("目标", c.goals)}
        ${listBlock("用户故事", c.user_stories)}
        ${listBlock("验收标准", c.acceptance_criteria, "ok")}
        ${listBlock("本期不做", c.out_of_scope)}
        ${listBlock("待确认", c.open_questions, "warn")}`;

    case "ARCHITECTURE":
      return `
        <p class="muted" style="margin:0 0 12px">${esc(c.overview || "")}</p>
        ${
          (c.components || []).length
            ? `<div class="faint" style="font-size:12px;margin-bottom:6px">模块划分</div>
               <div class="stack" style="gap:8px;margin-bottom:14px">
               ${c.components
                 .map(
                   (m) => `<div style="padding:10px 12px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.03)">
                     <div class="spread"><b class="mono">${esc(m.name)}</b>
                     <span class="faint" style="font-size:11.5px">${esc((m.depends_on || []).join(", ") || "无依赖")}</span></div>
                     <div class="muted" style="font-size:12.5px">${esc(m.responsibility)}</div></div>`,
                 )
                 .join("")}</div>`
            : ""
        }
        ${listBlock("数据模型", c.data_model)}
        ${listBlock("接口清单", c.api_endpoints)}
        ${listBlock("关键决策", c.key_decisions)}
        ${listBlock("风险", c.risks, "warn")}
        ${listBlock("测试策略", c.test_strategy)}`;

    case "PATCH":
      return `
        <p class="muted" style="margin:0 0 12px">${esc(c.summary || "")}</p>
        ${listBlock("注意事项", c.notes)}
        ${
          (c.files || []).length
            ? `<div class="faint" style="font-size:12px;margin:8px 0 6px">改动文件</div>
               ${c.files
                 .map(
                   (f) => `<div class="spread mono" style="font-size:12px;padding:4px 0">
                     <span>${esc(f.path)}</span>
                     <span class="faint">${f.is_new_file ? "新增" : f.changed ? "修改" : "无变化"}</span></div>`,
                 )
                 .join("")}`
            : ""
        }
        <div class="faint" style="font-size:12px;margin:14px 0 6px">diff 预览（写入前）</div>
        <pre class="code">${colorDiff(c.diff || "（无 diff）")}</pre>`;

    case "TEST_REPORT":
      return `
        <div class="spread" style="margin-bottom:10px">
          <span class="badge badge--${c.verdict === "pass" ? "ok" : "bad"}">${c.verdict === "pass" ? "全部通过" : "存在未通过"}</span>
          <span class="faint" style="font-size:12px">${(c.cases || []).filter((x) => x.passed).length}/${(c.cases || []).length} 用例通过</span>
        </div>
        <p class="muted" style="margin:0 0 12px">${esc(c.summary || "")}</p>
        ${renderExecution(c.execution)}
        <div class="stack" style="gap:6px">
          ${(c.cases || [])
            .map(
              (x) => `<div style="padding:9px 11px;border:1px solid var(--border);border-radius:10px;
                 background:${x.passed ? "rgba(52,211,153,.06)" : "rgba(251,113,133,.07)"}">
                <div class="spread"><b style="font-size:13px">${esc(x.name)}</b>
                <span class="badge badge--${x.passed ? "ok" : "bad"}">${x.passed ? "通过" : "未通过"}</span></div>
                <div class="muted" style="font-size:12.5px">期望：${esc(x.expectation)}</div>
                ${x.detail ? `<div class="faint" style="font-size:12px">观察：${esc(x.detail)}</div>` : ""}</div>`,
            )
            .join("")}
        </div>
        ${listBlock("无法验证的点", c.risks, "warn")}`;

    case "REVIEW":
      return `
        <div class="spread" style="margin-bottom:10px">
          <span class="badge badge--${c.verdict === "approved" ? "ok" : "bad"}">
            ${c.verdict === "approved" ? "审查通过" : "要求返工"}</span>
        </div>
        <p class="muted" style="margin:0 0 12px">${esc(c.summary || "")}</p>
        <div class="stack" style="gap:6px">
          ${(c.findings || [])
            .map((f) => {
              const kind = { blocker: "bad", major: "bad", minor: "warn", praise: "ok" }[f.severity] || "info";
              return `<div style="padding:9px 11px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.03)">
                <div class="spread"><span class="badge badge--${kind}">${esc(f.severity)}</span>
                <span class="mono faint" style="font-size:11.5px">${esc(f.file || "")}</span></div>
                <div style="font-size:13px">${esc(f.comment)}</div></div>`;
            })
            .join("")}
        </div>`;

    default:
      return `<pre class="code">${esc(JSON.stringify(c, null, 2))}</pre>`;
  }
}

/** 真实 pytest 执行结果。模型说通过 ≠ pytest 真的通过，两块都要显示。 */
function renderExecution(execution) {
  if (!execution) return "";
  let label = "真实执行：全部通过";
  let kind = "ok";
  if (execution.timed_out) {
    label = "真实执行：超时被终止";
    kind = "warn";
  } else if (execution.no_tests_collected) {
    label = "真实执行：没有收集到测试（exit code 5）";
    kind = "warn";
  } else if (!execution.passed) {
    label = `真实执行：存在失败（exit code ${execution.exit_code}）`;
    kind = "bad";
  }
  return `
    <div class="notice notice--${kind}" style="margin-bottom:12px">
      <div class="spread">
        <b>${esc(label)}</b>
        <span class="mono faint" style="font-size:11.5px">${esc(execution.command || "pytest")}</span>
      </div>
      ${execution.output_tail ? `<pre class="code" style="margin-top:9px;max-height:220px">${esc(execution.output_tail)}</pre>` : ""}
    </div>`;
}

function listBlock(title, items, kind) {
  if (!items || !items.length) return "";
  const cls = kind === "ok" ? "badge--ok" : kind === "warn" ? "badge--warn" : "badge--info";
  return `
    <div style="margin-top:14px">
      <div class="row" style="margin-bottom:6px">
        <span class="badge ${cls}">${esc(title)}</span><span class="faint" style="font-size:11.5px">${items.length}</span>
      </div>
      <ul class="clean">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
    </div>`;
}

/** diff 着色：只对行首的 +/-/@@ 上色，不做语法高亮（够用且不会出错）。 */
function colorDiff(diff) {
  return diff
    .split("\n")
    .map((line) => {
      if (line.startsWith("+++") || line.startsWith("---")) return `<span class="hunk">${esc(line)}</span>`;
      if (line.startsWith("@@")) return `<span class="hunk">${esc(line)}</span>`;
      if (line.startsWith("+")) return `<span class="add">${esc(line)}</span>`;
      if (line.startsWith("-")) return `<span class="del">${esc(line)}</span>`;
      return esc(line);
    })
    .join("\n");
}

function renderApprovalsCard(isOwner) {
  const items = state.approvals;
  return `
  <div class="card">
    <div class="card__title">
      <h3>审批</h3>
      <span class="card__hint">${items.filter((a) => a.status === "PENDING").length} 条待处理</span>
    </div>
    ${
      items.length
        ? `<div class="stack" style="gap:10px">${items
            .map(
              (a) => `
        <div style="padding:11px 12px;border:1px solid var(--border);border-radius:12px;
             background:${a.status === "PENDING" ? "rgba(251,191,36,.07)" : "rgba(255,255,255,.03)"}">
          <div class="spread">
            <b class="mono" style="font-size:12.5px">${esc(a.tool_name)}</b>
            <span class="badge badge--${a.status === "PENDING" ? "warn" : a.status === "APPROVED" ? "ok" : "bad"}">${esc(a.status)}</span>
          </div>
          <div class="faint" style="font-size:11.5px;margin-top:4px">
            由 ${esc(shortId(a.requested_by))} 发起 · 到期 ${esc(fmtTime(a.expires_at))}
          </div>
          ${a.review_note ? `<div class="muted" style="font-size:12.5px">意见：${esc(a.review_note)}</div>` : ""}
          ${
            a.status === "PENDING"
              ? `<div class="row" style="margin-top:9px">
                   <button class="btn btn--ok btn--sm" ${isOwner ? "" : "disabled"} data-act="approve-tool" data-id="${a.id}">批准</button>
                   <button class="btn btn--bad btn--sm" ${isOwner ? "" : "disabled"} data-act="reject-tool" data-id="${a.id}">驳回</button>
                   ${isOwner ? "" : '<span class="faint" style="font-size:11.5px">仅 OWNER 可批</span>'}
                 </div>`
              : ""
          }
        </div>`,
            )
            .join("")}</div>`
        : '<div class="empty"><div class="empty__icon">✓</div>暂无审批<br /><span style="font-size:12px">需要写盘时系统会自动发起</span></div>'
    }
  </div>`;
}

function renderWorkspaceCard() {
  const files = state.summary?.workspace_files || [];
  return `
  <div class="card">
    <div class="card__title">
      <h3>工作区文件</h3>
      <span class="card__hint">审批通过后才会写入</span>
    </div>
    ${
      files.length
        ? `<div class="stack" style="gap:4px">${files
            .map((f) => `<div class="mono faint" style="font-size:12px">▤ ${esc(f)}</div>`)
            .join("")}</div>`
        : '<div class="faint" style="font-size:12px">暂无文件 —— 补丁尚未应用</div>'
    }
  </div>`;
}

/* ------------------------------------------------------------------ 渲染入口 */

function render() {
  const app = $("#app");
  if (!state.token || !state.user) {
    app.innerHTML = renderLogin();
    if (state.modal) app.insertAdjacentHTML("beforeend", renderModal());
    return;
  }

  let main = "";
  if (state.route.name === "projects") main = renderProjects();
  else if (state.route.name === "project") main = renderProject();
  else if (state.route.name === "requirement") main = renderRequirement();
  else main = renderProjects();

  app.innerHTML = renderShell(main);
  if (state.modal) app.insertAdjacentHTML("beforeend", renderModal());
  paintStreamStatus();
  const inline = $("#sse-status-inline");
  if (inline && $("#sse-status")) inline.innerHTML = $("#sse-status").innerHTML;
}

function renderModal() {
  if (state.modal === "new-project") {
    return modal("新建项目", `
      <form data-act="create-project" class="modal__form">
        <div class="modal__body">
          <div class="field"><label>项目名称</label><input name="name" required placeholder="比如：Todo API" /></div>
          <div class="field"><label>描述（可选）</label><textarea name="description" placeholder="一句话说明这个项目要做什么"></textarea></div>
        </div>
        <div class="modal__footer">
          <button type="button" class="btn btn--ghost" data-act="close-modal">取消</button>
          <button class="btn btn--primary" type="submit">创建</button>
        </div>
      </form>`);
  }
  if (state.modal === "new-requirement") {
    return modal("新建需求", `
      <form data-act="create-requirement" class="modal__form">
        <div class="modal__body">
          <div class="field"><label>需求标题</label><input name="title" required placeholder="比如：实现待办事项接口" /></div>
          <div class="field"><label>需求描述</label>
            <textarea name="description" required placeholder="用自然语言写清要做什么、边界在哪。这段文字会直接进 Product Agent 的提示词。"></textarea></div>
          <div class="field"><label>优先级</label>
            <select name="priority"><option>P0</option><option selected>P1</option><option>P2</option><option>P3</option></select></div>
          <div class="field"><label>验收标准（每行一条，可选）</label>
            <textarea name="acceptance_criteria" placeholder="POST /api/todos 成功返回 201&#10;同一用户下标题重复时返回 409"></textarea></div>
        </div>
        <div class="modal__footer">
          <button type="button" class="btn btn--ghost" data-act="close-modal">取消</button>
          <button class="btn btn--primary" type="submit">创建</button>
        </div>
      </form>`);
  }
  if (state.modal === "add-member") {
    const candidates = state.users
      .filter((u) => !state.members.some((m) => m.user_id === u.id))
      .slice(0, 50)
      .map(
        (u) => `<div class="spread" style="padding:8px 0;border-bottom:1px solid var(--border)">
          <div><div style="font-size:13px">${esc(u.display_name)}</div>
          <div class="faint" style="font-size:11.5px">${esc(u.email)}</div></div>
          <div class="row">
            <button class="btn btn--sm" data-act="add-member-do" data-id="${u.id}" data-role="DEVELOPER">加为 DEVELOPER</button>
            <button class="btn btn--sm" data-act="add-member-do" data-id="${u.id}" data-role="VIEWER">加为 VIEWER</button>
          </div></div>`,
      )
      .join("");
    return modal(
      "添加项目成员",
      `<div class="modal__body">${candidates || '<div class="faint">没有可添加的用户（已全部在项目里）</div>'}</div>
       <div class="modal__footer"><button type="button" class="btn btn--ghost" data-act="close-modal">关闭</button></div>`,
    );
  }
  if (state.modal === "prompt-note") {
    return modal(state.modalTitle || "填写意见", `
      <form data-act="note-submit" class="modal__form">
        <div class="modal__body">
          <div class="field"><label>${esc(state.modalLabel || "审批意见")}</label>
            <textarea name="note" placeholder="写清理由 —— 这条会落库并进审计"></textarea></div>
        </div>
        <div class="modal__footer">
          <button type="button" class="btn btn--ghost" data-act="close-modal">取消</button>
          <button class="btn btn--primary" type="submit">提交</button>
        </div>
      </form>`);
  }
  return "";
}

function modal(title, body) {
  return `<div class="modal" data-act="modal-backdrop"><div class="modal__box">
    <h3>${esc(title)}</h3>${body}</div></div>`;
}

/* ------------------------------------------------------------------ 交互 */

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-act]");
  if (!target) return;
  const act = target.dataset.act;

  // 点击弹层背景关闭（但点击内容区不关）
  if (act === "modal-backdrop" && event.target === target) {
    state.modal = null;
    render();
    return;
  }

  try {
    switch (act) {
      case "login-tab": {
        state.loginTab = target.dataset.tab;
        render();
        break;
      }
      case "goto-projects":
        closeStream();
        await loadProjects();
        state.route = { name: "projects" };
        render();
        break;
      case "open-project":
        closeStream();
        await openProject(target.dataset.id);
        break;
      case "open-requirement":
        await openRequirement(target.dataset.id);
        break;
      case "refresh":
        if (state.route.name === "requirement") await refreshRequirement();
        else if (state.route.name === "project") await openProject(state.route.id);
        else await loadProjects().then(render);
        toast("已刷新", "ok");
        break;
      case "logout":
        doLogout();
        break;
      case "close-modal":
        state.modal = null;
        render();
        break;
      case "new-project":
        state.modal = "new-project";
        render();
        break;
      case "new-requirement":
        state.modal = "new-requirement";
        render();
        break;
      case "add-member":
        state.modal = "add-member";
        render();
        state.users = (await api("GET", "/users?limit=200")).items || [];
        render();
        break;
      case "add-member-do": {
        await api("POST", `/projects/${state.project.id}/members`, {
          user_id: target.dataset.id,
          role: target.dataset.role,
        });
        toast("成员已添加", "ok");
        state.modal = null;
        await openProject(state.project.id);
        break;
      }
      case "artifact-tab":
        state.artifactTab = target.dataset.type;
        render();
        break;

      /* ---------------- 工作流动作 ---------------- */
      case "create-run": {
        const key = `${state.requirement.id}-${Date.now()}`;
        await api("POST", `/requirements/${state.requirement.id}/runs`, undefined, { idempotencyKey: key });
        toast("工作流已创建，可以启动执行了", "ok");
        await refreshRequirement();
        break;
      }
      case "start-run": {
        const runId = state.run.id;
        const result = await longTask("工作流执行中", () =>
          api("POST", `/runs/${runId}/start`),
        );
        state.run = result.run;
        announceRun(result);
        await refreshRequirement();
        break;
      }
      case "resume-run": {
        const runId = state.run.id;
        // 补丁审批暂停时需要带上 approval_id —— 由后端校验，前端只负责提供
        const pending = state.approvals.find((a) => a.status === "PENDING" && a.tool_name === "apply_patch");
        const approved = state.approvals
          .filter((a) => a.tool_name === "apply_patch" && a.status === "APPROVED")
          .pop();
        const approvalId = (pending || approved)?.id;
        const result = await longTask("继续执行中", () =>
          api("POST", `/runs/${runId}/resume`, approvalId ? { approval_id: approvalId } : {}),
        );
        state.run = result.run;
        announceRun(result);
        await refreshRequirement();
        break;
      }
      case "approve-run":
        state.run = await api("POST", `/runs/${state.run.id}/approve`);
        toast("已批准，工作流完成", "ok");
        await refreshRequirement();
        break;
      case "reject-run":
        state.run = await api("POST", `/runs/${state.run.id}/reject`);
        toast("已驳回。可以让 Developer 返工", "warn");
        await refreshRequirement();
        break;
      case "cancel-run":
        state.run = await api("POST", `/runs/${state.run.id}/cancel`);
        toast("已取消", "warn");
        await refreshRequirement();
        break;

      /* ---------------- 工具审批 ---------------- */
      case "approve-tool": {
        const note = prompt("审批意见（可留空）") ?? "";
        await api("POST", `/approvals/${target.dataset.id}/approve`, { note: note || null });
        toast("已批准。回到工作流卡片点「批准后继续执行」", "ok");
        await refreshRequirement();
        break;
      }
      case "reject-tool": {
        const note = prompt("驳回理由（建议写清楚）") ?? "";
        await api("POST", `/approvals/${target.dataset.id}/reject`, { note: note || null });
        toast("已驳回", "warn");
        await refreshRequirement();
        break;
      }
      default:
        break;
    }
  } catch (err) {
    toast(err.message || "操作失败", "bad");
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-act]");
  if (!form) return;
  event.preventDefault();
  const act = form.dataset.act;

  try {
    if (act === "auth-submit") {
      await submitAuth(form);
      return;
    }
    if (act === "create-project") {
      const data = Object.fromEntries(new FormData(form));
      const project = await api("POST", "/projects", {
        name: data.name,
        description: data.description || null,
      });
      state.modal = null;
      toast("项目已创建", "ok");
      await loadProjects();
      await openProject(project.id);
      return;
    }
    if (act === "create-requirement") {
      const data = Object.fromEntries(new FormData(form));
      const criteria = (data.acceptance_criteria || "")
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const created = await api("POST", `/projects/${state.project.id}/requirements`, {
        title: data.title,
        description: data.description,
        priority: data.priority,
        acceptance_criteria: criteria,
      });
      state.modal = null;
      toast("需求已创建", "ok");
      await openProject(state.project.id);
      await openRequirement(created.id);
      return;
    }
    if (act === "note-submit") {
      state.modal = null;
      render();
    }
  } catch (err) {
    toast(err.message || "提交失败", "bad");
  }
});

/** 一次 start/resume 之后告诉用户停在哪 —— 两个停点是流程的正常组成部分。 */
function announceRun(result) {
  if (!result.paused) {
    toast("流程已走到终态", "ok");
    return;
  }
  if (result.pause_reason === "tool_approval") {
    toast("代码变更需要 OWNER 批准后才能写盘", "warn");
  } else if (result.pause_reason === "final_approval") {
    toast("审查已通过，等待最终人工批准", "warn");
  }
}

/* ------------------------------------------------------------------ 启动 */

(async function boot() {
  if (!state.token) {
    render();
    return;
  }
  try {
    await bootstrapSession();
  } catch {
    doLogout("登录已过期，请重新登录");
  }
})();
