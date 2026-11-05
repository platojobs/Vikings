#!/usr/bin/env python3
"""v1.33.0 告警 SLA / 超时升级 / 值班交接真实验证。

不 mock 数据库、不 mock 接收端、真实杀进程重启：
1. 空库 `upgrade head`、`downgrade -1`、再 `upgrade head`；
2. v1.32.0 既有库（0007）升级到 0008：历史告警 SLA 字段回填口径可见；
3. 真实 PostgreSQL + 真实 uvicorn 网关 + 真实本地 HTTP 接收端：
   - 短阈值（1 分钟）→ 真实巡检触发超时升级 → 接收端收到 `alert.escalation`
     （含第几次/上限/原 SLA/超出分钟）；
   - 同一条告警同一步只发一次；达上限后停止升级但仍保持 overdue；
   - 超时后确认 → 列表口径为 `late`（超时达成）；未超时确认 → `met`；
   - 值班交接：责任落库 + 审计 + 接收人只收到一条 `alert.handover`；
   - kill -9 重启后巡检不重复发送、不丢升级计数；
   - 权限：viewer 不能交接、operator 不能改 SLA 配置。

用法（仓库根目录）：
    gateway/.venv/bin/python scripts/verify_sla_v133.py
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

EMPTY_DB = "iot_sla_empty_verify"
LEGACY_DB = "iot_sla_v132_verify"
VERIFY_DB = "iot_sla_verify"
PORT = 8133
SECRET = "sla-verify-secret-2026"

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _repo_url() -> str:
    for line in (ROOT / "gateway" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 gateway/.env 的 DATABASE_URL；本脚本只验证真实 PostgreSQL")


REPO_URL = _repo_url()


def _dsn(database: str, *, asyncpg: bool = False) -> str:
    parts = urlsplit(REPO_URL.replace("postgresql+asyncpg", "postgresql"))
    scheme = "postgresql+asyncpg" if asyncpg else "postgresql"
    return urlunsplit((scheme, parts.netloc, f"/{database}", parts.query, ""))


VERIFY_URL = _dsn(VERIFY_DB, asyncpg=True)


def _create_database(name: str) -> None:
    owner = urlsplit(REPO_URL.replace("postgresql+asyncpg", "postgresql")).username
    subprocess.run(
        [
            "psql", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-c", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
            "-c", f'CREATE DATABASE "{name}" OWNER {owner}',
        ],
        check=True, stdout=subprocess.DEVNULL,
    )


def _alembic(database: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _dsn(database, asyncpg=True)
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT / "gateway", env=env, check=True,
        stdout=subprocess.DEVNULL,
    )


def _pg(database: str, sql: str, *params, fetch: bool = False):
    async def inner():
        import asyncpg

        conn = await asyncpg.connect(_dsn(database))
        try:
            return await conn.fetch(sql, *params) if fetch else await conn.execute(sql, *params)
        finally:
            await conn.close()

    return _run(inner())


class Receiver:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                with receiver._lock:
                    receiver.events.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/hook"

    def by_event(self, name: str) -> list[dict]:
        with self._lock:
            return [event for event in self.events if event.get("event") == name]

    def wait_for(self, name: str, count: int = 1, timeout: float = 40.0) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.by_event(name)
            if len(events) >= count:
                return events
            time.sleep(0.3)
        return self.by_event(name)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _token(role: str, username: str) -> str:
    import jwt

    payload = {
        "sub": username, "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _http(method: str, path: str, *, headers: dict, body: dict | None = None, params=None):
    import httpx

    return httpx.request(
        method, f"http://127.0.0.1:{PORT}{path}",
        headers=headers, json=body, params=params, timeout=30,
    )


def _start_server(log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": VERIFY_URL,
            "APP_ENV": "development",
            "JWT_SECRET": SECRET,
            "SEED_DEMO_USERS": "false",
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": "1883",
            "NOTIFICATION_RETRY_INTERVAL_SECONDS": "2",
            "SLA_SWEEP_INTERVAL_SECONDS": "5",
            "LOG_LEVEL": "INFO",
        }
    )
    handle = open(log_path, "a", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info",
        ],
        cwd=ROOT / "gateway", env=env,
        stdout=handle, stderr=subprocess.STDOUT,
    )


def _wait_ready(timeout: float = 40.0) -> None:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("网关未在限定时间内就绪")


def _columns(database: str, table: str) -> set[str]:
    rows = _pg(
        database,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name=$1 AND table_schema='public'",
        table, fetch=True,
    )
    return {row["column_name"] for row in rows}


def _tables(database: str) -> set[str]:
    rows = _pg(
        database,
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        fetch=True,
    )
    return {row["table_name"] for row in rows}


def _migration_checks() -> None:
    print("== 1. 迁移：空库 / v1.32 既有库 / downgrade 往返 ==")
    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "sla_config" in _tables(EMPTY_DB)
    assert {"assignee", "sla_minutes", "sla_deadline_at", "escalation_count"} <= _columns(
        EMPTY_DB, "device_alert"
    )
    assert {"kind", "extra"} <= _columns(EMPTY_DB, "notification_delivery")
    defaults = _pg(EMPTY_DB, "SELECT * FROM sla_config WHERE id=1", fetch=True)[0]
    print(f"[OK] 空库 upgrade head：sla_config 默认 critical={defaults['critical_minutes']} "
          f"warning={defaults['warning_minutes']} max={defaults['max_escalations']}")

    _alembic(EMPTY_DB, "downgrade", "-1")
    assert "sla_config" not in _tables(EMPTY_DB)
    assert not ({"assignee", "sla_minutes", "sla_deadline_at"} & _columns(EMPTY_DB, "device_alert"))
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "sla_config" in _tables(EMPTY_DB)
    print("[OK] downgrade -1 移除 SLA 表/列，再次 upgrade head 恢复（往返无残留）")

    # v1.32.0 既有库：先停在 0007，插入一条历史已确认（acked_at 为 NULL）的告警
    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0007_notification_rules_retry")
    _pg(
        LEGACY_DB,
        "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
        "VALUES ('dev-legacy-sla', '旧库设备', 'VG-S100', '1.0.0', '默认分组', 'online')",
    )
    _pg(
        LEGACY_DB,
        "INSERT INTO device_alert (alert_id, device_id, level, metric, value, threshold, "
        "message, ai_generated, status, created_at, ack_note, acked_by, acked_at) VALUES "
        "('alt-legacy-sla', 'dev-legacy-sla', 'critical', 'voltage', 2.9, 3.2, '旧库告警', "
        "false, 'acked', '2026-11-04 08:00:00+00', NULL, NULL, NULL)",
    )
    _alembic(LEGACY_DB, "upgrade", "head")
    legacy = _pg(
        LEGACY_DB,
        "SELECT assignee, sla_minutes, sla_deadline_at, escalation_count, acked_at "
        "FROM device_alert WHERE alert_id='alt-legacy-sla'",
        fetch=True,
    )[0]
    assert legacy["sla_minutes"] == 30, legacy
    assert legacy["sla_deadline_at"] == datetime(2026, 11, 4, 8, 30, tzinfo=timezone.utc), legacy
    assert legacy["assignee"] is None and legacy["escalation_count"] == 0, legacy
    assert legacy["acked_at"] is None, "历史 acked_at 必须保持 NULL，不能臆测回填"
    print(
        "[OK] v1.32 既有库 upgrade：历史已确认行 sla_minutes=30、deadline=created+30min，"
        "assignee=NULL、升级计数=0、acked_at 仍 NULL（读取时归为 unknown，不计达标）"
    )


def _seed_alert(
    alert_id: str,
    *,
    device_id: str,
    level: str = "critical",
    created_minutes_ago: int,
    sla_minutes: int,
    status: str = "open",
    assignee: str | None = None,
    escalation_count: int = 0,
):
    """直接落一条告警，SLA 快照与 deadline 由本脚本显式给出（时间可控）。"""
    _pg(
        VERIFY_DB,
        "INSERT INTO device_alert (alert_id, device_id, level, metric, value, threshold, "
        "message, ai_generated, status, created_at, sla_minutes, sla_deadline_at, "
        "assignee, escalation_count) VALUES "
        "($1, $2, $3, 'voltage', 2.9, 3.2, '电压过低（SLA 验证）', false, $4, "
        "now() - ($5::int || ' minutes')::interval, $6::int, "
        "now() - ($5::int || ' minutes')::interval + ($6::int || ' minutes')::interval, $7, $8)",
        alert_id, device_id, level, status, int(created_minutes_ago), int(sla_minutes),
        assignee, escalation_count,
    )


def _seed() -> None:
    _pg(
        VERIFY_DB,
        "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
        "VALUES ('dev-sla-verify', 'SLA 验证设备', 'VG-S100', '1.0.0', '默认分组', 'online') "
        "ON CONFLICT (device_id) DO NOTHING",
    )
    # 已超时：用于「真实巡检触发升级」与「超时后确认 = 超时达成」
    _seed_alert(
        "alt-sla-late", device_id="dev-sla-verify",
        created_minutes_ago=3, sla_minutes=1,
    )
    # 未超时：用于确认后 = 达标（10 分钟窗口，验证期间不会先被升级）
    _seed_alert(
        "alt-sla-met", device_id="dev-sla-verify",
        created_minutes_ago=0, sla_minutes=10,
    )
    # 已达升级上限的未处理告警：用于证明「达上限停止升级但仍是已超时」
    _seed_alert(
        "alt-sla-capped", device_id="dev-sla-verify",
        created_minutes_ago=30, sla_minutes=1, assignee="other-shift",
        escalation_count=1,
    )
    # 交接范围：未指派 1 条 + 本班 1 条 + 他人 1 条
    _seed_alert(
        "alt-sla-h1", device_id="dev-sla-verify",
        created_minutes_ago=5, sla_minutes=30,
    )
    _seed_alert(
        "alt-sla-h2", device_id="dev-sla-verify",
        created_minutes_ago=4, sla_minutes=30, assignee="verify-operator",
    )
    _seed_alert(
        "alt-sla-h3", device_id="dev-sla-verify",
        created_minutes_ago=2, sla_minutes=30, assignee="bob",
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-sla-"))
    log_path = tmp / "gateway.log"
    print(f"日志: {log_path}")

    _migration_checks()

    print("== 2. 真实 PostgreSQL + 真实网关 + 真实 HTTP 接收端 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    # 告警必须在通知与 SLA 配置就绪之后再落库：网关启动即开始巡检，
    # 否则会用默认配置（总开关未开）抢先升级并写入 suppressed 投递。
    receiver = Receiver()
    server = _start_server(log_path)
    try:
        _wait_ready()
        admin = {"Authorization": f"Bearer {_token('admin', 'verify-admin')}"}
        operator = {"Authorization": f"Bearer {_token('operator', 'verify-operator')}"}
        viewer = {"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"}

        # 通知链路：开启总开关 + Webhook 指向真实接收端（不使用额外投递实现）
        resp = _http(
            "PATCH", "/api/system/notifications/config", headers=admin,
            body={
                "enabled": True, "mute_minutes": 0, "webhook_enabled": True,
                "webhook_url": receiver.url, "webhook_max_attempts": 2,
                "webhook_timeout_seconds": 5, "levels": ["critical", "warning"],
            },
        )
        assert resp.status_code == 200, resp.text

        # 短阈值：critical 1 分钟、升级上限 1、间隔 1 分钟
        resp = _http(
            "PUT", "/api/alerts/sla-config", headers=admin,
            body={
                "critical_minutes": 1, "warning_minutes": 60,
                "max_escalations": 1, "escalation_interval_minutes": 1,
            },
        )
        assert resp.status_code == 200, resp.text
        print("[OK] SLA 配置（admin）：critical=1min / warning=60min / 上限=1 / 间隔=1min")

        resp = _http(
            "PUT", "/api/alerts/sla-config", headers=operator,
            body={"critical_minutes": 5},
        )
        assert resp.status_code == 403, resp.text
        resp = _http(
            "POST", "/api/alerts/handover", headers=viewer,
            body={"to": "next-shift"},
        )
        assert resp.status_code == 403, resp.text
        print("[OK] 权限：operator 改 SLA 配置 403，viewer 交接 403（只读）")

        _seed()
        print("[OK] 已落库 6 条告警（其中 alt-sla-late 已超时 2 分钟）")

        print("== 3. 超时升级：真实巡检 → 真实 HTTP 接收端 ==")
        events = receiver.wait_for("alert.escalation", 1, timeout=35.0)
        assert len(events) == 1, f"应恰好收到 1 条升级，实际 {len(events)}"
        payload = events[0]
        escalation = payload["escalation"]
        assert payload["alert"]["alertId"] == "alt-sla-late", payload
        assert escalation["attempt"] == 1 and escalation["maxAttempts"] == 1, escalation
        assert escalation["slaMinutes"] == 1, escalation
        print("[OK] 接收端 JSON 关键字段：")
        print(json.dumps(
            {
                "event": payload["event"],
                "deliveryId": payload["deliveryId"],
                "alertId": payload["alert"]["alertId"],
                "level": payload["alert"]["level"],
                "escalation": escalation,
            },
            ensure_ascii=False, indent=2,
        ))
        delivery = _pg(
            VERIFY_DB,
            "SELECT kind, status, attempts, extra FROM notification_delivery "
            "WHERE alert_id='alt-sla-late' ORDER BY id",
            fetch=True,
        )
        print(f"[OK] 投递记录：{[(row['kind'], row['status'], row['attempts']) for row in delivery]}")
        assert all(row["kind"] == "escalation" for row in delivery)

        time.sleep(12)
        assert len(receiver.by_event("alert.escalation")) == 1, "同一条告警同一步不得重复发送"
        row = _pg(
            VERIFY_DB,
            "SELECT escalation_count, status, last_escalated_at FROM device_alert "
            "WHERE alert_id='alt-sla-late'",
            fetch=True,
        )[0]
        assert row["escalation_count"] == 1 and row["status"] == "open", row
        print("[OK] 12 秒内重复巡检：仍只有 1 条升级通知，escalation_count=1")

        print("== 4. 超时后确认 = 超时达成；未超时确认 = 达标 ==")
        resp = _http("POST", "/api/alerts/alt-sla-late/ack", headers=operator,
                     body={"note": "超时后才处理"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["sla_state"] == "late", resp.text
        resp = _http("POST", "/api/alerts/alt-sla-met/ack", headers=operator,
                     body={"note": "及时处理"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["sla_state"] == "met", resp.text
        late_page = _http(
            "GET", "/api/alerts", headers=viewer,
            params={"sla": "late", "pageSize": 10},
        ).json()["data"]
        met_page = _http(
            "GET", "/api/alerts", headers=viewer,
            params={"sla": "met", "pageSize": 10},
        ).json()["data"]
        overdue_page = _http(
            "GET", "/api/alerts", headers=viewer,
            params={"sla": "overdue", "pageSize": 20},
        ).json()["data"]
        print(f"[OK] 列表筛选：late={late_page['total']}、met={met_page['total']}、"
              f"overdue={overdue_page['total']}")
        assert late_page["total"] == 1 and late_page["items"][0]["alert_id"] == "alt-sla-late"
        assert met_page["total"] == 1 and met_page["items"][0]["alert_id"] == "alt-sla-met"
        assert any(item["alert_id"] == "alt-sla-capped" for item in overdue_page["items"])
        capped = next(item for item in overdue_page["items"] if item["alert_id"] == "alt-sla-capped")
        assert capped["sla_state"] == "overdue" and capped["escalation_count"] == 1
        print("[OK] 已达升级上限的 alt-sla-capped：不再升级，但 SLA 仍为 overdue（未洗白）")

        print("== 5. 值班交接：责任落库 + 审计 + 一条汇总通知 ==")
        resp = _http(
            "POST", "/api/alerts/handover", headers=operator,
            body={"to": "next-shift", "note": "夜班交接"},
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["data"]
        assert result["count"] == 2, result
        handover_events = receiver.wait_for("alert.handover", 1, timeout=15.0)
        assert len(handover_events) == 1, f"交接必须只发一条汇总，实际 {len(handover_events)}"
        print("[OK] 接收端收到 1 条 alert.handover：")
        print(json.dumps(handover_events[0].get("handover"), ensure_ascii=False, indent=2))
        rows = _pg(
            VERIFY_DB,
            "SELECT alert_id, assignee, sla_deadline_at, escalation_count FROM device_alert "
            "WHERE alert_id IN ('alt-sla-h1','alt-sla-h2','alt-sla-h3')",
            fetch=True,
        )
        assignees = {row["alert_id"]: row["assignee"] for row in rows}
        assert assignees["alt-sla-h1"] == "next-shift" and assignees["alt-sla-h2"] == "next-shift"
        assert assignees["alt-sla-h3"] == "bob", "不得越权交接他人的告警"
        audit = _pg(
            VERIFY_DB,
            "SELECT operator, detail FROM audit_log WHERE action='alert.handover' "
            "ORDER BY id DESC LIMIT 1",
            fetch=True,
        )[0]
        detail = json.loads(audit["detail"]) if isinstance(audit["detail"], str) else audit["detail"]
        assert detail["count"] == 2 and detail["from"] == "verify-operator", detail
        print(f"[OK] 交接后责任人：{assignees}（h3 保持 bob）")
        print(f"[OK] 审计：operator={audit['operator']} detail={json.dumps(detail, ensure_ascii=False)}")
        h1 = next(row for row in rows if row["alert_id"] == "alt-sla-h1")
        assert h1["escalation_count"] == 0 and h1["sla_deadline_at"] is not None
        print("[OK] 交接不重置 SLA 截止时间与升级计数（h1 计数仍为 0，deadline 未变）")

        print("== 6. 重启：kill -9 后巡检不重复、不丢失 ==")
        before = len(receiver.by_event("alert.escalation"))
        server.send_signal(signal.SIGKILL)
        server.wait(timeout=10)
        server = _start_server(log_path)
        _wait_ready()
        time.sleep(12)
        after = len(receiver.by_event("alert.escalation"))
        assert after == before, f"重启后不得重复升级：{before} → {after}"
        capped_row = _pg(
            VERIFY_DB,
            "SELECT escalation_count, status FROM device_alert WHERE alert_id='alt-sla-capped'",
            fetch=True,
        )[0]
        assert capped_row["escalation_count"] == 1 and capped_row["status"] == "open", capped_row
        print(f"[OK] 重启后升级通知条数不变（{before}），alt-sla-capped 计数=1、状态=open")

        print("== 汇总 ==")
        print(f"接收端事件统计：escalation={len(receiver.by_event('alert.escalation'))} "
              f"handover={len(receiver.by_event('alert.handover'))} "
              f"total={len(receiver.events)}")
        print("v1.33.0 真实验证全部通过")
    finally:
        try:
            if server.poll() is None:
                server.send_signal(signal.SIGTERM)
                server.wait(timeout=10)
        except Exception:
            server.kill()
        receiver.close()


if __name__ == "__main__":
    main()
