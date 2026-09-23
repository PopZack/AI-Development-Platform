# AI Dev Team —— 多人部署镜像
#
# 三条设计取舍：
# 1. **多阶段构建**：uv 只在 builder 阶段出现，运行镜像里不带它 ——
#    镜像小一点、攻击面也小一点。
# 2. **非 root 运行**：容器里的进程没有 uid 0，工作区文件即使被写坏也影响有限。
# 3. **镜像里不含 tests/**：运行不需要它们，而 `uv sync --no-dev` 也不会装 pytest。
#    要跑测试就在开发机上跑（CI 里也是）。
#
# 多 worker 必须配 REDIS_URL（限流与 SSE 事件要跨进程共享），
# 表结构必须用 `alembic upgrade head` 而不是 create_all —— 见 README「部署」小节。

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只拷依赖清单：依赖没变时这一层能命中缓存，改代码不会重装依赖
COPY pyproject.toml uv.lock ./
# 需要 MySQL / Redis 时传进来（compose 里传的是 "mysql,redis"）。
# 逗号分隔而不是空格：ARG 用空格分隔时 shell 会把它拆成两个参数，
# 而 --extra 只吃后面那个，前面的会变成 uv 的子命令 —— 那是很难看懂的错误。
# 这里显式展开成重复的 --extra，避免依赖 uv 对 "a,b" 的解析行为。
ARG EXTRAS=""
RUN set -eu; \
    flags=""; \
    for extra in $(echo "$EXTRAS" | tr ',' ' '); do \
        [ -n "$extra" ] && flags="$flags --extra $extra"; \
    done; \
    uv sync --frozen --no-dev $flags

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# curl 只用于容器健康检查；装完不清理 apt 缓存会让镜像大几十 MB
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# 非 root：容器逃逸的成本更高，误写宿主文件的机会更小
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY web ./web

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 工作区挂在这里：它必须落在持久卷上，否则容器重建就丢了 Agent 写的代码
RUN mkdir -p /data/workspace && chown -R appuser:appuser /data /app
VOLUME ["/data/workspace"]

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 默认单 worker（不需要 Redis）。多 worker 用 compose 里的 command 覆盖
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
