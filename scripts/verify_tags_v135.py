#!/usr/bin/env python3
"""v1.35.0 设备标签 + 规则多值范围 + 规则历史真实验证。

不 mock：真实 PostgreSQL + 真实 Alembic 迁移 + 真实 mosquitto + 真实 MQTT 上报
+ 真实 uvicorn 网关 + 真实 HTTP API。覆盖：
1. 迁移三场景：空库 upgrade、v1.34 既有库（0009）upgrade 回填、downgrade 往返；
2. 权限：未登录 401、operator/viewer 打标签 403、admin 可写；
3. 校验：空白 / 超长 / 非法字符标签返回 400 可读原因；
4. 批量打标签：逐设备 updated / unchanged / not_found / over_limit，部分失败可见；
5. 设备列表标签筛选（或语义）与分组「且」组合，不传标签总数不变；
6. 规则多值范围：标签 OR、分组 OR、设备 OR；非命中设备不触发；
7. 旧单值规则 + scope_values=NULL 回退 scope_value 的行为不变；
8. 告警列表标签筛选、两类 CSV 导出按标签的真实行数；
9. 规则变更历史来自审计，阈值前后值正确且不含敏感字段；
10. v1.34 默认阈值行为在升级后不变。

用法（仓库根目录，需本地 PostgreSQL 与 mosquitto）：
    gateway/.venv/bin/python scripts/verify_tags_v135.py
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

EMPTY_DB = "iot_tags_empty_verify"
LEGACY_DB = "iot_tags_v134_verify"
VERIFY_DB = "iot_tags_verify"
PORT = 8135
MQTT_PORT = 18884
SECRET = "tags-verify-secret-2026"

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


def _migration_checks() -> None:
    print("== 1. 迁移：空库 / v1.34 既有库 / downgrade 往返 ==")

    _create_database(EMPTY_DB)
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "device_tag" in _tables(EMPTY_DB)
    assert "scope_values" in _columns(EMPTY_DB, "alert_rule")
    dtype = _pg(
        EMPTY_DB,
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='alert_rule' AND column_name='scope_values'",
        fetch=True,
    )[0]["data_type"]
    assert dtype == "jsonb", dtype
    indexes = {
        row["indexname"]
        for row in _pg(
            EMPTY_DB,
            "SELECT indexname FROM pg_indexes WHERE tablename='device_tag'",
            fetch=True,
        )
    }
    assert "idx_device_tag_tag" in indexes, indexes
    rules = _pg(
        EMPTY_DB,
        "SELECT scope_type, scope_value, scope_values FROM alert_rule ORDER BY id",
        fetch=True,
    )
    assert len(rules) == 10, rules
    assert all(row["scope_type"] == "global" and row["scope_values"] is None for row in rules)
    print(f"[OK] 空库 upgrade head：device_tag 建表 + idx_device_tag_tag；"
          f"alert_rule.scope_values={dtype}；10 条默认规则 scope_values=NULL")

    _alembic(EMPTY_DB, "downgrade", "-1")
    assert "device_tag" not in _tables(EMPTY_DB)
    assert "scope_values" not in _columns(EMPTY_DB, "alert_rule")
    _alembic(EMPTY_DB, "upgrade", "head")
    assert "device_tag" in _tables(EMPTY_DB)
    count = _pg(EMPTY_DB, "SELECT count(*) AS c FROM alert_rule", fetch=True)[0]["c"]
    assert count == 10, count
    print("[OK] downgrade -1 删除 device_tag 与 scope_values；再次 upgrade head 恢复，默认规则仍 10 条")

    # v1.34.0 既有库：停在 0009，已有一条分组单值规则
    _create_database(LEGACY_DB)
    _alembic(LEGACY_DB, "upgrade", "0009_alert_rules")
    _pg(
        LEGACY_DB,
        "INSERT INTO alert_rule (name, metric, op, threshold, level, scope_type, "
        "scope_value, enabled, consecutive_count, priority, is_default, updated_by) "
        "VALUES ('旧单值规则', 'voltage', 'lt', 3.5, 'warning', 'group', '华东', "
        "true, 1, 100, false, 'ops')",
    )
    _alembic(LEGACY_DB, "upgrade", "head")
    row = _pg(
        LEGACY_DB,
        "SELECT name, threshold, scope_type, scope_value, scope_values "
        "FROM alert_rule WHERE name='旧单值规则'",
        fetch=True,
    )[0]
    assert row["threshold"] == __import__("decimal").Decimal("3.5"), row
    assert row["scope_type"] == "group" and row["scope_value"] == "华东", row
    assert _json(row["scope_values"]) == ["华东"], row
    print("[OK] v1.34 既有库 upgrade：旧单值规则阈值 3.5 不变，"
          "scope_value='华东' 回填为 scope_values=['华东']")

    _alembic(LEGACY_DB, "downgrade", "-1")
    assert "device_tag" not in _tables(LEGACY_DB)
    assert "scope_values" not in _columns(LEGACY_DB, "alert_rule")
    row = _pg(
        LEGACY_DB,
        "SELECT threshold, scope_value FROM alert_rule WHERE name='旧单值规则'",
        fetch=True,
    )[0]
    assert row["scope_value"] == "华东", row
    assert row["threshold"] == __import__("decimal").Decimal("3.5"), row
    _alembic(LEGACY_DB, "upgrade", "head")
    row = _pg(
        LEGACY_DB,
        "SELECT scope_values FROM alert_rule WHERE name='旧单值规则'",
        fetch=True,
    )[0]
    assert _json(row["scope_values"]) == ["华东"], row
    print("[OK] downgrade 到 0009 后 device_tag/scope_values 移除、scope_value 保留；"
          "再次 upgrade 重新回填 ['华东']")


class Broker:
    """真实 mosquitto（匿名、独立端口），只服务本次验证。"""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vikings-tags-mosq-"))
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


def alerts_for(device_id: str) -> list[dict]:
    resp = _http(
        "GET", "/api/alerts",
        headers={"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"},
        params={"deviceId": device_id, "pageSize": 50},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


def wait_alert(device_id: str, metric: str, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [row for row in alerts_for(device_id) if row["metric"] == metric]
        if rows:
            return rows
        time.sleep(0.3)
    return [row for row in alerts_for(device_id) if row["metric"] == metric]


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


def _add_tags(device_ids: list[str], add: list[str] | None = None,
              remove: list[str] | None = None) -> dict:
    resp = _http(
        "POST", "/api/devices/tags/batch",
        headers={"Authorization": f"Bearer {_token('admin', 'verify-admin')}"},
        body={"device_ids": device_ids, "add": add or [], "remove": remove or []},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _csv_rows(resp) -> list[str]:
    return [line for line in resp.text.lstrip("\ufeff").strip().splitlines() if line]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-tags-"))
    log_path = tmp / "gateway.log"
    print(f"日志: {log_path}")

    _migration_checks()

    print("== 2. 真实 PostgreSQL + mosquitto + 上报 + 网关 ==")
    _create_database(VERIFY_DB)
    _alembic(VERIFY_DB, "upgrade", "head")
    devices = (
        ("dev-cold", "冷库组"),
        ("dev-key", "关键组"),
        ("dev-normal", "普通组"),
        ("dev-south", "华南"),
        ("dev-east2", "华东"),
        ("dev-north2", "华北"),
        ("dev-single", "单值组"),
        ("dev-old", "旧规则组"),
        ("dev-over", "超限组"),
        ("dev-base", "默认分组"),
    )
    for device_id, group in devices:
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

        print("== 3. v1.34 默认阈值行为升级后不变 ==")
        baseline_total = _http("GET", "/api/devices", headers=viewer).json()["data"]["total"]
        assert baseline_total == len(devices), baseline_total
        publish("dev-base", 1, voltage=3.19)
        base = wait_alert("dev-base", "voltage")
        assert len(base) == 1 and base[0]["level"] == "critical", base
        assert base[0]["matched_rule"]["threshold"] == 3.2, base
        assert base[0]["matched_rule"]["rule_name"] == "电压过低-严重", base
        print(f"[OK] 默认规则：3.19V → critical（阈值 3.2，matched_rule={base[0]['matched_rule']['rule_name']}）；"
              f"无标签设备总数 {baseline_total} 与升级前一致")
        _disable_all(admin)

        print("== 4. 标签权限与校验 ==")
        assert _http("GET", "/api/devices/tags").status_code == 401
        assert _http("POST", "/api/devices/tags/batch", body={"device_ids": []}).status_code == 401
        resp = _http("POST", "/api/devices/tags/batch", headers=operator,
                     body={"device_ids": ["dev-cold"], "add": ["冷库"]})
        assert resp.status_code == 403, resp.text
        resp = _http("POST", "/api/devices/tags/batch", headers=viewer,
                     body={"device_ids": ["dev-cold"], "add": ["冷库"]})
        assert resp.status_code == 403, resp.text
        for bad_tag, keyword in (("   ", "标签"), ("x" * 33, "32"), ("bad!tag", "符号")):
            resp = _http("POST", "/api/devices/tags/batch", headers=admin,
                         body={"device_ids": ["dev-cold"], "add": [bad_tag]})
            assert resp.status_code == 400, resp.text
            assert keyword in resp.json()["message"], resp.text
        print("[OK] 未登录 401；operator/viewer 打标签 403；空白/超长/非法字符 400 且错误可读")

        print("== 5. 批量打标签：部分失败逐台可见 ==")
        # 先给三台设备打同一个批量标记，验证批量更新计数
        resp = _http("POST", "/api/devices/tags/batch", headers=admin,
                     body={"device_ids": ["dev-cold", "dev-key", "dev-normal"],
                           "add": ["批量标记"]})
        assert resp.status_code == 200, resp.text
        first = resp.json()["data"]
        assert first["updated"] == 3 and first["unchanged"] == 0, first
        # 再分别打上各自的业务标签
        _add_tags(["dev-cold"], ["冷库"])
        _add_tags(["dev-key"], ["关键"])
        _add_tags(["dev-normal"], ["普通"])
        _add_tags(["dev-south"], ["华南"])
        _add_tags(["dev-east2"], ["华东"])
        _add_tags(["dev-cold"], ["华东"])

        # 16 个标签后继续加 → over_limit；缺失设备 → not_found；重复标签 → unchanged
        sixteen = [f"overflow-{i:02d}" for i in range(1, 17)]
        _add_tags(["dev-over"], sixteen)
        # 先让 dev-key 已持有「批量B」，再对多台批量添加同一标签：
        # dev-normal 实际新增 → updated；dev-key 已有 → unchanged
        _add_tags(["dev-key"], ["批量B"])
        result = _add_tags(
            ["dev-normal", "dev-key", "dev-missing", "dev-over"],
            add=["批量B"],
            remove=["不存在的标签"],
        )
        statuses = {row["device_id"]: row["status"] for row in result["results"]}
        assert statuses["dev-normal"] == "updated", result
        assert statuses["dev-key"] == "unchanged", result
        assert statuses["dev-missing"] == "not_found", result
        assert statuses["dev-over"] == "over_limit", result
        assert result["updated"] == 1 and result["unchanged"] == 1, result
        assert result["not_found"] == 1 and result["over_limit"] == 1, result
        over = next(row for row in result["results"] if row["device_id"] == "dev-over")
        assert over["tag_count"] == 16 and "16" in (over["error"] or ""), over
        print(f"[OK] 批量结果 {json.dumps({k: result[k] for k in ('updated','unchanged','not_found','over_limit','added','removed')}, ensure_ascii=False)}；"
              f"dev-over 仍 16 个标签，error={over['error']}")

        tags = _http("GET", "/api/devices/tags", headers=viewer).json()["data"]
        table = {row["tag"]: row["device_count"] for row in tags}
        assert table["冷库"] == 1 and table["华东"] == 2 and table["关键"] == 1, table
        assert table["普通"] == 1 and table["批量标记"] == 3, table
        print(f"[OK] GET /api/devices/tags 设备数：{json.dumps(table, ensure_ascii=False, sort_keys=True)}")

        print("== 6. 设备列表标签筛选（或）与分组（且） ==")
        total_all = _http("GET", "/api/devices", headers=viewer).json()["data"]["total"]
        assert total_all == len(devices), total_all
        resp = _http("GET", "/api/devices", headers=viewer, params={"tag": ["华东"]})
        assert resp.json()["data"]["total"] == 2, resp.text
        resp = _http("GET", "/api/devices", headers=viewer, params={"tag": ["冷库", "普通"]})
        ids = {row["device_id"] for row in resp.json()["data"]["items"]}
        assert resp.json()["data"]["total"] == 2 and ids == {"dev-cold", "dev-normal"}, ids
        resp = _http("GET", "/api/devices", headers=viewer,
                     params={"tag": ["华东"], "group": "华东"})
        ids = {row["device_id"] for row in resp.json()["data"]["items"]}
        assert resp.json()["data"]["total"] == 1 and ids == {"dev-east2"}, ids
        print("[OK] tag=华东 → 2 台；tag=冷库|普通 → 2 台（或）；tag=华东&group=华东 → 仅 dev-east2（且）；"
              f"不传 tag 仍为 {total_all} 台")

        print("== 7. 规则多值范围：标签 OR / 分组 OR / 设备 OR ==")
        _disable_all(admin)
        tag_rule = _create_rule(
            admin, name="标签湿度过高", metric="humidity", op="gt", threshold=75,
            level="warning", scope_type="tag", scope_values=["冷库", "关键"],
        )
        assert tag_rule["scope_values"] == ["冷库", "关键"], tag_rule
        assert tag_rule["scope_value"] == "冷库", tag_rule
        publish("dev-cold", 2, humidity=80)
        publish("dev-key", 3, humidity=80)
        publish("dev-normal", 4, humidity=80)
        assert wait_alert("dev-cold", "humidity")[0]["matched_rule"]["rule_name"] == "标签湿度过高"
        assert wait_alert("dev-key", "humidity")[0]["matched_rule"]["rule_name"] == "标签湿度过高"
        wait_no_alert("dev-normal", "humidity")
        print("[OK] 标签规则 scope_values=['冷库','关键']：冷库/关键设备命中，普通设备不触发（组内或）")

        _disable_all(admin)
        group_rule = _create_rule(
            admin, name="华东华南电压", metric="voltage", op="lt", threshold=3.5,
            level="warning", scope_type="group", scope_values=["华东", "华南"],
        )
        assert group_rule["scope_values"] == ["华东", "华南"], group_rule
        publish("dev-east2", 5, voltage=3.3)
        publish("dev-south", 6, voltage=3.3)
        publish("dev-north2", 7, voltage=3.3)
        assert wait_alert("dev-east2", "voltage")[0]["matched_rule"]["rule_name"] == "华东华南电压"
        assert wait_alert("dev-south", "voltage")[0]["matched_rule"]["rule_name"] == "华东华南电压"
        wait_no_alert("dev-north2", "voltage")
        print("[OK] 分组规则 scope_values=['华东','华南']：华东/华南命中，华北不触发")

        _disable_all(admin)
        device_rule = _create_rule(
            admin, name="双机信号弱", metric="rssi", op="lt", threshold=-90,
            level="warning", scope_type="device", scope_values=["dev-cold", "dev-normal"],
        )
        assert device_rule["scope_values"] == ["dev-cold", "dev-normal"], device_rule
        publish("dev-cold", 8, rssi=-95)
        publish("dev-normal", 9, rssi=-95)
        publish("dev-key", 10, rssi=-95)
        assert wait_alert("dev-cold", "rssi")[0]["matched_rule"]["rule_name"] == "双机信号弱"
        assert wait_alert("dev-normal", "rssi")[0]["matched_rule"]["rule_name"] == "双机信号弱"
        wait_no_alert("dev-key", "rssi")
        print("[OK] 设备规则 scope_values=['dev-cold','dev-normal']：两台命中，dev-key 不触发")

        print("== 8. 旧单值规则与 scope_values=NULL 回退 ==")
        _disable_all(admin)
        legacy = _create_rule(
            admin, name="旧单值温度", metric="temperature", op="gt", threshold=40,
            level="warning", scope_type="group", scope_value="单值组",
        )
        assert legacy["scope_values"] == ["单值组"], legacy
        publish("dev-single", 11, temperature=50)
        assert wait_alert("dev-single", "temperature")[0]["matched_rule"]["rule_name"] == "旧单值温度"

        fallback = _create_rule(
            admin, name="回退温度", metric="temperature", op="gt", threshold=40,
            level="warning", scope_type="group", scope_value="旧规则组",
        )
        _pg(VERIFY_DB, "UPDATE alert_rule SET scope_values=NULL WHERE id=$1", fallback["id"])
        resp = _http("PUT", f"/api/rules/{fallback['id']}", headers=admin,
                     body={"name": "回退温度"})
        assert resp.status_code == 200, resp.text
        publish("dev-old", 12, temperature=50)
        assert wait_alert("dev-old", "temperature")[0]["matched_rule"]["rule_name"] == "回退温度"
        print("[OK] 只传 scope_value 的旧写法可用；scope_values=NULL 时回退 scope_value 仍命中"
              "（dev-old 命中，阈值与 v1.34 一致）")

        print("== 9. 告警标签筛选与导出行数 ==")
        resp = _http("GET", "/api/alerts", headers=viewer, params={"tag": ["冷库"], "pageSize": 50})
        items = resp.json()["data"]["items"]
        assert all(row["device_id"] == "dev-cold" for row in items), items
        assert len(items) == 2, items  # humidity + rssi
        resp = _http("GET", "/api/alerts", headers=viewer,
                     params={"tag": ["冷库", "关键"], "pageSize": 50})
        ids = {row["device_id"] for row in resp.json()["data"]["items"]}
        assert ids == {"dev-cold", "dev-key"}, ids
        resp = _http("GET", "/api/alerts", headers=viewer,
                     params={"tag": ["冷库"], "level": "critical", "pageSize": 50})
        assert resp.json()["data"]["total"] == 0, resp.text
        print(f"[OK] 告警 tag=冷库 → 2 条（humidity/rssi）；tag=冷库|关键 → dev-cold + dev-key；"
              f"tag=冷库&level=critical → 0 条")

        record_count = _pg(
            VERIFY_DB,
            "SELECT count(*) AS c FROM sensor_record WHERE device_id='dev-cold'",
            fetch=True,
        )[0]["c"]
        resp = _http("GET", "/api/exports/records", headers=operator,
                     params={"tag": ["冷库"]})
        assert resp.status_code == 200, resp.text
        rows = _csv_rows(resp)
        assert len(rows) - 1 == record_count, (len(rows), record_count)
        alert_count = _pg(
            VERIFY_DB,
            "SELECT count(*) AS c FROM device_alert WHERE device_id='dev-cold'",
            fetch=True,
        )[0]["c"]
        resp = _http("GET", "/api/exports/alerts", headers=operator,
                     params={"tag": ["冷库"]})
        assert resp.status_code == 200, resp.text
        rows = _csv_rows(resp)
        assert len(rows) - 1 == alert_count, (len(rows), alert_count)
        print(f"[OK] 导出 tag=冷库：records {record_count} 行、alerts {alert_count} 行，"
              f"与数据库按设备统计一致（CSV 含表头）")

        print("== 10. 规则变更历史来自审计 ==")
        assert _http("GET", f"/api/rules/{tag_rule['id']}/history").status_code == 401
        resp = _http("PUT", f"/api/rules/{tag_rule['id']}", headers=admin,
                     body={"threshold": 70, "scope_values": ["冷库"]})
        assert resp.status_code == 200, resp.text
        resp = _http("GET", f"/api/rules/{tag_rule['id']}/history", headers=viewer)
        assert resp.status_code == 200, resp.text
        entries = resp.json()["data"]
        update = next(row for row in entries if row["action"] == "alert_rule.update")
        assert update["operator"] == "verify-admin", update
        assert update["changes"]["threshold"]["from"] == 75, update
        assert update["changes"]["threshold"]["to"] == 70, update
        assert update["changes"]["scope_values"]["from"] == ["冷库", "关键"], update
        assert update["changes"]["scope_values"]["to"] == ["冷库"], update
        dumped = json.dumps(entries, ensure_ascii=False).lower()
        for word in ("password", "token", "secret", "authorization"):
            assert word not in dumped, word
        audit = _pg(
            VERIFY_DB,
            "SELECT operator, action, target_id, detail FROM audit_log "
            "WHERE action LIKE 'alert_rule.%' ORDER BY id",
            fetch=True,
        )
        audit_update = next(
            row for row in audit
            if row["action"] == "alert_rule.update"
            and row["target_id"] == str(tag_rule["id"])
        )
        assert _json(audit_update["detail"])["changes"]["threshold"] == {"from": 75, "to": 70}
        print(f"[OK] 历史条目 operator={update['operator']}，changes.threshold="
              f"{json.dumps(update['changes']['threshold'], ensure_ascii=False)}，"
              f"scope_values={json.dumps(update['changes']['scope_values'], ensure_ascii=False)}；"
              "与 audit_log 一致且不含口令/密钥字段")

        print("v1.35.0 真实验证全部通过")
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
