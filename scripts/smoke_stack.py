#!/usr/bin/env python
"""整栈冒烟检查：把「部署验收」从人肉记忆变成脚本。

用法：

    python scripts/smoke_stack.py                 # 起栈（compose up -d --build）→ 检查 → 拆栈
    python scripts/smoke_stack.py --skip-build    # 复用已在跑的栈
    python scripts/smoke_stack.py --keep          # 检查完不拆栈（本地排查用）

五条断言对应的是部署时**真正踩过的坑**，不是走过场：

1. 启动日志必须含 ``events=redis``，出现 ``events=in-process`` 直接失败
   —— compose 配了 REDIS_URL 但镜像没装 redis 包时会静默退化，
   多 worker 下额度翻倍、SSE 事件时有时无（redis extra 未装事故）
2. 全量日志里不许出现 MySQL 密码（启动日志曾明文打印，4 个 worker 各一遍）
3. 数据库表数 == 模型表数 + 1（alembic_version）—— 迁移与模型漂移直接暴露
4. /health 与 /ui/ 必须 200
5. 注册 → 登录 → 建项目 → 建需求 → 建工作流（含幂等重放）走通

任何一条失败：退出码非 0，且打印每条的 PASS/FAIL 明细。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# 直接以 ``python scripts/smoke_stack.py`` 运行时，sys.path[0] 是 scripts/，
# 仓库根不在上面 —— 断言 3 要 import app.models 数表，必须先把根放进去
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

API = "/api/v1"
HEALTH_TIMEOUT = 240  # 秒：等 db 健康 + alembic + uvicorn 起来


def _compose(*args: str, capture: bool = True) -> str:
    """跑 docker compose 子命令。检查类操作失败就抛异常（让外层计数）。"""
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose {' '.join(args)} 失败（退出码 {result.returncode}）："
            f"{(result.stderr or result.stdout or '')[-400:]}"
        )
    return result.stdout or ""


def _env_value(key: str) -> str | None:
    """从 .env 读单个值（只取需要的，不整体解析）。"""
    path = ROOT / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if name.strip() == key and sep:
            return value.strip().strip("'\"")
    return None


def _count_tables() -> int:
    """数数据库里的表（在 db 容器里用 MYSQL_PWD 而不是 -p，密码不进命令行）。"""
    payload = (
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -e '
        "\"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='aidev'\""
    )
    out = _compose("exec", "-T", "db", "sh", "-c", payload)
    return int(out.strip().splitlines()[-1])


def _wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=5).status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - 起来之前全是连接错误，正常
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(3)
    raise TimeoutError(f"{HEALTH_TIMEOUT}s 内 /health 未就绪（最后一次错误：{last_error}）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--skip-build", action="store_true", help="复用已在跑的栈（不 compose up --build）")
    parser.add_argument("--keep", action="store_true", help="检查完不拆栈（本地排查用）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    failures: list[str] = []
    total = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # ---------- 起栈 ----------
    if not args.skip_build:
        print("== 1. 构建并启动整栈（app 4 worker + MySQL 8.4 + Redis）==")
        _compose("up", "-d", "--build")
    else:
        print("== 1. 复用已在跑的栈（--skip-build）==")
    print(f"   等待 /health 就绪（最多 {HEALTH_TIMEOUT}s）…")
    try:
        _wait_for_health(args.base_url)
        print("   ✅ 栈已就绪")
    except TimeoutError as exc:
        print(f"   ❌ {exc}")
        print("== 冒烟中止（栈都没起来，后续断言无意义）==")
        return 2

    try:
        # ---------- 断言 1：Redis 真的生效 ----------
        print("== 2. events=redis（配了 REDIS_URL 就必须真的走 Redis）==")
        app_logs = _compose("logs", "app")
        has_redis = "events=redis" in app_logs
        has_inprocess = "events=in-process" in app_logs
        ok = has_redis and not has_inprocess
        check(
            "启动日志含 events=redis 且无 events=in-process",
            ok,
            "" if ok else "看到 in-process 说明限流与事件在静默退化 —— 查 redis extra 是否装进镜像",
        )

        # ---------- 断言 2：日志不漏密码 ----------
        print("== 3. 日志脱敏（不许出现 MySQL 密码明文）==")
        password = _env_value("MYSQL_PASSWORD") or ""
        if not password:
            check(".env 里有 MYSQL_PASSWORD 可供扫描", False, "读不到密码，无法完成脱敏断言")
        else:
            all_logs = _compose("logs")
            leaked = [line for line in all_logs.splitlines() if password in line]
            check(
                "全部服务日志中无 MySQL 密码明文",
                not leaked,
                f"泄漏 {len(leaked)} 行，示例：{leaked[0][:120]}" if leaked else "",
            )

        # ---------- 断言 3：表数与模型一致 ----------
        print("== 4. 迁移建出的表数 == 模型表数 + alembic_version ==")
        import app.models  # noqa: F401 —— 导入以填充 metadata
        from app.infrastructure.db.base import Base

        expected = len(Base.metadata.tables) + 1
        actual = _count_tables()
        check(
            "数据库表数一致",
            actual == expected,
            f"模型 {len(Base.metadata.tables)} 张 + alembic_version，库里 {actual} 张",
        )

        # ---------- 断言 4：页面与健康检查 ----------
        print("== 5. /health 与 /ui/ ==")
        health = httpx.get(f"{args.base_url}/health", timeout=10)
        ui = httpx.get(f"{args.base_url}/ui/", timeout=10)
        check("/health 200", health.status_code == 200, f"实得 {health.status_code}")
        check("/ui/ 200", ui.status_code == 200, f"实得 {ui.status_code}")

        # ---------- 断言 5：核心业务链路 ----------
        print("== 6. 注册 → 登录 → 项目 → 需求 → 工作流（含幂等重放）==")
        email = f"smoke-{uuid4().hex[:8]}@example.com"
        with httpx.Client(base_url=f"{args.base_url}{API}", timeout=30) as client:
            reg = client.post(
                "/auth/register",
                json={"email": email, "password": "SmokePass123!", "display_name": "冒烟验收"},
            )
            check("注册 201", reg.status_code == 201, f"实得 {reg.status_code} {reg.text[:80]}")

            login = client.post("/auth/login", json={"email": email, "password": "SmokePass123!"})
            token = login.json().get("access_token") if login.status_code == 200 else ""
            check("登录 200", login.status_code == 200, f"实得 {login.status_code}")
            if not token:
                raise RuntimeError("登录失败，后续链路无从验证")
            headers = {"Authorization": f"Bearer {token}"}

            project = client.post("/projects", json={"name": "冒烟验收项目"}, headers=headers)
            check("建项目 201", project.status_code == 201, f"实得 {project.status_code}")

            requirement = client.post(
                f"/projects/{project.json()['id']}/requirements",
                json={"title": "冒烟需求", "description": "冒烟检查用，不启动执行。", "priority": "P1"},
                headers=headers,
            )
            check("建需求 201", requirement.status_code == 201, f"实得 {requirement.status_code}")

            key = f"smoke-{uuid4().hex[:8]}"
            first = client.post(
                f"/requirements/{requirement.json()['id']}/runs", headers={**headers, "Idempotency-Key": key}
            )
            second = client.post(
                f"/requirements/{requirement.json()['id']}/runs", headers={**headers, "Idempotency-Key": key}
            )
            check(
                "建工作流 201 + 幂等重放 200（Idempotent-Replay: true）",
                first.status_code == 201
                and second.status_code == 200
                and second.headers.get("idempotent-replay") == "true",
                f"first={first.status_code} second={second.status_code} "
                f"header={second.headers.get('idempotent-replay')}",
            )
    except Exception as exc:  # noqa: BLE001 - 冒烟自己崩了也是失败
        check("冒烟脚本执行完整", False, f"{type(exc).__name__}: {exc}")
    finally:
        # ---------- 拆栈 ----------
        if not args.keep:
            print("== 7. 拆栈（docker compose down -v）==")
            try:
                _compose("down", "-v", "--remove-orphans")
            except RuntimeError as exc:
                print(f"   ⚠️ 拆栈失败（不影响检查结论）：{exc}")
        else:
            print("== 7. 保留栈运行（--keep）==")

    # ---------- 结论 ----------
    passed = total - len(failures)
    print(f"\n== 冒烟结论：{passed}/{total} 通过 ==")
    if failures:
        print("失败项：" + "、".join(failures))
        return 1
    print("✅ 整栈冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
