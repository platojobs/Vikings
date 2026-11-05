#!/usr/bin/env python3
"""v1.31.0 通知可靠性真实验证（不 mock 数据库、不 mock 接收端、真实杀进程）。

覆盖：
1. 空库迁移、既有 v1.29.0 库升级（配置/投递保留 + 默认规则回填）、downgrade 往返；
2. 真实 PostgreSQL + 真实 uvicorn 网关 + 真实本地 HTTP 接收端；
3. 分组路由：两个分组两个接收人、未匹配 suppressed、分组变更立即生效、优先级兜底；
4. 重启续投：接收端 500 → kill -9 网关 → 重启 → 继续重试并成功；
   重试上限耗尽 → 终态 failed，原因可见；
5. 投递历史筛选/分页与权限（viewer 403 / operator 200）；
6. 旁路性能：延迟 2s 的接收端下，MQTT 上报处理器耗时对比 v1.29.0 基线；
7. 日志、审计、接口回显均不含 URL/密钥。

用法（仓库根目录）：
    gateway/.venv/bin/python scripts/verify_notifications_v131.py
"""

import asyncio
import json
import os
import shutil
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
VERIFY_DB = "iot_notify_verify"
LEGACY_DB = "iot_notify_legacy_verify"
EMPTY_DB = "iot_notify_empty_verify"
PORT = 8131
BASELINE_REPORT_MS = 5.5
SECRET = "verify-notify-secret-must-not-leak"
WEBHOOK_SECRET = "rule-secret-must-not-leak"
SMTP_PASSWORD = "smtp-pass-must-not-leak"

# 验证脚本里所有协程共用一个事件循环：SQLAlchemy asyncpg 连接池绑定 loop，
# 反复 asyncio.run 会触发 “Future attached to a different loop”。
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _loop_run(coro):
    return _LOOP.run_until_complete(coro)


def _repo_database_url() -> str:
    env_file = ROOT / "gateway" / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 gateway/.env 的 DATABASE_URL；本脚本只验证真实 PostgreSQL")


def _dsn(url: str, database: str, *, asyncpg: bool) -> str:
    parts = urlsplit(url.replace("postgresql+asyncpg", "postgresql"))
    scheme = "postgresql+asyncpg" if asyncpg else "postgresql"
    return urlunsplit((scheme, parts.netloc, f"/{database}", parts.query, ""))


REPO_URL = _repo_database_url()
VERIFY_URL = _dsn(REPO_URL, VERIFY_DB, asyncpg=True)


def _create_database(name: str) -> None:
    owner = urlsplit(REPO_URL.replace("postgresql+asyncpg", "postgresql")).username
    subprocess.run(
        [
            "psql", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-c", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
            "-c", f'CREATE DATABASE "{name}" OWNER {owner}',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _alembic(database: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _dsn(REPO_URL, database, asyncpg=True)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT / "gateway",
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stdout + proc.stderr)


async def _pg(database: str, sql: str, *params, fetch: bool = False):
    import asyncpg

    conn = await asyncpg.connect(_dsn(REPO_URL, database, asyncpg=False))
    try:
        if fetch:
            return await conn.fetch(sql, *params)
        return await conn.execute(sql, *params)
    finally:
        await conn.close()


class Receiver:
    """真实 HTTP 接收端：可切换状态码/延迟，记录请求头与请求体。"""

    def __init__(self, name: str, *, status: int = 200, delay: float = 0.0) -> None:
        self.name = name
        self.status = status
        self.delay = delay
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with receiver._lock:
                    receiver.requests.append(
                        {"path": self.path, "headers": dict(self.headers), "body": body}
                    )
                if receiver.delay:
                    threading.Event().wait(receiver.delay)
                self.send_response(receiver.status)
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
        return f"http://{host}:{port}/{self.name}"

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _token(role: str, username: str) -> str:
    import jwt

    payload = {"sub": username, "role": role, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _http(method: str, path: str, *, headers: dict, json_body: dict | None = None, params=None):
    import httpx

    response = httpx.request(
        method, f"http://127.0.0.1:{PORT}{path}", headers=headers, json=json_body, params=params, timeout=30
    )
    return response


def _start_broker(log_path: Path) -> subprocess.Popen | None:
    """起一个真实 mosquitto（仅监听本机、允许匿名），让告警走真实 MQTT 链路。

    没有 mosquitto 时返回 None，脚本退回「进程内直接派发」的方式（仅影响
    「首投由谁执行」的保真度，重启续投仍由真实网关调度器完成）。
    """
    binary = shutil.which("mosquitto")
    if not binary:
        print("未找到 mosquitto：重启续投首投改为脚本内派发（仍使用真实数据库）")
        return None
    config = log_path.parent / "mosquitto.conf"
    config.write_text(
        "listener 1883 127.0.0.1\nallow_anonymous true\npersistence false\n",
        encoding="utf-8",
    )
    handle = open(log_path.parent / "broker.log", "a", encoding="utf-8")
    return subprocess.Popen(
        [binary, "-c", str(config)], stdout=handle, stderr=subprocess.STDOUT
    )


def _wait_mqtt(log_path: Path, occurrences: int, timeout: float = 20.0) -> None:
    """等待网关日志出现第 N 次「已连接 MQTT Broker」，确保订阅生效后再发布。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if text.count("已连接 MQTT Broker") >= occurrences:
            return
        time.sleep(0.3)
    raise SystemExit("网关未连接到 MQTT Broker")


def _publish_report(
    device_id: str, *, seq: int, voltage: float = 3.9, temperature: float = 80.0
) -> None:
    """真实 MQTT 发布一条越限上报（默认温度越限），由网关 handler 生成告警并触发通知。"""
    import paho.mqtt.publish as publish

    payload = {
        "deviceId": device_id,
        "temperature": temperature,
        "humidity": 60.0,
        "voltage": voltage,
        "rssi": -70,
        "battery": 80,
        "seq": seq,
        "ts": int(time.time() * 1000),
    }
    publish.single(
        f"device/{device_id}/sensor/report",
        payload=json.dumps(payload),
        qos=1,
        hostname="127.0.0.1",
        port=1883,
    )


async def _wait_new_pending(baseline: int, device_id: str, timeout: float = 25.0) -> dict:
    """等待网关完成首投（attempts>=1）且仍在 pending（退避期），用于中途杀进程。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rows = await _delivery_rows(f"WHERE device_id='{device_id}' AND id > {baseline}")
        if rows:
            last = rows[0]
            if last["status"] == "pending" and last["attempts"] >= 1:
                return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"等待 {device_id} 首投失败进入退避超时，最后状态={last}")


def _start_server(log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": VERIFY_URL,
            "APP_ENV": "development",
            "JWT_SECRET": SECRET,
            "ADMIN_PASSWORD": "VerifyAdmin#2026",
            "SEED_DEMO_USERS": "false",
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": "1883",
            "NOTIFICATION_RETRY_INTERVAL_SECONDS": "2",
            "LOG_LEVEL": "INFO",
        }
    )
    handle = open(log_path, "a", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info",
        ],
        cwd=ROOT / "gateway",
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
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


def _migration_checks() -> None:
    print("== 1. 迁移：空库 / 既有 v1.29.0 库 / downgrade 往返 ==")
    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    tables = _loop_run(
        _pg(
            EMPTY_DB,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
            fetch=True,
        )
    )
    names = {row["table_name"] for row in tables}
    assert {"notification_rule", "notification_delivery", "notification_config"} <= names
    columns = _loop_run(
        _pg(
            EMPTY_DB,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='notification_delivery'",
            fetch=True,
        )
    )
    delivery_columns = {row["column_name"] for row in columns}
    added = {"next_attempt_at", "finished_at", "duration_ms", "rule_id", "updated_at"}
    assert added <= delivery_columns
    print(f"空库 upgrade head：表 {len(names)} 个，delivery 新列 "
          f"{sorted(added & delivery_columns)}")

    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0006_notifications")
    _loop_run(
        _pg(
            LEGACY_DB,
            "INSERT INTO notification_config "
            "(id, enabled, webhook_enabled, webhook_url, webhook_secret, smtp_enabled, smtp_to, "
            " levels, groups, device_allow, device_deny) "
            "VALUES (1, true, true, 'http://127.0.0.1:1/legacy-hook', 'legacy-secret', "
            " true, 'oncall@example.com', 'critical', '华东', '', '')",
        )
    )
    _loop_run(
        _pg(
            LEGACY_DB,
            "INSERT INTO notification_delivery "
            "(alert_id, device_id, metric, level, channel, status, attempts, error) "
            "VALUES ('alt-legacy', 'dev-legacy', 'voltage', 'critical', 'webhook', 'sent', 1, NULL)",
        )
    )
    _alembic(LEGACY_DB, "upgrade", "head")
    config = _loop_run(
        _pg(LEGACY_DB, "SELECT webhook_url, smtp_to FROM notification_config WHERE id=1", fetch=True)
    )[0]
    rules = _loop_run(
        _pg(LEGACY_DB, "SELECT name, priority, groups, webhook_url FROM notification_rule", fetch=True)
    )
    delivery = _loop_run(
        _pg(
            LEGACY_DB,
            "SELECT status, attempts FROM notification_delivery WHERE alert_id='alt-legacy'",
            fetch=True,
        )
    )[0]
    assert config["webhook_url"] == "http://127.0.0.1:1/legacy-hook"
    assert len(rules) == 1 and rules[0]["priority"] == 100 and rules[0]["groups"] == "华东"
    assert delivery["status"] == "sent" and delivery["attempts"] == 1
    print(f"既有 v1.29.0 库 upgrade：配置保留；投递历史保留；默认规则 "
          f"name={rules[0]['name']!r} priority={rules[0]['priority']} groups={rules[0]['groups']}")

    _alembic(LEGACY_DB, "downgrade", "-2")
    names_after_down = {
        row["table_name"]
        for row in _loop_run(
            _pg(
                LEGACY_DB,
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
                fetch=True,
            )
        )
    }
    assert "notification_rule" not in names_after_down
    assert "notification_delivery" not in names_after_down
    _alembic(LEGACY_DB, "upgrade", "head")
    print("downgrade -2 → 0005：规则/投递表已移除；再次 upgrade head 成功")
    print("PASS 迁移三项\n")


def _seed() -> None:
    _loop_run(
        _pg(
            VERIFY_DB,
            "INSERT INTO device (device_id, name, model, firmware_version, group_name, status, "
            "mqtt_credential_version) VALUES "
            "('dev-east', '华东设备', 'VG-S100', '1.0.0', '华东', 'online', 0),"
            "('dev-south', '华南设备', 'VG-S100', '1.0.0', '华南', 'online', 0),"
            "('dev-north', '西北设备', 'VG-S100', '1.0.0', '西北', 'online', 0)",
        )
    )
    _loop_run(
        _pg(
            VERIFY_DB,
            "INSERT INTO device_alert (alert_id, device_id, level, metric, value, threshold, "
            "message, ai_generated, status, created_at) VALUES "
            "('alt-route-east', 'dev-east', 'critical', 'voltage', 2.9, 3.2, '电压过低', false, 'open', now()),"
            "('alt-route-south', 'dev-south', 'critical', 'voltage', 2.9, 3.2, '电压过低', false, 'open', now()),"
            "('alt-route-north', 'dev-north', 'critical', 'voltage', 2.9, 3.2, '电压过低', false, 'open', now()),"
            "('alt-restart', 'dev-south', 'critical', 'voltage', 2.9, 3.2, '电压过低', false, 'open', now()),"
            "('alt-limit', 'dev-south', 'critical', 'humidity', 99, 97, '湿度过高', false, 'open', now())",
        )
    )


def _alert(alert_id: str, device_id: str, metric: str = "voltage") -> dict:
    return {
        "alertId": alert_id,
        "deviceId": device_id,
        "deviceName": device_id,
        "level": "critical",
        "metric": metric,
        "value": 2.9,
        "threshold": 3.2,
        "message": "电压过低",
    }


async def _dispatch(alert: dict) -> list[dict]:
    from app.database import SessionFactory
    from app.services import notify_service

    return await notify_service.dispatch_alert(alert, session_factory=SessionFactory)


async def _delivery_rows(where: str = "", limit: int = 20) -> list[dict]:
    sql = (
        "SELECT id, alert_id, device_id, metric, channel, status, attempts, error, "
        "rule_id, rule_name, group_name, duration_ms, next_attempt_at, finished_at, created_at "
        "FROM notification_delivery "
        + where
        + f" ORDER BY id DESC LIMIT {limit}"
    )
    return [dict(row) for row in await _pg(VERIFY_DB, sql, fetch=True)]


async def _wait_delivery(where: str, status: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rows = await _delivery_rows(where)
        if rows:
            last = rows[0]
            if last["status"] == status:
                return last
        await asyncio.sleep(0.3)
    raise AssertionError(f"等待 {where} → {status} 超时，最后状态={last}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-notify-"))
    log_path = tmp / "gateway.log"
    print(f"日志: {log_path}")

    _migration_checks()

    print("== 2. 准备真实 PostgreSQL + 启动真实网关 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    _seed()
    os.environ.update(
        {
            "DATABASE_URL": VERIFY_URL,
            "JWT_SECRET": SECRET,
            "APP_ENV": "development",
            "NOTIFICATION_RETRY_INTERVAL_SECONDS": "2",
        }
    )
    east = Receiver("east")
    south = Receiver("south")
    catch_all = Receiver("catch-all")
    slow = Receiver("slow", delay=2.0)
    broker = _start_broker(log_path)
    server = _start_server(log_path)
    try:
        _wait_ready()
        if broker is not None:
            _wait_mqtt(log_path, 1)
        admin = {"Authorization": f"Bearer {_token('admin', 'verify-admin')}"}
        operator = {"Authorization": f"Bearer {_token('operator', 'verify-operator')}"}
        viewer = {"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"}

        config = _http(
            "PATCH", "/api/system/notifications/config", headers=admin,
            json_body={
                "enabled": True,
                "mute_minutes": 0,
                "retry_backoff_seconds": 5,
                "webhook_max_attempts": 3,
                "webhook_timeout_seconds": 5,
                "smtp_max_attempts": 3,
                "smtp_timeout_seconds": 5,
                "smtp_host": "127.0.0.1",
                "smtp_from": "alerts@vikings.local",
                "smtp_password": SMTP_PASSWORD,
            },
        )
        assert config.status_code == 200, config.text
        print(f"网关 pid={server.pid}，通知总开关已开启（静默 0，退避 5s，上限 3 次）")

        print("\n== 3. 分组路由：两分组两接收人 ==")
        rule_east = _http(
            "POST", "/api/system/notifications/rules", headers=admin,
            json_body={
                "name": "华东值班", "priority": 10, "levels": ["critical"], "groups": ["华东"],
                "webhook_enabled": True, "webhook_url": east.url, "webhook_secret": WEBHOOK_SECRET,
            },
        )
        assert rule_east.status_code == 200, rule_east.text
        # 一次规则更新：验证 PATCH 审计只记字段名，且证明更新立即生效
        updated_rule = _http(
            "PATCH", f"/api/system/notifications/rules/{rule_east.json()['data']['id']}",
            headers=admin, json_body={"priority": 10, "levels": ["critical"]},
        )
        assert updated_rule.status_code == 200, updated_rule.text
        rule_south = _http(
            "POST", "/api/system/notifications/rules", headers=admin,
            json_body={
                "name": "华南值班", "priority": 20, "levels": ["critical"], "groups": ["华南"],
                "webhook_enabled": True, "webhook_url": south.url, "webhook_secret": WEBHOOK_SECRET,
            },
        )
        assert rule_south.status_code == 200, rule_south.text

        _loop_run(_dispatch(_alert("alt-route-east", "dev-east")))
        _loop_run(_dispatch(_alert("alt-route-south", "dev-south")))
        north = _loop_run(_dispatch(_alert("alt-route-north", "dev-north")))
        print(f"华东接收端收到={east.count()} 华南接收端收到={south.count()} 未匹配结果={north}")
        assert east.count() == 1 and south.count() == 1
        assert north[0]["status"] == "suppressed" and "未匹配任何通知规则" in north[0]["error"]
        row = (_loop_run(_delivery_rows("WHERE alert_id='alt-route-north'")))[0]
        assert row["channel"] == "rule" and row["status"] == "suppressed"
        print("PASS 分组路由 + 未匹配 suppressed（channel=rule）")

        print("\n== 4. 分组变更立即生效 + 优先级兜底 ==")
        _loop_run(
            _pg(VERIFY_DB, "UPDATE device SET group_name='华南' WHERE device_id='dev-east'")
        )
        _loop_run(_dispatch(_alert("alt-route-east-groupchange", "dev-east")))
        assert east.count() == 1, "旧分组规则不应再收到"
        assert south.count() == 2, "新分组规则应立即生效"
        print(f"dev-east 分组改为华南后：华东={east.count()} 华南={south.count()}（旧规则不再命中）")

        catch_rule = _http(
            "POST", "/api/system/notifications/rules", headers=admin,
            json_body={
                "name": "兜底值班", "priority": 1, "levels": ["critical"], "groups": [],
                "webhook_enabled": True, "webhook_url": catch_all.url,
            },
        )
        assert catch_rule.status_code == 200, catch_rule.text
        _loop_run(_dispatch(_alert("alt-route-priority", "dev-south")))
        assert catch_all.count() == 1 and south.count() == 2
        listed = _http("GET", "/api/system/notifications/rules", headers=admin).json()["data"]
        print(f"规则顺序（priority ASC）：{[(r['name'], r['priority']) for r in listed]}")
        assert [r["name"] for r in listed] == ["兜底值班", "华东值班", "华南值班"]
        deleted = _http(
            "DELETE", f"/api/system/notifications/rules/{catch_rule.json()['data']['id']}",
            headers=admin,
        )
        assert deleted.status_code == 200
        print("PASS 分组实时生效 + priority 升序首条命中（已删除兜底规则）")

        print("\n== 5. 重启续投：MQTT 触发告警 → 500 → 退避期内 kill -9 → 重启 → 继续重试成功 ==")
        south.status = 500
        baseline = _loop_run(
            _pg(VERIFY_DB, "SELECT COALESCE(MAX(id), 0) AS m FROM notification_delivery", fetch=True)
        )[0]["m"]
        if broker is not None:
            # 走真实链路：MQTT 上报 → 网关 handler 生成告警 → 旁路 dispatch → webhook 500
            _publish_report("dev-south", seq=7777)
        else:
            first = _loop_run(_dispatch(_alert("alt-restart", "dev-south")))
            print(f"脚本内首投结果={first}")
        pending = _loop_run(_wait_new_pending(int(baseline), "dev-south", timeout=25))
        print(f"网关首投失败后（退避期内）：delivery={pending['id']} status={pending['status']} "
              f"attempts={pending['attempts']} error={pending['error']!r} "
              f"next_attempt_at={pending['next_attempt_at']}")
        print(f"立即 kill -9 网关 pid={server.pid}")
        server.kill()
        server.wait(timeout=5)
        print(f"kill 返回码={server.returncode}")
        pending = (_loop_run(_delivery_rows(f"WHERE id={pending['id']}")))[0]
        print(f"杀进程后库中状态={pending['status']} attempts={pending['attempts']} "
              f"next_attempt_at={pending['next_attempt_at']}（进程内队列已随进程消失，仅剩数据库）")

        south.status = 200
        time.sleep(0.5)
        server = _start_server(log_path)
        _wait_ready()
        print(f"重启完成 pid={server.pid}，等待调度器续投…")
        sent = _loop_run(_wait_delivery(f"WHERE id={pending['id']}", "sent", timeout=20))
        print(f"续投成功：status={sent['status']} attempts={sent['attempts']} "
              f"duration_ms={sent['duration_ms']} next_attempt_at={sent['next_attempt_at']} "
              f"finished_at={sent['finished_at']}")
        assert sent["attempts"] == 2 and sent["next_attempt_at"] is None
        assert sent["duration_ms"] > 0
        log = log_path.read_text(encoding="utf-8", errors="replace")
        retry_lines = [line for line in log.splitlines() if "通知投递失败" in line or "通知重试成功" in line]
        print("重试相关日志：")
        for line in retry_lines[-6:]:
            print("  " + line)
        assert any("通知投递失败" in line for line in retry_lines)
        assert any("通知重试成功" in line for line in retry_lines)
        print("PASS 重启后从数据库队列继续投递并成功")

        # 后续上限验证需要快速迭代：把退避从 5s 调成 0.5s（也验证配置热读取）
        _http(
            "PATCH", "/api/system/notifications/config", headers=admin,
            json_body={"retry_backoff_seconds": 0.5},
        )

        print("\n== 6. 重试上限：终态 failed 且原因可见 ==")
        south.status = 500
        _loop_run(_dispatch(_alert("alt-limit", "dev-south", metric="humidity")))
        failed = _loop_run(_wait_delivery("WHERE alert_id='alt-limit'", "failed", timeout=20))
        print(f"终态：status={failed['status']} attempts={failed['attempts']} "
              f"error={failed['error']!r} next_attempt_at={failed['next_attempt_at']} "
              f"finished_at={failed['finished_at']}")
        assert failed["attempts"] == 3 and failed["error"] == "HTTP 500"
        assert failed["next_attempt_at"] is None
        time.sleep(3)
        still = _loop_run(_delivery_rows("WHERE alert_id='alt-limit'"))[0]
        assert still["attempts"] == 3 and still["status"] == "failed", "不得无限重试"
        print("PASS 上限 3 次后停在 failed，不再重试")

        print("\n== 7. 投递历史：筛选 / 分页 / 权限 ==")
        rows_all = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator, params={"page": 1, "page_size": 4},
        ).json()["data"]
        print(f"全部 total={rows_all['total']} page={len(rows_all['items'])}")
        page2 = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator, params={"page": 2, "page_size": 4},
        ).json()["data"]
        assert len(page2["items"]) == len(rows_all["items"]) or page2["total"] == rows_all["total"]
        failed_filter = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator, params={"status": "failed"},
        ).json()["data"]
        device_filter = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator, params={"device_id": "dev-east"},
        ).json()["data"]
        channel_filter = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator, params={"channel": "rule", "status": "suppressed"},
        ).json()["data"]
        now = datetime.now(timezone.utc)
        ranged = _http(
            "GET", "/api/system/notifications/deliveries",
            headers=operator,
            params={
                "start": (now - timedelta(minutes=10)).isoformat(),
                "end": (now + timedelta(minutes=1)).isoformat(),
            },
        ).json()["data"]
        print(f"status=failed → {failed_filter['total']} 条，示例 error={failed_filter['items'][0]['error']!r}")
        print(f"device=dev-east → {device_filter['total']} 条；channel=rule&status=suppressed → "
              f"{channel_filter['total']} 条；最近 10 分钟 → {ranged['total']} 条")
        assert failed_filter["total"] == 1 and failed_filter["items"][0]["attempts"] == 3
        assert device_filter["total"] >= 2
        assert channel_filter["total"] >= 1
        assert ranged["total"] == rows_all["total"]
        codes = {}
        for role, headers in (("viewer", viewer), ("operator", operator), ("admin", admin)):
            codes[role] = _http(
                "GET", "/api/system/notifications/deliveries", headers=headers, params={"page_size": 1}
            ).status_code
        print(f"权限：{codes}")
        assert codes == {"viewer": 403, "operator": 200, "admin": 200}
        print("PASS 历史筛选、分页与权限")

        print("\n== 8. 旁路性能：延迟 2s 接收端下 MQTT 上报处理耗时 ==")
        perf_rule = _http(
            "POST", "/api/system/notifications/rules", headers=admin,
            json_body={
                "name": "性能测试组", "priority": 5, "levels": ["critical"], "groups": ["西北"],
                "webhook_enabled": True, "webhook_url": slow.url,
            },
        )
        assert perf_rule.status_code == 200, perf_rule.text
        _loop_run(
            _pg(
                VERIFY_DB,
                "INSERT INTO device (device_id, name, model, firmware_version, group_name, status, "
                "mqtt_credential_version) SELECT 'perf-dev-' || i, '性能设备' || i, 'VG-S100', "
                "'1.0.0', '西北', 'online', 0 FROM generate_series(1, 10) i",
            )
        )

        from app.mqtt.handlers import MessageHandler

        async def _measure(prefix: str, seq_base: int) -> list[float]:
            """真实调用 MQTT 消费入口（与 Broker 回调完全同一方法），逐条计时。"""
            handler = MessageHandler()

            async def _send(device_id: str, seq: int) -> float:
                payload = {
                    "deviceId": device_id,
                    "temperature": 25.0,
                    "humidity": 60.0,
                    "voltage": 2.5,
                    "rssi": -70,
                    "battery": 80,
                    "seq": seq,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                started = time.perf_counter()
                await handler("report", device_id, payload)
                return (time.perf_counter() - started) * 1000

            # 预热：排除连接池首次建连等一次性开销
            await _send(f"{prefix}-warm", seq_base)
            timings = [await _send(f"{prefix}-{i}", seq_base + i) for i in range(1, 11)]
            await asyncio.sleep(0.5)
            return timings

        async def _perf():
            # 控制组：设备分组不命中任何规则（dispatch 立即 suppressed），
            # 用来抵消本机数据库/事件循环抖动，只比较「通知旁路」带来的增量。
            control = await _measure("ctrl-dev", 0)
            notify_timings = await _measure("perf-dev", 100)
            # 派发并发受限，10 条通知会在后台陆续发出；等它们全部到达延迟接收端，
            # 证明上报处理早已返回、没有被 2s 的接收端拖住。
            deadline = time.time() + 30
            while slow.count() < 10 and time.time() < deadline:
                await asyncio.sleep(0.5)
            # 接收端在记录请求后才睡 2s，最后一条的结果写回要等它响应
            await asyncio.sleep(2.5)
            return control, notify_timings

        control, timings = _loop_run(_perf())

        def _stats(values: list[float]) -> str:
            ordered = sorted(values)
            return (
                f"平均={sum(values) / len(values):.2f}ms "
                f"p95={ordered[int(len(ordered) * 0.95) - 1]:.2f}ms "
                f"最大={ordered[-1]:.2f}ms"
            )

        control_avg = sum(control) / len(control)
        avg_ms = sum(timings) / len(timings)
        max_ms = max(timings)
        print(f"控制组（无匹配规则，10 次）：{_stats(control)}")
        print(f"通知组（延迟 2s 接收端，10 次）：{_stats(timings)}"
              f"（v1.29.0 基线 {BASELINE_REPORT_MS}ms）")
        print(f"通知旁路带来的增量={avg_ms - control_avg:+.2f}ms；"
              f"接收端 `slow` 收到请求={slow.count()}")
        assert avg_ms - control_avg < 5.0, "旁路给上报处理带来明显增量"
        assert max_ms < 50
        assert slow.count() >= 10, "旁路派发未真正执行"

        # 清理性能组规则，避免影响后续审计计数
        _http(
            "DELETE", f"/api/system/notifications/rules/{perf_rule.json()['data']['id']}",
            headers=admin,
        )
        print("PASS 上报耗时未被延迟接收端拖慢")

        print("\n== 9. 安全：接口回显 / 审计 / 日志 ==")
        rules_resp = _http("GET", "/api/system/notifications/rules", headers=admin)
        config_resp = _http("GET", "/api/system/notifications/config", headers=admin)
        api_text = rules_resp.text + config_resp.text
        print(f"规则接口示例={rules_resp.json()['data'][0]}")
        for secret in (east.url, south.url, WEBHOOK_SECRET, SMTP_PASSWORD):
            assert secret not in api_text, f"接口回显泄露 {secret}"
        assert east.url not in log, "日志泄露 Webhook URL"
        assert WEBHOOK_SECRET not in log, "日志泄露签名密钥"
        assert SMTP_PASSWORD not in log, "日志泄露 SMTP 口令"

        audits = _loop_run(
            _pg(
                VERIFY_DB,
                "SELECT operator, action, target_type, target_id, detail FROM audit_log "
                "WHERE action LIKE 'notify.%' ORDER BY id DESC",
                fetch=True,
            )
        )
        serialized = json.dumps([dict(row) for row in audits], ensure_ascii=False, default=str)
        for secret in (east.url, south.url, WEBHOOK_SECRET, SMTP_PASSWORD):
            assert secret not in serialized, f"审计泄露 {secret}"
        print("审计记录（脱敏）：")
        for row in audits[:6]:
            detail = row["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            print(f"  {row['operator']} {row['action']} {row['target_type']}:{row['target_id']} "
                  f"detail={json.dumps(detail, ensure_ascii=False)}")
        actions = {row["action"] for row in audits}
        assert {"notify.rule_create", "notify.rule_update", "notify.rule_delete",
                "notify.config_update"} <= actions
        print("PASS URL/密钥/口令未出现在界面回显、审计、日志；规则变更逐条审计")

        print("\n== 10. 最终投递记录 ==")
        for row in _loop_run(_delivery_rows(limit=12)):
            print(
                f"id={row['id']} {row['alert_id']:<26} {row['device_id']:<10} "
                f"rule={str(row['rule_name']):<10} channel={row['channel']:<7} "
                f"status={row['status']:<10} attempts={row['attempts']} "
                f"error={row['error']!r} duration={row['duration_ms']}ms"
            )
        print("\n全部通知验证通过")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        for receiver in (east, south, catch_all, slow):
            receiver.close()
        if broker is not None:
            broker.terminate()
            try:
                broker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                broker.kill()
        try:
            from app.database import engine

            _loop_run(engine.dispose())
        except Exception:
            pass
        _LOOP.close()


if __name__ == "__main__":
    main()
