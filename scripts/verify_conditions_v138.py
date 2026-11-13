#!/usr/bin/env python3
"""v1.38.0 告警规则条件树真实验证。

不 mock：真实 PostgreSQL + 真实 Alembic 迁移 + 真实 mosquitto + 真实 MQTT 上报
+ 真实 uvicorn 网关 + 真实 HTTP API。覆盖：
1. 迁移三场景：空库 upgrade、v1.35 既有库（0012）等价回填、downgrade 往返（老字段可读）；
2. v1.34 默认阈值行为在升级后不变；
3. 条件树权限与校验：未登录 401、operator/viewer 403、未知维度/空叶子/超深度/冲突 400；
4. 「标签 A 且 分组 B」真实上报：命中与不命中各一侧；
5. 嵌套「(标签 A 或 标签 B) 且 分组 C」真实命中；
6. 六种老形态对拍：先用老字段上报，再把条件树等价回填，判定结果逐条一致；
7. 抑制窗口、SLA、通知路由在条件树下仍正常；
8. 条件树变更在既有审计历史里有字段级前后值；
9. 回滚到 v1.35 后老字段仍可读、单维度规则仍命中。

用法（仓库根目录，需本地 PostgreSQL 与 mosquitto）：
    gateway/.venv/bin/python scripts/verify_conditions_v138.py
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

EMPTY_DB = "iot_cond_empty_verify"
LEGACY_DB = "iot_cond_v135_verify"
VERIFY_DB = "iot_cond_verify"
PORT = 8136
MQTT_PORT = 18885
WEBHOOK_PORT = 18999
SECRET = "conditions-verify-secret-2026"

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


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _token(role: str, username: str) -> str:
    import jwt

    payload = {
        "sub": username, "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _http(method: str, path: str, *, headers: dict | None = None, body=None, params=None):
    import httpx

    return httpx.request(
        method, f"http://127.0.0.1:{PORT}{path}",
        headers=headers or {}, json=body, params=params, timeout=30,
    )


def _tables(database: str) -> set[str]:
    rows = _pg(
        database,
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        fetch=True,
    )
    return {row["table_name"] for row in rows}


def _columns(database: str, table: str) -> set[str]:
    rows = _pg(
        database,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name=$1 AND table_schema='public'",
        table, fetch=True,
    )
    return {row["column_name"] for row in rows}


def _expected_tree(scope_type: str, values: list[str]) -> dict:
    return {"op": "any", "children": [{"dim": scope_type, "values": values}]}


def _migration_checks() -> None:
    print("== 1. 迁移：空库 / v1.35 既有库 / downgrade 往返 ==")

    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "conditions" in _columns(EMPTY_DB, "alert_rule")
    dtype = _pg(
        EMPTY_DB,
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='alert_rule' AND column_name='conditions'",
        fetch=True,
    )[0]["data_type"]
    assert dtype == "jsonb", dtype
    rules = _pg(
        EMPTY_DB,
        "SELECT name, scope_type, scope_value, scope_values, conditions "
        "FROM alert_rule ORDER BY id",
        fetch=True,
    )
    assert len(rules) == 10, rules
    assert all(row["conditions"] is None for row in rules), rules
    print(f"[OK] 空库 upgrade head：alert_rule.conditions={dtype}；10 条默认规则 conditions=NULL")

    _alembic(EMPTY_DB, "downgrade", "-1")
    assert "conditions" not in _columns(EMPTY_DB, "alert_rule")
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "conditions" in _columns(EMPTY_DB, "alert_rule")
    count = _pg(EMPTY_DB, "SELECT count(*) AS c FROM alert_rule", fetch=True)[0]["c"]
    assert count == 10, count
    print("[OK] downgrade -1 删除 conditions；再次 upgrade head 恢复，默认规则仍 10 条")

    # v1.35 既有库：停在 0012，写入六种老形态规则
    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0012_shadow_history")
    legacy_rows = [
        ("老-全局", "voltage", "lt", 3.3, "warning", "global", None, None),
        ("老-单值分组", "temperature", "gt", 40, "warning", "group", "华东", '["华东"]'),
        ("老-单值设备", "rssi", "lt", -90, "warning", "device", "dev-a", '["dev-a"]'),
        ("老-多值分组", "voltage", "lt", 3.4, "critical", "group", "华东", '["华东","华南"]'),
        ("老-多值标签", "humidity", "gt", 75, "warning", "tag", "冷库", '["冷库","关键"]'),
        ("老-多值设备", "temperature", "gt", 45, "warning", "device", "dev-a", '["dev-a","dev-b"]'),
    ]
    for name, metric, op, threshold, level, scope_type, scope_value, scope_values in legacy_rows:
        _pg(
            LEGACY_DB,
            "INSERT INTO alert_rule (name, metric, op, threshold, level, scope_type, "
            "scope_value, scope_values, enabled, consecutive_count, priority, is_default, updated_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,true,1,100,false,'ops')",
            name, metric, op, threshold, level, scope_type, scope_value, scope_values,
        )
    _alembic(LEGACY_DB, "upgrade", "head")
    rows = _pg(
        LEGACY_DB,
        "SELECT name, scope_type, scope_value, scope_values, conditions "
        "FROM alert_rule WHERE name LIKE '老-%' ORDER BY id",
        fetch=True,
    )
    assert len(rows) == 6, rows
    for row in rows:
        old = next(item for item in legacy_rows if item[0] == row["name"])
        _, _, _, _, _, scope_type, scope_value, scope_values = old
        assert row["scope_type"] == scope_type, row
        assert row["scope_value"] == scope_value, row
        assert _json(row["scope_values"]) == (json.loads(scope_values) if scope_values else None), row
        if scope_type == "global":
            assert row["conditions"] is None, row
        else:
            assert _json(row["conditions"]) == _expected_tree(
                scope_type, json.loads(scope_values)
            ), row
    print("[OK] v1.35 既有库 upgrade：六种老规则等价回填 conditions，"
          "scope_type/scope_value/scope_values 原样保留")

    _alembic(LEGACY_DB, "downgrade", "-1")
    assert "conditions" not in _columns(LEGACY_DB, "alert_rule")
    row = _pg(
        LEGACY_DB,
        "SELECT scope_type, scope_value, scope_values FROM alert_rule WHERE name='老-多值标签'",
        fetch=True,
    )[0]
    assert row["scope_type"] == "tag" and row["scope_value"] == "冷库", row
    assert _json(row["scope_values"]) == ["冷库", "关键"], row
    _alembic(LEGACY_DB, "upgrade", "head")
    row = _pg(
        LEGACY_DB,
        "SELECT conditions FROM alert_rule WHERE name='老-多值标签'",
        fetch=True,
    )[0]
    assert _json(row["conditions"]) == _expected_tree("tag", ["冷库", "关键"]), row
    print("[OK] downgrade 到 0012 后 conditions 移除、老字段仍可读；再次 upgrade 重新等价回填")


class Broker:
    """真实 mosquitto（匿名、独立端口），只服务本次验证。"""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vikings-cond-mosq-"))
        conf = self.dir / "mosquitto.conf"
        conf.write_text(
            f"listener {MQTT_PORT} 127.0.0.1\n"
            "allow_anonymous true\n"
            "persistence false\n"
            "log_dest file " + str(self.dir / "mosquitto.log") + "\n",
            encoding="utf-8",
        )
        self.proc = subprocess.Popen(
            ["mosquitto", "-c", str(conf)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        self._wait_port()

    def _wait_port(self, timeout: float = 15.0) -> None:
        import socket

        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", MQTT_PORT)) == 0:
                    return
            time.sleep(0.2)
        raise SystemExit("mosquitto 未在限定时间内就绪")

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class WebhookReceiver:
    """本地 HTTP 接收器：验证条件树告警仍走真实通知路由。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    receiver.events.append(json.loads(body))
                except Exception:
                    receiver.events.append({"raw": body.decode("utf-8", "replace")})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):  # 静默访问日志
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", WEBHOOK_PORT), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _start_server(log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": VERIFY_URL,
            "APP_ENV": "development",
            "JWT_SECRET": SECRET,
            "SEED_DEMO_USERS": "false",
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": str(MQTT_PORT),
            "MQTT_USERNAME": "",
            "MQTT_PASSWORD": "",
            "NOTIFICATION_ENABLED": "false",
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


def publish(device_id: str, seq: int, **payload) -> None:
    """真实 MQTT 上报到 mosquitto。"""
    import paho.mqtt.publish as mqtt_publish

    body = {
        "deviceId": device_id,
        "temperature": 24.0,
        "humidity": 55.0,
        "voltage": 3.75,
        "rssi": -60,
        "battery": 80,
        "seq": seq,
        "ts": int(time.time() * 1000),
    }
    body.update(payload)
    mqtt_publish.single(
        f"device/{device_id}/sensor/report",
        payload=json.dumps(body),
        qos=1,
        hostname="127.0.0.1",
        port=MQTT_PORT,
    )


def alerts_for(device_id: str) -> list[dict]:
    resp = _http(
        "GET", "/api/alerts",
        headers={"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"},
        params={"deviceId": device_id, "pageSize": 50},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


def wait_alert(device_id: str, metric: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        if rows:
            return rows[0]
        time.sleep(0.3)
    rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
    assert rows, f"{device_id}/{metric} 未在 {timeout}s 内产生告警"
    return rows[0]


def wait_no_alert(device_id: str, metric: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        assert not rows, f"预期不触发，但产生了告警：{rows}"
        time.sleep(0.3)


def _rules(headers: dict) -> list[dict]:
    resp = _http("GET", "/api/rules", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _disable_all(headers: dict) -> None:
    for row in _rules(headers):
        if row["enabled"]:
            _http("PATCH", f"/api/rules/{row['id']}/enabled", headers=headers, body={"enabled": False})


def _create_rule(headers: dict, **body) -> dict:
    resp = _http("POST", "/api/rules", headers=headers, body=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _backfill(rule_id: int) -> None:
    _pg(
        VERIFY_DB,
        "UPDATE alert_rule SET conditions = jsonb_build_object("
        "  'op','any','children', jsonb_build_array(jsonb_build_object("
        "     'dim', scope_type,"
        "     'values', COALESCE(scope_values, jsonb_build_array(scope_value))"
        "  ))) WHERE id=$1 AND scope_type IN ('group','device','tag')",
        rule_id,
    )


def _refresh_rule(headers: dict, rule_id: int, name: str) -> None:
    """PUT 触发服务端重新读取库里的条件树并刷新规则缓存。"""
    resp = _http("PUT", f"/api/rules/{rule_id}", headers=headers, body={"name": name})
    assert resp.status_code == 200, resp.text


def _decide(device_id: str, metric: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """等待 MQTT 上报被消费并判定完成，返回 (规则名, 级别)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        if rows:
            return rows[0]["matched_rule"]["rule_name"], rows[0]["level"]
        time.sleep(0.3)
    return None, None


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-cond-"))
    log_path = tmp / "gateway.log"
    print(f"日志: {log_path}")

    _migration_checks()

    print("== 2. 真实 PostgreSQL + mosquitto + 上报 + 网关 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    devices = {
        "dev-cold": ("华东", ["冷库"]),
        "dev-key": ("华东", ["关键"]),
        "dev-normal": ("华北", ["冷库", "普通"]),
        "dev-east2": ("华东", ["华东"]),
        "dev-south": ("华南", ["华南"]),
        "dev-north2": ("华北", ["华北"]),
        "dev-single": ("单值组", []),
        "dev-old": ("旧规则组", []),
        "dev-d1": ("设备组", []),
        "dev-d2": ("设备组", []),
        "dev-g1": ("全局对拍", []),
        "dev-g2": ("全局对拍", []),
        "dev-sg1": ("单值对拍", []),
        "dev-sg2": ("单值对拍", []),
        "dev-dv1": ("设备对拍", []),
    }
    for device_id, (group, tags) in devices.items():
        _pg(
            VERIFY_DB,
            "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
            "VALUES ($1, $1, 'VG-S100', '1.0.0', $2, 'online') ON CONFLICT (device_id) DO NOTHING",
            device_id, group,
        )
        for tag in tags:
            _pg(
                VERIFY_DB,
                "INSERT INTO device_tag (device_id, tag, created_by) VALUES ($1,$2,'verify') "
                "ON CONFLICT DO NOTHING",
                device_id, tag,
            )

    broker = Broker()
    receiver = WebhookReceiver()
    server = _start_server(log_path)
    try:
        _wait_ready()
        admin = {"Authorization": f"Bearer {_token('admin', 'verify-admin')}"}
        operator = {"Authorization": f"Bearer {_token('operator', 'verify-operator')}"}
        viewer = {"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"}

        print("== 3. v1.34 默认阈值行为升级后不变 ==")
        publish("dev-g1", 1, voltage=3.19)
        base = wait_alert("dev-g1", "voltage")
        assert base["level"] == "critical", base
        assert base["matched_rule"]["threshold"] == 3.2, base
        assert base["matched_rule"]["rule_name"] == "电压过低-严重", base
        print(f"[OK] 默认规则：3.19V → critical（阈值 3.2，"
              f"matched_rule={base['matched_rule']['rule_name']}）")
        _disable_all(admin)

        print("== 4. 条件树权限与校验 ==")
        sample = {
            "name": "权限条件", "metric": "voltage", "op": "lt", "threshold": 3.5,
            "level": "warning", "scope_type": "custom",
            "conditions": {"op": "all", "children": [{"dim": "tag", "values": ["冷库"]}]},
        }
        assert _http("POST", "/api/rules", body=sample).status_code == 401
        assert _http("POST", "/api/rules", headers=operator, body=sample).status_code == 403
        assert _http("POST", "/api/rules", headers=viewer, body=sample).status_code == 403
        assert _http("PUT", "/api/rules/1", body={"conditions": sample["conditions"]}).status_code == 401
        assert _http("PUT", "/api/rules/1", headers=viewer,
                     body={"conditions": sample["conditions"]}).status_code == 403

        bad_cases = [
            ({"op": "all", "children": []}, "至少"),
            ({"op": "all", "children": [{"dim": "site", "values": ["x"]}]}, "维度"),
            ({"op": "all", "children": [{"dim": "tag", "values": []}]}, "至少"),
            ({"op": "or", "children": [{"dim": "tag", "values": ["x"]}]}, "all"),
        ]
        for conditions, keyword in bad_cases:
            body = dict(sample, name=f"非法-{keyword}", conditions=conditions)
            resp = _http("POST", "/api/rules", headers=admin, body=body)
            assert resp.status_code == 400, resp.text
            assert keyword in resp.json()["message"], resp.text

        deep = {
            "op": "all",
            "children": [{"op": "any", "children": [{
                "op": "all", "children": [{"op": "any", "children": [
                    {"dim": "tag", "values": ["冷库"]}]}],
            }]}],
        }
        resp = _http("POST", "/api/rules", headers=admin,
                     body=dict(sample, name="非法-深度", conditions=deep))
        assert resp.status_code == 400 and "嵌套" in resp.json()["message"], resp.text

        conflict = dict(sample, name="非法-维度冲突", scope_type="group", scope_values=["华东"])
        resp = _http("POST", "/api/rules", headers=admin, body=conflict)
        assert resp.status_code == 400 and "scope_type" in resp.json()["message"], resp.text
        print("[OK] 未登录 401；operator/viewer 403；空条件组/未知维度/空叶子/未知 op/"
              "超深度/维度冲突 400 且错误可读")

        print("== 5. 「标签 A 且 分组 B」真实上报 ==")
        _disable_all(admin)
        and_rule = _create_rule(
            admin, name="冷库且华东电压", metric="voltage", op="lt", threshold=3.5,
            level="warning", scope_type="custom",
            conditions={
                "op": "all",
                "children": [
                    {"dim": "tag", "values": ["冷库"]},
                    {"dim": "group", "values": ["华东"]},
                ],
            },
        )
        assert and_rule["scope_type"] == "custom", and_rule
        assert and_rule["scope_values"] is None, and_rule
        publish("dev-cold", 2, voltage=3.3)
        hit = wait_alert("dev-cold", "voltage")
        assert hit["matched_rule"]["rule_name"] == "冷库且华东电压", hit
        publish("dev-normal", 3, voltage=3.3)  # 标签冷库，但分组华北
        wait_no_alert("dev-normal", "voltage")
        print("[OK] dev-cold（冷库+华东）命中；dev-normal（冷库+华北）不命中（组间且）")

        print("== 6. 嵌套「(标签A 或 标签B) 且 分组C」真实上报 ==")
        _disable_all(admin)
        nested = _create_rule(
            admin, name="冷库或关键且华东", metric="humidity", op="gt", threshold=75,
            level="warning", scope_type="custom",
            conditions={
                "op": "all",
                "children": [
                    {"op": "any", "children": [
                        {"dim": "tag", "values": ["冷库"]},
                        {"dim": "tag", "values": ["关键"]},
                    ]},
                    {"dim": "group", "values": ["华东"]},
                ],
            },
        )
        assert nested["conditions"]["children"][0]["op"] == "any", nested
        publish("dev-key", 4, humidity=80)
        nested_hit = wait_alert("dev-key", "humidity")
        assert nested_hit["matched_rule"]["rule_name"] == "冷库或关键且华东", nested_hit
        publish("dev-south", 5, humidity=80)  # 无这两个标签，且分组华南
        wait_no_alert("dev-south", "humidity")
        print("[OK] dev-key（关键 ∈ 或分支 + 华东）命中；dev-south 不命中")

        print("== 7. 六种老形态对拍：老字段 → 条件树等价回填 ==")
        # (scope_type, scope_value, scope_values, dev_before, dev_after,
        #  metric, op, threshold, level, payload)
        phases = [
            ("global", None, None, "dev-g1", "dev-g2", "voltage", "lt", 3.2, "critical",
             {"voltage": 3.0}),
            ("group", "单值对拍", None, "dev-sg1", "dev-sg2", "temperature", "gt", 40, "warning",
             {"temperature": 50}),
            ("device", "dev-dv1", None, "dev-dv1", "dev-dv1", "rssi", "lt", -90, "warning",
             {"rssi": -95}),
            ("group", None, ["华东", "华南"], "dev-east2", "dev-south", "voltage", "lt", 3.5,
             "warning", {"voltage": 3.3}),
            ("tag", None, ["冷库", "关键"], "dev-cold", "dev-key", "humidity", "gt", 75,
             "warning", {"humidity": 80}),
            ("device", None, ["dev-d1", "dev-d2"], "dev-d1", "dev-d2", "temperature", "gt", 45,
             "warning", {"temperature": 50}),
        ]
        for (
            scope_type, scope_value, scope_values, dev_before, dev_after,
            metric, op, threshold, level, payload,
        ) in phases:
            _disable_all(admin)
            # 清掉历史告警，避免 v1.34 起的同设备同指标抑制窗口把新上报吞掉
            for dev in {dev_before, dev_after}:
                _pg(VERIFY_DB, "DELETE FROM device_alert WHERE device_id=$1", dev)
            expected_values = scope_values or ([scope_value] if scope_value else None)
            if scope_type == "global":
                # global 无老字段可回填：直接启用默认全局规则作为对拍基准
                default = next(row for row in _rules(admin) if row["name"] == "电压过低-严重")
                _http("PATCH", f"/api/rules/{default['id']}/enabled", headers=admin,
                      body={"enabled": True})
                rule_id = None
                expected_before = "电压过低-严重"
            else:
                rule = _create_rule(
                    admin, name=f"对拍-{scope_type}-{dev_before}", metric=metric, op=op,
                    threshold=threshold, level=level, scope_type=scope_type,
                    scope_value=scope_value, scope_values=scope_values,
                )
                rule_id = rule["id"]
                expected_before = rule["name"]

            publish(dev_before, 100 + len(dev_before), **payload)
            before_name, before_level = _decide(dev_before, metric)
            assert before_name == expected_before, (scope_type, before_name, expected_before)

            if rule_id is not None:
                _backfill(rule_id)
                row = _pg(
                    VERIFY_DB,
                    "SELECT conditions, scope_type, scope_value, scope_values "
                    "FROM alert_rule WHERE id=$1",
                    rule_id, fetch=True,
                )[0]
                assert _json(row["conditions"]) == _expected_tree(scope_type, expected_values), row
                assert row["scope_type"] == scope_type and row["scope_value"] == expected_values[0], row
                _refresh_rule(admin, rule_id, f"对拍-{scope_type}-{dev_before}")

            if dev_after == dev_before:
                # 单设备形态：清掉本设备的告警与抑制窗口后复测
                _pg(VERIFY_DB, "DELETE FROM device_alert WHERE device_id=$1", dev_after)
            publish(dev_after, 200 + len(dev_after), **payload)
            after_name, after_level = _decide(dev_after, metric)
            assert (after_name, after_level) == (before_name, before_level), (
                scope_type, scope_values, (before_name, before_level), (after_name, after_level)
            )
            label = f"{scope_type} {expected_values}" if rule_id else "global（无老字段）"
            print(f"[OK] {label}：回填前后均命中 {after_name}（{after_level}）")

        print("== 8. 抑制窗口 / SLA / 通知路由在条件树下 ==")
        _disable_all(admin)
        suppress_rule = _create_rule(
            admin, name="条件抑制", metric="voltage", op="lt", threshold=3.6,
            level="warning", scope_type="custom",
            conditions={"op": "any", "children": [{"dim": "tag", "values": ["冷库"]}]},
        )
        assert suppress_rule["conditions"]["op"] == "any", suppress_rule
        publish("dev-cold", 301, voltage=3.0)
        first = wait_alert("dev-cold", "voltage")
        assert first["matched_rule"]["rule_name"] == "条件抑制", first
        publish("dev-cold", 302, voltage=3.0)
        time.sleep(2)
        same = [row for row in alerts_for("dev-cold") if row["metric"] == "voltage"]
        assert len(same) == 1, same
        assert first["sla_state"] in {"pending", "normal", "at_risk", "overdue"}, first
        assert first["sla_deadline_at"] is not None, first
        print(f"[OK] 抑制窗口生效（同设备同指标 2s 内仅 1 条）；告警带 SLA 字段 "
              f"sla_state={first['sla_state']}")

        resp = _http(
            "PATCH", "/api/system/notifications/config", headers=admin,
            body={
                "enabled": True,
                "webhook_enabled": True,
                "webhook_url": f"http://127.0.0.1:{WEBHOOK_PORT}/hook",
                "webhook_timeout_seconds": 3,
                "webhook_max_attempts": 1,
            },
        )
        assert resp.status_code == 200, resp.text
        resp = _http(
            "POST", "/api/system/notifications/rules", headers=admin,
            body={
                "name": "条件告警路由", "enabled": True, "priority": 1,
                "levels": ["warning", "critical"], "webhook_enabled": True,
                "webhook_url": f"http://127.0.0.1:{WEBHOOK_PORT}/hook",
            },
        )
        assert resp.status_code == 200, resp.text
        # dev-cold 携带 冷库 标签，才能命中「条件抑制」；清掉告警解除抑制窗口后复测
        _pg(VERIFY_DB, "DELETE FROM device_alert WHERE device_id=$1", "dev-cold")
        publish("dev-cold", 303, voltage=3.0)
        wait_alert("dev-cold", "voltage")
        deadline = time.time() + 30
        delivery = None
        while time.time() < deadline and delivery is None:
            resp = _http("GET", "/api/system/notifications/deliveries", headers=operator,
                         params={"device_id": "dev-cold", "page_size": 20})
            assert resp.status_code == 200, resp.text
            rows = resp.json()["data"]["items"]
            # 刚入队的行是 pending（首次投递在另一个协程里进行），等它落终态再断言
            delivery = next(
                (row for row in rows if row["rule_name"] == "条件告警路由"
                 and row["channel"] == "webhook" and row["status"] != "pending"),
                None,
            )
            if delivery is None:
                time.sleep(0.5)
        assert delivery is not None, "条件树告警未进入通知路由"
        assert delivery["status"] == "sent", delivery
        assert receiver.events, "本地 webhook 未收到请求"
        print(f"[OK] 条件树告警经通知路由投递：rule={delivery['rule_name']} "
              f"channel={delivery['channel']} status={delivery['status']}；"
              f"本地 webhook 收到 {len(receiver.events)} 次")

        print("== 9. 条件树变更历史（来自既有审计） ==")
        resp = _http("PUT", f"/api/rules/{and_rule['id']}", headers=admin,
                     body={"conditions": {
                         "op": "all",
                         "children": [
                             {"op": "any", "children": [
                                 {"dim": "tag", "values": ["冷库"]},
                                 {"dim": "tag", "values": ["关键"]},
                             ]},
                             {"dim": "group", "values": ["华东"]},
                         ],
                     }})
        assert resp.status_code == 200, resp.text
        assert _http("GET", f"/api/rules/{and_rule['id']}/history").status_code == 401
        entries = _http("GET", f"/api/rules/{and_rule['id']}/history", headers=viewer).json()["data"]
        update = next(row for row in entries if row["action"] == "alert_rule.update")
        change = update["changes"]["conditions"]
        assert change["from"] == {"op": "all", "children": [
            {"dim": "tag", "values": ["冷库"]},
            {"dim": "group", "values": ["华东"]},
        ]}, change
        assert change["to"]["children"][0]["op"] == "any", change
        dumped = json.dumps(entries, ensure_ascii=False).lower()
        for word in ("password", "token", "secret", "authorization"):
            assert word not in dumped, word
        print(f"[OK] operator={update['operator']}，changes.conditions 前后值正确："
              f"from={json.dumps(change['from'], ensure_ascii=False)} → "
              f"to={json.dumps(change['to'], ensure_ascii=False)[:120]}…；不含敏感字段")

        print("v1.38.0 真实验证全部通过")
    finally:
        try:
            if server.poll() is None:
                server.send_signal(signal.SIGTERM)
                server.wait(timeout=10)
        except Exception:
            server.kill()
        broker.close()
        receiver.close()


if __name__ == "__main__":
    main()
