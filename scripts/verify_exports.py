#!/usr/bin/env python3
"""v1.30.0 CSV 导出真实大数据验证（不 mock 数据库、不 mock HTTP）。

流程：
1. 在真实 PostgreSQL 上新建独立验证库 `iot_export_verify`，跑完整 Alembic 迁移；
2. 造 200,000 条 sensor_record + 200,001 条 device_alert（含逗号/引号/换行设备名）；
3. 启动真实 uvicorn 网关（独立进程），用真实 JWT 调用 `/api/exports/*`；
4. 流式下载 CSV，测量文件大小/行数/耗时与网关进程 RSS 峰值；
5. 用 `file` 检测编码，用 Python `csv` 回读校验列数、特殊字符、前导零与 UTC 时间；
6. 对比「分页 API 翻页拉全量」的请求数、耗时、JSON 体积、RSS 与 tracemalloc 峰值；
7. 权限矩阵 viewer/operator/admin 与超限 422 的真实响应；
8. 查询审计表，打印脱敏后的真实记录。

用法（仓库根目录）：
    gateway/.venv/bin/python scripts/verify_exports.py
"""

import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

VERIFY_DB = "iot_export_verify"
PORT = 8127
MESSY_NAME = '华东, "1号" 设备\n(新)'
ALERT_TOTAL = 200_001  # 比默认上限 200_000 多 1 行，用来验证 422 而不是静默截断


def _repo_database_url() -> str:
    """从 gateway/.env 读取 DATABASE_URL；没有则明确要求配置 PostgreSQL。"""
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


def _run_alembic() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = VERIFY_URL
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT / "gateway",
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _create_database() -> None:
    """用本机 psql 超级用户建独立验证库，属主给业务账号。

    业务账号（.env 里的 iot）通常没有 CREATEDB 权限，这是生产上的正确配置，
    所以建库走本机管理员，迁移与查询仍用业务账号，验证链路不降权。
    """
    owner = urlsplit(REPO_URL.replace("postgresql+asyncpg", "postgresql")).username
    subprocess.run(
        [
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f'DROP DATABASE IF EXISTS "{VERIFY_DB}" WITH (FORCE)',
            "-c",
            f'CREATE DATABASE "{VERIFY_DB}" OWNER {owner}',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


async def _seed() -> None:
    import asyncpg

    conn = await asyncpg.connect(_dsn(REPO_URL, VERIFY_DB, asyncpg=False))
    try:
        await conn.execute(
            """
            INSERT INTO device (device_id, name, model, firmware_version, group_name, status,
                                mqtt_credential_version)
            VALUES
              ('dev-0001', $1, 'VG-S100', '1.0.0', '华东', 'online', 0),
              ('dev-0002', '华南设备', 'VG-S100', '1.0.0', '华南', 'online', 0),
              ('001', '前导零设备', 'VG-S100', '1.0.0', '华东', 'offline', 0)
            """,
            MESSY_NAME,
        )
        # 200,000 条记录：120k + 60k + 20k，时间各设备独立递增
        for device_id, count, offset in (
            ("dev-0001", 120_000, 0),
            ("dev-0002", 60_000, 120_000),
            ("001", 20_000, 180_000),
        ):
            await conn.execute(
                """
                INSERT INTO sensor_record
                    (device_id, temperature, humidity, voltage, rssi, battery, seq, ts)
                SELECT $1,
                       20 + (i % 100) / 10.0,
                       40 + (i % 50),
                       3.60 + (i % 9) / 100.0,
                       -70 - (i % 20),
                       80 + (i % 15),
                       $3 + i,
                       TIMESTAMPTZ '2026-10-01 00:00:00+00' + (i || ' seconds')::interval
                FROM generate_series(1, $2) i
                """,
                device_id,
                count,
                offset,
            )
        # 告警：200,001 条（超过默认上限 1 条），覆盖 warning/critical、open/acked
        await conn.execute(
            f"""
            INSERT INTO device_alert
                (alert_id, device_id, level, metric, value, threshold, message,
                 ai_generated, status, created_at, ack_note, acked_by, acked_at)
            SELECT 'alt-' || i,
                   CASE WHEN i % 3 = 0 THEN '001'
                        WHEN i % 3 = 1 THEN 'dev-0001'
                        ELSE 'dev-0002' END,
                   CASE WHEN i % 4 = 0 THEN 'warning' ELSE 'critical' END,
                   'voltage', 2.95, 3.20,
                   CASE WHEN i % 7 = 0 THEN '-20°C, 电压过低' || chr(10) || '请联系值班'
                        ELSE '电压过低' END,
                   (i % 2 = 0),
                   CASE WHEN i % 5 = 0 THEN 'acked' ELSE 'open' END,
                   TIMESTAMPTZ '2026-10-01 00:00:00+00' + (i || ' seconds')::interval,
                   CASE WHEN i % 5 = 0 THEN '已到现场' || chr(10) || '复核完成' ELSE NULL END,
                   CASE WHEN i % 5 = 0 THEN 'operator' ELSE NULL END,
                   CASE WHEN i % 5 = 0 THEN TIMESTAMPTZ '2026-10-02 00:00:00+00' ELSE NULL END
            FROM generate_series(1, {ALERT_TOTAL}) i
            """
        )
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM sensor_record) AS records,"
            " (SELECT count(*) FROM device_alert) AS alerts"
        )
        print(f"SEED sensor_record={counts['records']} device_alert={counts['alerts']}")
    finally:
        await conn.close()


def _token(role: str, username: str) -> str:
    import jwt

    secret = os.environ["JWT_SECRET"]
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _rss_kb(pid: int) -> int:
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
    return int(out) if out else 0


class _RssMonitor:
    """轮询网关进程 RSS（macOS/Linux `ps` 均可），记录峰值。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.peak_kb = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    ["ps", "-o", "rss=", "-p", str(self.pid)], text=True
                ).strip()
                if out:
                    self.peak_kb = max(self.peak_kb, int(out))
            except (subprocess.CalledProcessError, ValueError):
                pass
            self._stop.wait(0.02)

    def start(self) -> "_RssMonitor":
        self._thread.start()
        return self

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=2)
        return self.peak_kb


def _start_server() -> subprocess.Popen:
    log = Path(tempfile.mkdtemp(prefix="vikings-export-")) / "gateway.log"
    env = os.environ.copy()
    secret = "verify-export-secret-0123456789abcdef-must-not-be-prod"
    env.update(
        {
            "DATABASE_URL": VERIFY_URL,
            "APP_ENV": "development",
            "JWT_SECRET": secret,
            "ADMIN_PASSWORD": "VerifyAdmin#2026",
            "SEED_DEMO_USERS": "false",
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": "1883",
            "EXPORT_MAX_ROWS": "200000",
            "LOG_LEVEL": "WARNING",
        }
    )
    os.environ["JWT_SECRET"] = secret
    handle = open(log, "w", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT / "gateway",
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def _wait_ready(timeout: float = 40.0) -> None:
    """等待 HTTP 存活（/api/health）；MQTT 未连不影响导出验证，只会在日志里重连。"""
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("网关未在限定时间内就绪，见 uvicorn 日志")


def _download(path: str, params: dict, headers: dict, target: Path) -> dict:
    import httpx

    started = time.perf_counter()
    with httpx.Client(timeout=120) as client:
        with client.stream(
            "GET", f"http://127.0.0.1:{PORT}{path}", params=params, headers=headers
        ) as response:
            if response.status_code != 200:
                body = response.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {response.status_code}: {body[:300]}")
            disposition = response.headers.get("content-disposition", "")
            with open(target, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    return {
        "seconds": time.perf_counter() - started,
        "bytes": target.stat().st_size,
        "disposition": disposition,
    }


def _status(path: str, params: dict, headers: dict) -> tuple[int, str]:
    import httpx

    with httpx.Client(timeout=60) as client:
        with client.stream(
            "GET", f"http://127.0.0.1:{PORT}{path}", params=params, headers=headers
        ) as response:
            body = response.read().decode("utf-8", "replace") if response.status_code != 200 else ""
            return response.status_code, body[:200]


def _read_csv(path: Path) -> tuple[list[str], int, int]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = 0
        columns_ok = True
        for row in reader:
            rows += 1
            if len(row) != len(header):
                columns_ok = False
    return header, rows, len(header) if columns_ok else -1


def _file_type(path: Path) -> str:
    return subprocess.check_output(["file", str(path)], text=True).strip()


def _fetch_export(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vikings-export-out-"))
    print("== 1. 准备真实 PostgreSQL ==")
    _create_database()
    _run_alembic()
    asyncio.run(_seed())
    print(f"验证库: {_dsn(REPO_URL, VERIFY_DB, asyncpg=False)}")

    print("\n== 2. 启动真实网关进程 ==")
    server = _start_server()
    try:
        _wait_ready()
        print(f"uvicorn pid={server.pid} port={PORT}")

        viewer = {"Authorization": f"Bearer {_token('viewer', 'verify-viewer')}"}
        operator = {"Authorization": f"Bearer {_token('operator', 'verify-operator')}"}
        admin = {"Authorization": f"Bearer {_token('admin', 'verify-admin')}"}

        time.sleep(0.5)
        baseline_kb = _rss_kb(server.pid)
        print(f"网关启动后基线RSS={baseline_kb / 1024:.1f} MB")

        print("\n== 3a. 小范围导出（20,000 行）作为内存对照 ==")
        small_path = tmp / "records_small.csv"
        monitor = _RssMonitor(server.pid).start()
        small = _download("/api/exports/records", {"deviceId": "001"}, operator, small_path)
        small_peak_kb = monitor.stop()
        print(
            f"20,000 行导出 大小={small['bytes'] / 1024 / 1024:.2f} MB "
            f"耗时={small['seconds']:.2f}s RSS峰值={small_peak_kb / 1024:.1f} MB "
            f"（与 200k 同进程，内存应接近基线）"
        )

        print("\n== 3. 导出 200,000 行设备记录（流式） ==")
        records_path = tmp / "records.csv"
        monitor = _RssMonitor(server.pid).start()
        result = _download("/api/exports/records", {}, operator, records_path)
        peak_export_kb = monitor.stop()
        print(
            f"文件={records_path.name} 大小={result['bytes'] / 1024 / 1024:.2f} MB "
            f"耗时={result['seconds']:.2f}s 网关RSS峰值={peak_export_kb / 1024:.1f} MB "
            f"Content-Disposition={result['disposition']}"
        )
        records_seconds = result["seconds"]
        header, rows, columns = _read_csv(records_path)
        raw = records_path.read_bytes()[:3]
        print(
            f"BOM={raw!r} 行数={rows} 列数={columns} 表头={header}"
        )
        print(f"file: {_file_type(records_path)}")
        parsed = _fetch_export(records_path)
        data_rows = parsed[1:]
        messy = [row for row in data_rows if row[2] == MESSY_NAME]
        zero = [row for row in data_rows if row[1] == '="001"']
        print(
            f"特殊设备名回读={len(messy)} 行（含逗号/引号/换行，示例={messy[0][2]!r}）"
            f" 前导零设备ID={zero[0][1] if zero else '缺失'} "
            f"UTC时间示例={data_rows[0][10]}"
        )
        assert rows == 200_000 and columns == 11
        assert raw == b"\xef\xbb\xbf"
        assert messy and zero
        print("PASS 记录 CSV 行数/列数/BOM/转义/前导零/UTC")

        print("\n== 4. 告警导出（筛选子集）与超限 422 ==")
        alerts_path = tmp / "alerts_subset.csv"
        result = _download(
            "/api/exports/alerts",
            {
                "level": "warning",
                "status": "open",
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
            },
            operator,
            alerts_path,
        )
        header, rows, columns = _read_csv(alerts_path)
        print(
            f"筛选导出 大小={result['bytes'] / 1024:.1f} KB 行数={rows} 列数={columns} "
            f"文件名={result['disposition']}"
        )
        assert "alerts_2026-10-01_2026-10-02.csv" in result["disposition"]
        alert_rows = _fetch_export(alerts_path)[1:]
        quoted = [row for row in alert_rows if "\n" in row[8]]
        prefixed = [row for row in alert_rows if row[8].startswith("'-20")]
        print(
            f"含换行告警内容回读={len(quoted)} 行，示例={quoted[0][8]!r}"
            f"；以 - 开头已加前缀={len(prefixed)} 行"
        )
        assert quoted and prefixed

        status_code, body = _status("/api/exports/alerts", {}, operator)
        print(f"未筛选告警导出（{ALERT_TOTAL} 行）→ HTTP {status_code} {body}")
        assert status_code == 422 and "超过上限" in body

        print("\n== 5. 权限矩阵 ==")
        matrix = {}
        for role, headers in (("viewer", viewer), ("operator", operator), ("admin", admin)):
            code, _ = _status("/api/exports/records", {"deviceId": "001"}, headers)
            matrix[role] = code
            print(f"{role:8s} records=HTTP {code}")
        assert matrix == {"viewer": 403, "operator": 200, "admin": 200}
        print("PASS viewer 403 / operator 200 / admin 200")

        print("\n== 6. 对比：分页 API 全量拉取同 200,000 行 ==")
        import tracemalloc

        import httpx

        page_started = time.perf_counter()
        page_requests = 0
        page_bytes = 0
        page_rows = 0
        tracemalloc.start()
        monitor = _RssMonitor(server.pid).start()
        with httpx.Client(timeout=60) as client:
            for device_id, total in (("dev-0001", 120_000), ("dev-0002", 60_000), ("001", 20_000)):
                for offset in range(0, total, 2000):
                    response = client.get(
                        f"http://127.0.0.1:{PORT}/api/devices/{device_id}/records",
                        params={"limit": 2000, "offset": offset},
                        headers=operator,
                    )
                    page_requests += 1
                    page_bytes += len(response.content)
                    payload = response.json()["data"]
                    page_rows += len(payload)
        page_peak_kb = monitor.stop()
        _, client_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        page_seconds = time.perf_counter() - page_started
        print(
            f"请求数={page_requests} 行数={page_rows} 耗时={page_seconds:.2f}s "
            f"JSON体积={page_bytes / 1024 / 1024:.2f} MB "
            f"网关RSS峰值={page_peak_kb / 1024:.1f} MB "
            f"客户端tracemalloc峰值={client_peak / 1024 / 1024:.2f} MB"
        )
        print(
            f"对比：流式导出 200,000 行耗时={records_seconds:.2f}s；"
            f"分页共 {page_requests} 次请求、{page_seconds:.2f}s"
        )
        assert page_rows == 200_000

        print("\n== 7. 审计真实记录（脱敏） ==")
        import asyncpg

        async def _audit() -> list[dict]:
            conn = await asyncpg.connect(_dsn(REPO_URL, VERIFY_DB, asyncpg=False))
            try:
                rows = await conn.fetch(
                    "SELECT operator, action, target_type, target_id, detail, created_at"
                    " FROM audit_log WHERE action LIKE 'export.%' ORDER BY id DESC LIMIT 5"
                )
                return [dict(row) for row in rows]
            finally:
                await conn.close()

        for row in asyncio.run(_audit()):
            detail = row["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            redacted = {k: detail[k] for k in ("filters", "rows", "duration_ms", "filename") if k in detail}
            print(
                f"{row['created_at'].isoformat()} operator={row['operator']} "
                f"action={row['action']} target={row['target_type']}:{row['target_id']} "
                f"detail={json.dumps(redacted, ensure_ascii=False)}"
            )
            assert set(detail) == {"filters", "rows", "duration_ms", "filename"}
            assert "电压过低" not in json.dumps(detail, ensure_ascii=False)
        print("PASS 审计只含筛选/行数/耗时/文件名，无导出内容")

        print("\n== 8. 输出文件 ==")
        for path in (records_path, alerts_path):
            print(f"{path}  {path.stat().st_size} bytes")
        print("\n全部导出验证通过")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
