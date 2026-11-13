#!/usr/bin/env python3
"""v1.39.0 维护窗口真实验证（不 mock）。

真实 PostgreSQL + 真实 Alembic 迁移 + 真实 mosquitto + 真实 MQTT 上报 + 真实网关。
覆盖：空库/既有库升级与 downgrade 往返、窗口内抑制（不入告警/通知/SLA）、
范围外设备正常告警、结束后汇总（含 critical 压制）、提前结束恢复、周期下次触发。

用法（仓库根目录，需本地 PostgreSQL 与 mosquitto）：
    gateway/.venv/bin/python scripts/verify_maintenance_v139.py
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

EMPTY_DB = "iot_mw_empty_verify"
LEGACY_DB = "iot_mw_v138_verify"
VERIFY_DB = "iot_mw_verify"
PORT = 8137
MQTT_PORT = 18886
SECRET = "maintenance-verify-secret-2026"

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
    print("== 1. 迁移：空库 / v1.38 既有库 / downgrade 往返 ==")

    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    tables = _tables(EMPTY_DB)
    assert {"maintenance_window", "maintenance_suppression", "maintenance_summary"} <= tables, tables
    dtype = _pg(
        EMPTY_DB,
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='maintenance_window' AND column_name='scope'",
        fetch=True,
    )[0]["data_type"]
    assert dtype == "jsonb", dtype
    rules = _pg(EMPTY_DB, "SELECT count(*) AS c FROM alert_rule", fetch=True)[0]["c"]
    assert rules == 10, rules
    print(f"[OK] 空库 upgrade head：维护窗口三表存在，scope={dtype}，默认规则仍 {rules} 条")

    _alembic(EMPTY_DB, "downgrade", "-1")
    assert "maintenance_window" not in _tables(EMPTY_DB)
    assert "maintenance_suppression" not in _tables(EMPTY_DB)
    assert "maintenance_summary" not in _tables(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "maintenance_window" in _tables(EMPTY_DB)
    print("[OK] downgrade -1 删除维护窗口三表；再次 upgrade head 恢复")

    # v1.38 既有库（停在 0013）：升级不得改动既有规则/告警
    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0013_rule_conditions")
    _pg(
        LEGACY_DB,
        "INSERT INTO alert_rule (name, metric, op, threshold, level, scope_type, scope_value, "
        "enabled, consecutive_count, priority, is_default, updated_by) "
        "VALUES ('既有规则', 'voltage', 'lt', 3.5, 'warning', 'group', '华东', "
        "true, 1, 100, false, 'ops')",
    )
    assert "maintenance_window" not in _tables(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "head")
    row = _pg(
        LEGACY_DB,
        "SELECT name, threshold, scope_value FROM alert_rule WHERE name='既有规则'",
        fetch=True,
    )[0]
    assert row["threshold"] == __import__("decimal").Decimal("3.5"), row
    assert row["scope_value"] == "华东", row
    assert "maintenance_window" in _tables(LEGACY_DB)
    print("[OK] v1.38 既有库 upgrade：既有规则阈值 3.5 / scope_value='华东' 原样保留，新增维护窗口表")

    _alembic(LEGACY_DB, "downgrade", "-1")
    assert "maintenance_window" not in _tables(LEGACY_DB)
    row = _pg(
        LEGACY_DB,
        "SELECT threshold, scope_value FROM alert_rule WHERE name='既有规则'",
        fetch=True,
    )[0]
    assert row["scope_value"] == "华东", row
    assert row["threshold"] == __import__("decimal").Decimal("3.5"), row
    print("[OK] downgrade 到 0013：维护窗口表移除，既有规则与条件树字段不受影响")


class Broker:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vikings-mw-mosq-"))
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
            "MAINTENANCE_SWEEP_INTERVAL_SECONDS": "5",
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
            if httpx.get(f"http://127.0.0.1:{PORT}/api/health/ready", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("网关未在限定时间内就绪")


def publish(device_id: str, seq: int, **payload) -> None:
    import paho.mqtt.publish as mqtt_publish

    body = {
        "deviceId": device_id,
        "temperature": 25.0,
        "humidity": 55.0,
        "voltage": 3.75,
        "rssi": -60,
        "battery": 90,
        "seq": seq,
        "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    body.update(payload)
    mqtt_publish.single(
        f"device/{device_id}/sensor/report",
        json.dumps(body, ensure_ascii=False),
        qos=1,
        hostname="127.0.0.1",
        port=MQTT_PORT,
    )


def alerts_for(device_id: str) -> list[dict]:
    rows = _http(
        "GET", "/api/alerts", headers=_headers("admin"), params={"deviceId": device_id, "pageSize": 50}
    )
    assert rows.status_code == 200, rows.text
    return rows.json()["data"]["items"]


def _headers(role: str, username: str | None = None) -> dict:
    return {"Authorization": f"Bearer {_token(role, username or role)}"}


def wait_alert(device_id: str, metric: str, *, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        if rows:
            return rows[0]
        time.sleep(0.3)
    raise AssertionError(f"{device_id} 在 {timeout}s 内未产生 {metric} 告警")


def wait_no_alert(device_id: str, metric: str, *, timeout: float = 6.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        if rows:
            raise AssertionError(f"{device_id} 不应产生 {metric} 告警：{rows[0]}")
        time.sleep(0.3)


def _seed() -> None:
    _pg(
        VERIFY_DB,
        "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) VALUES "
        "('dev-cold', '冷库设备', 'VG-S100', '1.0.0', '华东', 'online'), "
        "('dev-north', '华北冷库', 'VG-S100', '1.0.0', '华北', 'online'), "
        "('dev-out', '范围外设备', 'VG-S100', '1.0.0', '华南', 'online') "
        "ON CONFLICT (device_id) DO NOTHING",
    )
    _pg(
        VERIFY_DB,
        "INSERT INTO device_tag (device_id, tag) VALUES ('dev-cold','冷库'), ('dev-north','冷库') "
        "ON CONFLICT DO NOTHING",
    )


def _alert_summary(headers: dict) -> dict:
    resp = _http("GET", "/api/alerts/summary", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def main() -> None:
    _migration_checks()

    print("== 2. 真实环境：PostgreSQL + mosquitto + 网关 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    _seed()
    broker = Broker()
    log_path = Path(tempfile.mkdtemp(prefix="vikings-mw-")) / "gateway.log"
    server = _start_server(log_path)
    admin = _headers("admin")
    operator = _headers("operator")
    viewer = _headers("viewer")
    try:
        _wait_ready()
        print(f"[OK] 网关就绪（{PORT}），Broker {MQTT_PORT}，设备 3 台已入库")

        now = datetime.now(timezone.utc)
        future = {
            "name": "未来窗口（用于窗口外对照）",
            "scope": {"op": "any", "children": [{"dim": "tag", "values": ["冷库"]}]},
            "mode": "once",
            "starts_at": (now + timedelta(minutes=30)).isoformat(),
            "ends_at": (now + timedelta(minutes=60)).isoformat(),
        }
        resp = _http("POST", "/api/maintenance/windows", headers=admin, body=future)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["active"] is False

        print("== 3. 同一设备同指标：窗口外正常告警 ==")
        publish("dev-cold", 101, voltage=3.0)
        outside = wait_alert("dev-cold", "voltage")
        assert outside["level"] == "critical", outside
        rule_name = (outside.get("matched_rule") or {}).get("rule_name", "-")
        print(f"[OK] 窗口外 dev-cold voltage=3.0 → 告警 {outside['level']}（rule={rule_name}）")

        _pg(VERIFY_DB, "DELETE FROM device_alert WHERE device_id=$1", "dev-cold")

        print("== 4. 权限与校验 ==")
        body = {
            "name": "冷库维护",
            "reason": "冷库机房割接",
            "scope": {"op": "any", "children": [{"dim": "tag", "values": ["冷库"]}]},
            "mode": "once",
            "starts_at": (now - timedelta(minutes=5)).isoformat(),
            "ends_at": (now + timedelta(minutes=40)).isoformat(),
        }
        assert _http("POST", "/api/maintenance/windows", body=body).status_code == 401
        assert _http("POST", "/api/maintenance/windows", headers=operator, body=body).status_code == 403
        assert _http("POST", "/api/maintenance/windows", headers=viewer, body=body).status_code == 403
        bad = dict(body, ends_at=(now - timedelta(minutes=10)).isoformat())
        bad_resp = _http("POST", "/api/maintenance/windows", headers=admin, body=bad)
        assert bad_resp.status_code == 400, bad_resp.text
        assert "结束时间" in bad_resp.json()["message"]
        print("[OK] 未登录 401；operator/viewer 403；结束早于开始 400 且原因可读")

        created = _http("POST", "/api/maintenance/windows", headers=admin, body=body)
        assert created.status_code == 200, created.text
        window = created.json()["data"]
        window_id = window["window_id"]
        assert window["active"] is True
        print(f"[OK] 活动窗口 {window_id} 创建成功：active=True，范围={window['scope']}")

        print("== 5. 窗口内抑制（范围命中） ==")
        publish("dev-cold", 102, voltage=3.0)
        wait_no_alert("dev-cold", "voltage")
        suppressed = _pg(
            VERIFY_DB,
            "SELECT device_id, metric, level, value, threshold, rule_name "
            "FROM maintenance_suppression WHERE window_id=$1",
            window_id, fetch=True,
        )
        assert len(suppressed) == 1, suppressed
        assert suppressed[0]["level"] == "critical", suppressed
        assert suppressed[0]["metric"] == "voltage", suppressed
        device_alerts = _pg(
            VERIFY_DB,
            "SELECT count(*) AS c FROM device_alert WHERE device_id='dev-cold'",
            fetch=True,
        )[0]["c"]
        assert device_alerts == 0, "抑制必须发生在入库之前"
        sla_rows = _pg(
            VERIFY_DB,
            "SELECT count(*) AS c FROM device_alert WHERE sla_deadline_at IS NOT NULL",
            fetch=True,
        )[0]["c"]
        assert sla_rows == 0, "抑制项不得进入 SLA"
        print(f"[OK] 窗口内 dev-cold voltage=3.0 → 无告警；抑制记录 1 条（{suppressed[0]['level']}），device_alert=0，SLA=0")

        print("== 6. 范围外设备在窗口内仍正常告警 ==")
        publish("dev-out", 103, voltage=3.0)
        out_alert = wait_alert("dev-out", "voltage")
        assert out_alert["level"] == "critical", out_alert
        print(f"[OK] 范围外 dev-out（无冷库标签）→ 正常告警 {out_alert['level']}")

        print("== 7. 抑制项不计入列表 / 概览 / SLA ==")
        summary = _alert_summary(admin)
        total = _pg(VERIFY_DB, "SELECT count(*) AS c FROM device_alert", fetch=True)[0]["c"]
        assert total == 1, total
        assert summary["open"] == 1 and summary["today"] == 1, summary
        print(f"[OK] device_alert 总数={total}，告警概览 open={summary['open']} today={summary['today']}；"
              f"被抑制的 1 条未进入任何统计")

        print("== 8. 提前结束 → 汇总 + 立即恢复告警 ==")
        ended = _http("POST", f"/api/maintenance/windows/{window_id}/end", headers=admin)
        assert ended.status_code == 200, ended.text
        assert ended.json()["data"]["status"] == "ended"
        summaries = _http(
            "GET", f"/api/maintenance/windows/{window_id}/summaries", headers=viewer
        ).json()["data"]
        assert len(summaries) == 1, summaries
        item = summaries[0]
        assert item["suppressed_count"] == 1, item
        assert item["critical_suppressed"] == 1, item
        assert item["device_count"] == 1 and item["devices"] == ["dev-cold"], item
        assert item["metrics"] == {"voltage": 1}, item
        assert item["ended_early"] is True, item
        print(f"[OK] 汇总：suppressed={item['suppressed_count']} critical={item['critical_suppressed']} "
              f"devices={item['devices']} metrics={item['metrics']} ended_early={item['ended_early']}")

        publish("dev-cold", 104, voltage=3.0)
        resumed = wait_alert("dev-cold", "voltage")
        assert resumed["level"] == "critical", resumed
        print(f"[OK] 提前结束后 dev-cold 立即恢复告警：{resumed['level']}")

        print("== 9. 周期窗口的下一次触发 ==")
        target_weekday = (datetime.now(timezone.utc).weekday() + 2) % 7
        weekly = {
            "name": "每周运维窗口",
            "mode": "weekly",
            "recurrence": {"weekday": target_weekday, "start": "18:00", "duration_minutes": 240},
            "scope": None,
        }
        resp = _http("POST", "/api/maintenance/windows", headers=admin, body=weekly)
        assert resp.status_code == 200, resp.text
        weekly_out = resp.json()["data"]
        assert weekly_out["next_start"], weekly_out
        moment = datetime.now(timezone.utc)
        days_ahead = (target_weekday - moment.weekday()) % 7
        candidate = (moment + timedelta(days=days_ahead)).replace(
            hour=18, minute=0, second=0, microsecond=0
        )
        if candidate <= moment:
            candidate += timedelta(days=7)
        assert datetime.fromisoformat(weekly_out["next_start"]).astimezone(timezone.utc) == candidate, (
            weekly_out["next_start"], candidate
        )
        print(f"[OK] 每周窗口 next_start={weekly_out['next_start']}（UTC，跨周计算正确）")

        sys.path.insert(0, str(ROOT / "gateway"))
        from app import maintenance_window as maintenance
        from types import SimpleNamespace

        cross = SimpleNamespace(
            mode="weekly", status="scheduled", starts_at=None, ends_at=None,
            recurrence={"weekday": 5, "start": "22:00", "duration_minutes": 240},
        )
        occurrence = maintenance.occurrence_for(
            cross, datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)
        )
        assert occurrence is not None
        assert occurrence.start == datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)
        assert occurrence.end == datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
        print("[OK] 跨天：周六 22:00 起 4 小时，周日 01:00 仍命中，02:00 结束")

        print("== 10. 抑制记录接口 ==")
        record_resp = _http(
            "GET", f"/api/maintenance/windows/{window_id}/suppressions", headers=viewer
        )
        assert record_resp.status_code == 200, record_resp.text
        items = record_resp.json()["data"]["items"]
        assert items and items[0]["device_id"] == "dev-cold", items
        print(f"[OK] 抑制记录可查：{items[0]['device_id']} {items[0]['metric']} {items[0]['level']}")

        print("v1.39.0 维护窗口真实验证全部通过")
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=15)
        except Exception:
            server.kill()
        broker.close()


if __name__ == "__main__":
    main()
