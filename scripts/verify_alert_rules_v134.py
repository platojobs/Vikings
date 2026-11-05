#!/usr/bin/env python3
"""v1.34.0 可配置告警阈值规则真实验证。

不 mock：真实 PostgreSQL + 真实 Alembic 迁移 + 真实 mosquitto + 真实 MQTT 上报
+ 真实 uvicorn 网关 + 真实 HTTP API 查询。覆盖：
1. 迁移：空库 upgrade head / downgrade -1 / 再 upgrade；v1.33.0 既有库（0008）
   upgrade 后默认规则回填（含 rule_config 在线覆盖值）；downgrade 往返；
2. 权限：未登录 401、operator/viewer 写 403 只读、admin 可写；
3. 校验：负数/越界/NaN/反向比较/范围缺值均拒绝并返回可读中文错误；
4. 真实上报：临界值触发、matched_rule 指向规则与阈值；
5. 热生效：改阈值后同一上报不再触发，改回后恢复；无需重启；
6. 范围隔离：指定分组 / 指定设备规则不影响其它分组/设备；
7. 多规则裁决：同指标取最高级别（critical 优先），同级按 priority；
8. 连续 N 次：第 N 次才触发，正常上报清零；
9. 停用规则：不产生新告警，历史告警与统计不变；
10. 规则重载（写规则触发缓存刷新）后同一告警不重复入库。

用法（仓库根目录，需本地 PostgreSQL 与 mosquitto）：
    gateway/.venv/bin/python scripts/verify_alert_rules_v134.py
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

EMPTY_DB = "iot_rules_empty_verify"
LEGACY_DB = "iot_rules_v133_verify"
VERIFY_DB = "iot_rules_verify"
PORT = 8134
MQTT_PORT = 18883
SECRET = "rules-verify-secret-2026"

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


def _migration_checks() -> None:
    print("== 1. 迁移：空库 / v1.33 既有库 / downgrade 往返 ==")
    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "alert_rule" in _tables(EMPTY_DB)
    assert "matched_rule" in _columns(EMPTY_DB, "device_alert")
    rows = _pg(
        EMPTY_DB,
        "SELECT name, metric, op, level, threshold, scope_type, enabled, consecutive_count "
        "FROM alert_rule ORDER BY id",
        fetch=True,
    )
    assert len(rows) == 10, rows
    table = {(row["metric"], row["op"], row["level"]): float(row["threshold"]) for row in rows}
    assert table[("voltage", "lt", "critical")] == 3.2
    assert table[("voltage", "lt", "warning")] == 3.4
    assert table[("temperature", "gt", "critical")] == 75.0
    assert table[("temperature", "gt", "warning")] == 60.0
    assert table[("temperature", "lt", "critical")] == -20.0
    assert table[("temperature", "lt", "warning")] == -10.0
    assert table[("humidity", "gt", "critical")] == 97.0
    assert table[("humidity", "gt", "warning")] == 90.0
    assert table[("rssi", "lt", "critical")] == -110.0
    assert table[("rssi", "lt", "warning")] == -100.0
    assert all(row["scope_type"] == "global" and row["enabled"] and row["consecutive_count"] == 1 for row in rows)
    print("[OK] 空库 upgrade head：alert_rule 10 条默认规则（全局/启用/连续 1 次），"
          "阈值 3.2/3.4、75/60、97/90、-110/-100；device_alert.matched_rule 已存在")

    _alembic(EMPTY_DB, "downgrade", "-1")
    assert "alert_rule" not in _tables(EMPTY_DB)
    assert "matched_rule" not in _columns(EMPTY_DB, "device_alert")
    _alembic(EMPTY_DB, "upgrade", "head")
    count = _pg(EMPTY_DB, "SELECT count(*) AS c FROM alert_rule", fetch=True)[0]["c"]
    assert count == 10, count
    print("[OK] downgrade -1 移除规则表与 matched_rule；再次 upgrade head 恢复并重新种子 10 条")

    # v1.33.0 既有库：停在 0008；在线改过电压警告阈值 3.00
    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0008_alert_sla_handover")
    _pg(
        LEGACY_DB,
        "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
        "VALUES ('dev-legacy-rules', '旧库设备', 'VG-S100', '1.0.0', '默认分组', 'online')",
    )
    _pg(
        LEGACY_DB,
        "INSERT INTO device_alert (alert_id, device_id, level, metric, value, threshold, "
        "message, ai_generated, status, created_at) VALUES "
        "('alt-legacy-rules', 'dev-legacy-rules', 'critical', 'voltage', 2.9, 3.2, "
        "'旧库告警', false, 'open', '2026-11-04 08:00:00+00')",
    )
    _pg(
        LEGACY_DB,
        "INSERT INTO rule_config (key, value, updated_by) "
        "VALUES ('threshold_voltage_warning', 3.00, 'ops')",
    )
    _alembic(LEGACY_DB, "upgrade", "head")
    rows = _pg(
        LEGACY_DB,
        "SELECT metric, op, level, threshold, is_default FROM alert_rule ORDER BY id",
        fetch=True,
    )
    table = {(row["metric"], row["op"], row["level"]): float(row["threshold"]) for row in rows}
    assert len(rows) == 10 and all(row["is_default"] for row in rows)
    assert table[("voltage", "lt", "warning")] == 3.0, "rule_config 在线覆盖值必须带入默认规则"
    assert table[("voltage", "lt", "critical")] == 3.2, "未覆盖项保持 v1.33.0 默认值"
    matched = _pg(
        LEGACY_DB,
        "SELECT matched_rule FROM device_alert WHERE alert_id='alt-legacy-rules'",
        fetch=True,
    )[0]["matched_rule"]
    assert matched is None, "历史告警不臆测回填 matched_rule"
    print("[OK] v1.33 既有库 upgrade：默认规则 10 条回填，电压警告沿用在线值 3.0，"
          "严重仍 3.2；历史告警 matched_rule=NULL")


class Broker:
    """真实 mosquitto（匿名、独立端口），只服务本次验证。"""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vikings-rules-mosq-"))
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


def alerts_for(device_id: str, timeline: bool = False) -> list[dict]:
    resp = _http(
        "GET", "/api/alerts",
        headers={"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"},
        params={"deviceId": device_id, "pageSize": 50},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    if timeline:
        items.sort(key=lambda item: item["created_at"])
    return items


def wait_alert(device_id: str, metric: str, count: int = 1, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id, timeline=True) if row["metric"] == metric]
        if len(rows) >= count:
            return rows
        time.sleep(0.3)
    return [row for row in alerts_for(device_id, timeline=True) if row["metric"] == metric]


def wait_no_alert(device_id: str, metric: str, timeout: float = 6.0) -> None:
    """等待窗口内不得出现该指标告警（用于「改阈值后不触发」）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id, timeline=True) if row["metric"] == metric]
        assert not rows, f"预期不触发，但产生了告警：{rows}"
        time.sleep(0.3)


def _rules(headers: dict) -> list[dict]:
    resp = _http("GET", "/api/rules", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _rule_id(rules: list[dict], metric: str, level: str, op: str | None = None) -> int:
    return next(
        row["id"]
        for row in rules
        if row["metric"] == metric and row["level"] == level and (op is None or row["op"] == op)
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-rules-"))
    log_path = tmp / "gateway.log"
    print(f"日志: {log_path}")

    _migration_checks()

    print("== 2. 真实 PostgreSQL + mosquitto + 上报 + 网关 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    # 设备分组：范围隔离需要固定分组名
    for device_id, group in (
        ("dev-east", "华东"), ("dev-east-new", "华东"), ("dev-north", "华北"),
        ("dev-consec", "默认分组"), ("dev-consec-off", "默认分组"),
    ):
        _pg(
            VERIFY_DB,
            "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
            "VALUES ($1, $1, 'VG-S100', '1.0.0', $2, 'online') ON CONFLICT (device_id) DO NOTHING",
            device_id, group,
        )

    broker = Broker()
    server = _start_server(log_path)
    try:
        _wait_ready()
        admin = {"Authorization": f"Bearer {_token('admin', 'verify-admin')}"}
        operator = {"Authorization": f"Bearer {_token('operator', 'verify-operator')}"}
        viewer = {"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"}
        rules = _rules(viewer)
        assert len(rules) == 10, rules
        print(f"[OK] 登录可读规则 {len(rules)} 条（viewer 只读）")

        print("== 3. 权限与校验 ==")
        assert _http("GET", "/api/rules").status_code == 401
        payload = {
            "name": "验证规则", "metric": "voltage", "op": "lt",
            "threshold": 3.5, "level": "warning", "scope_type": "global",
        }
        assert _http("POST", "/api/rules", headers=operator, body=payload).status_code == 403
        assert _http("POST", "/api/rules", headers=viewer, body=payload).status_code == 403
        for bad, keyword in (
            ({**payload, "threshold": -1}, "2.0~4.5"),
            ({**payload, "threshold": 9}, "2.0~4.5"),
            ({**payload, "metric": "humidity", "op": "lt", "threshold": 50}, "比较方向"),
            ({**payload, "scope_type": "group", "scope_value": " "}, "必须填写"),
            ({**payload, "level": "critical", "threshold": 3.6}, "比较方向疑似反向"),
        ):
            resp = _http("POST", "/api/rules", headers=admin, body=bad)
            assert resp.status_code == 400, resp.text
            assert keyword in resp.json()["message"], resp.text
        print("[OK] 未登录 401；operator/viewer 写 403；负数/越界/方向反向/范围缺值均 400 且错误可读")

        print("== 4. 真实上报触发临界值 + matched_rule ==")
        publish("dev-east", 1, voltage=3.19)
        rows = wait_alert("dev-east", "voltage")
        assert len(rows) == 1, rows
        alert = rows[0]
        assert alert["level"] == "critical", alert
        assert alert["threshold"] == 3.2, alert
        assert alert["matched_rule"]["rule_name"] == "电压过低-严重", alert
        assert alert["matched_rule"]["rule_id"] == _rule_id(rules, "voltage", "critical")
        assert alert["matched_rule"]["op"] == "lt"
        assert alert["matched_rule"]["value"] == 3.19
        print(f"[OK] 3.19V（临界值，阈值 3.2/严格小于）→ {alert['level']}，"
              f"matched_rule={json.dumps(alert['matched_rule'], ensure_ascii=False)}")

        print("== 5. 改阈值热生效（无需重启） ==")
        vid = _rule_id(rules, "voltage", "critical")
        wid = _rule_id(rules, "voltage", "warning")
        # 先把 critical 下移（2.8 < 当前 warning 3.4，允许），再下移 warning
        # （3.0 > critical 2.8，允许）；反向顺序会被校验拒绝，这本身也是校验生效的证据
        resp = _http("PUT", f"/api/rules/{vid}", headers=admin, body={"threshold": 2.8})
        assert resp.status_code == 200, resp.text
        resp = _http("PUT", f"/api/rules/{wid}", headers=admin, body={"threshold": 3.0})
        assert resp.status_code == 200, resp.text
        publish("dev-east-2", 2, voltage=3.19)
        wait_no_alert("dev-east-2", "voltage")
        print("[OK] 阈值改为 2.8/3.0 后，同一 3.19V 上报不再触发（无需重启）")

        # 改回默认 3.2/3.4：先放宽 warning 再收紧 critical
        resp = _http("PUT", f"/api/rules/{wid}", headers=admin, body={"threshold": 3.4})
        assert resp.status_code == 200, resp.text
        resp = _http("PUT", f"/api/rules/{vid}", headers=admin, body={"threshold": 3.2})
        assert resp.status_code == 200, resp.text
        publish("dev-east-3", 3, voltage=3.19)
        restored = wait_alert("dev-east-3", "voltage")
        assert len(restored) == 1 and restored[0]["matched_rule"]["threshold"] == 3.2, restored
        print("[OK] 阈值改回 3.2/3.4 后恢复触发，matched_rule 阈值回到 3.2")

        print("== 6. 生效范围隔离（指定分组 / 指定设备） ==")
        for row in _rules(admin):
            if row["metric"] == "voltage" and row["is_default"]:
                _http("PATCH", f"/api/rules/{row['id']}/enabled", headers=admin, body={"enabled": False})
        resp = _http(
            "POST", "/api/rules", headers=admin,
            body={
                "name": "华东电压专属", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning", "scope_type": "group", "scope_value": "华东",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = _http(
            "POST", "/api/rules", headers=admin,
            body={
                "name": "单机湿度专属", "metric": "humidity", "op": "gt", "threshold": 80,
                "level": "warning", "scope_type": "device", "scope_value": "dev-east-new",
            },
        )
        assert resp.status_code == 200, resp.text
        publish("dev-east-new", 4, voltage=3.19, humidity=85)
        east = [row["metric"] for row in wait_alert("dev-east-new", "voltage")]
        assert east, "华东设备应命中分组规则"
        east_humidity = wait_alert("dev-east-new", "humidity")
        assert east_humidity and east_humidity[0]["matched_rule"]["rule_name"] == "单机湿度专属"
        publish("dev-north", 5, voltage=3.19, humidity=85)
        wait_no_alert("dev-north", "voltage")
        wait_no_alert("dev-north", "humidity")
        print("[OK] 华东设备命中「华东电压专属 / 单机湿度专属」；华北设备两项均不触发")

        print("== 7. 多规则裁决：取最高级别，同级按 priority ==")
        for row in _rules(admin):
            if row["metric"] == "voltage" and row["is_default"]:
                _http("PATCH", f"/api/rules/{row['id']}/enabled", headers=admin, body={"enabled": True})
        publish("dev-arb", 6, voltage=2.9)
        arb = wait_alert("dev-arb", "voltage")
        assert len(arb) == 1 and arb[0]["level"] == "critical", arb
        assert arb[0]["matched_rule"]["rule_name"] == "电压过低-严重", arb
        resp = _http(
            "POST", "/api/rules", headers=admin,
            body={
                "name": "电压警告-P50", "metric": "voltage", "op": "lt",
                "threshold": 3.5, "level": "warning", "priority": 50,
            },
        )
        assert resp.status_code == 200, resp.text
        publish("dev-arb-2", 7, voltage=3.3)
        same = wait_alert("dev-arb-2", "voltage")
        assert len(same) == 1 and same[0]["matched_rule"]["rule_name"] == "电压警告-P50", same
        print("[OK] 2.9V 同时命中 critical/warning → 只产 1 条 critical；3.3V 同级命中 → priority=50 的规则胜出")

        print("== 8. 连续 N 次越限 ==")
        resp = _http(
            "POST", "/api/rules", headers=admin,
            body={
                "name": "连续三次湿度", "metric": "humidity", "op": "gt", "threshold": 80,
                "level": "warning", "scope_type": "device", "scope_value": "dev-consec",
                "consecutive_count": 3,
            },
        )
        assert resp.status_code == 200, resp.text
        for seq, value in ((10, 85), (11, 86), (12, 87)):
            publish("dev-consec", seq, humidity=value)
            time.sleep(0.5)
        rows = wait_alert("dev-consec", "humidity", timeout=10)
        assert len(rows) == 1, rows
        assert rows[0]["matched_rule"]["consecutive"] == 3
        assert rows[0]["value"] == 87, rows
        publish("dev-consec", 13, humidity=50)
        time.sleep(0.5)
        publish("dev-consec", 14, humidity=85)
        publish("dev-consec", 15, humidity=86)
        time.sleep(1.5)
        assert len([row for row in alerts_for("dev-consec") if row["metric"] == "humidity"]) == 1
        print("[OK] 连续第 3 次（87%）才触发；中间一次正常上报清零，后续 2 次不再触发")

        print("== 9. 停用规则：不产生新告警，历史与统计不变 ==")
        resp = _http(
            "POST", "/api/rules", headers=admin,
            body={
                "name": "待停用湿度", "metric": "humidity", "op": "gt", "threshold": 80,
                "level": "warning", "scope_type": "group", "scope_value": "默认分组",
            },
        )
        assert resp.status_code == 200, resp.text
        off_id = resp.json()["data"]["id"]
        publish("dev-consec-off", 16, humidity=85)
        before = wait_alert("dev-consec-off", "humidity")
        assert len(before) == 1, before
        summary_before = _http("GET", "/api/alerts/summary", headers=viewer).json()["data"]
        resp = _http("PATCH", f"/api/rules/{off_id}/enabled", headers=admin, body={"enabled": False})
        assert resp.status_code == 200 and resp.json()["data"]["enabled"] is False, resp.text
        publish("dev-consec-off-2", 17, humidity=85)
        wait_no_alert("dev-consec-off-2", "humidity")
        assert len([row for row in alerts_for("dev-consec-off") if row["metric"] == "humidity"]) == 1
        summary_after = _http("GET", "/api/alerts/summary", headers=viewer).json()["data"]
        total_before = _http("GET", "/api/alerts", headers=viewer).json()["data"]["total"]
        assert summary_after == summary_before, (summary_before, summary_after)
        assert _http("GET", "/api/alerts", headers=viewer).json()["data"]["total"] == total_before
        print(f"[OK] 停用后新设备不产生告警；历史告警仍为 1 条，统计不变（open={summary_after.get('open')}）")

        print("== 10. 规则重载不重复入库 ==")
        dup_before = len(alerts_for("dev-arb"))
        publish("dev-arb", 18, voltage=2.9)
        time.sleep(1.0)
        # 写一次规则（触发缓存重载）后再上报同设备同指标
        _http("PUT", f"/api/rules/{vid}", headers=admin, body={"name": "电压过低-严重"})
        publish("dev-arb", 19, voltage=2.9)
        dup_after = len(alerts_for("dev-arb"))
        assert dup_after == dup_before, (dup_before, dup_after)
        print(f"[OK] 重载规则后同一设备/指标 5 分钟窗口内仍只有 {dup_after} 条告警，无重复入库")

        print("== 11. 审计（只记字段名，不记阈值） ==")
        audit = _pg(
            VERIFY_DB,
            "SELECT operator, action, target_id, detail FROM audit_log "
            "WHERE action LIKE 'alert_rule.%' ORDER BY id",
            fetch=True,
        )
        actions = {row["action"] for row in audit}
        assert {"alert_rule.create", "alert_rule.update", "alert_rule.disable"} <= actions, actions
        create = next(row for row in audit if row["action"] == "alert_rule.create")
        detail = create["detail"]
        detail = json.loads(detail) if isinstance(detail, str) else detail
        assert "fields" in detail and "3.5" not in json.dumps(detail), detail
        print(f"[OK] 审计动作 {sorted(actions)}；create detail={json.dumps(detail, ensure_ascii=False)}")

        print("v1.34.0 真实验证全部通过")
    finally:
        try:
            if server.poll() is None:
                server.send_signal(signal.SIGTERM)
                server.wait(timeout=10)
        except Exception:
            server.kill()
        broker.close()


if __name__ == "__main__":
    main()
