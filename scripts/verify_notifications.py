#!/usr/bin/env python3
"""v1.29.0 告警通知真实投递验证。

不 mock 网络：启动本地 HTTP 接收器与本地 SMTP 桩，通过真实网关服务
（配置热读取、规则、静默、重试、旁路异步）把告警真正发出去。

覆盖：
1. critical 告警 → Webhook + SMTP 均真实收到（打印 payload、HMAC、邮件头/主题）
2. warning 但规则只配 critical → 不投递
3. 配置热更新（改 Webhook URL / 静默窗口）不重启即生效
4. 静默窗口：同设备同指标 3 条 → 只通知 1 次，3 条告警在 REST 仍可见
5. 接收端 500 → 指数退避重试、最终失败留痕；告警仍正常入库
6. MQTT 上报处理不等待通知（慢接收器 1.2s，处理耗时仍为毫秒级）
7. 日志与审计中搜不到 Webhook 密钥 / SMTP 口令

用法（仓库根目录）：
    gateway/.venv/bin/python scripts/verify_notifications.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

TMP = Path(tempfile.mkdtemp(prefix="vikings-notify-"))
# 必须在导入 app.config / app.database 之前设置
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP / 'notify.db'}"
os.environ["ALERT_SUPPRESS_SECONDS"] = "0"  # 关闭入库层 5 分钟去重，单独验证通知静默
os.environ["LLM_API_KEY"] = ""              # 走本地兜底文案，验证不依赖外部 LLM
os.environ["APP_ENV"] = "development"

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionFactory, prepare_database  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Device,
    DeviceAlert,
    NotificationDelivery,
)
from app.mqtt.handlers import MessageHandler  # noqa: E402
from app.security import create_access_token  # noqa: E402
from app.services import notify_service  # noqa: E402

WEBHOOK_SECRET = "whsec-verify-do-not-log-9f1c2a"
SMTP_PASSWORD = "smtp-verify-do-not-log-7b3d1e"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""))


class _HttpReceiver:
    """真实 HTTP 接收器；可配置状态码与延迟，记录 body/headers。"""

    def __init__(self, name: str, *, status: int = 200, delay: float = 0.0) -> None:
        self.name = name
        self.status = status
        self.delay = delay
        self.requests: list[dict] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                receiver.requests.append({"headers": dict(self.headers), "body": body})
                if receiver.delay:
                    threading.Event().wait(receiver.delay)
                self.send_response(receiver.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):  # 不打印访问日志（避免 URL 进入日志）
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/hook"

    def count_for(self, device_id: str) -> int:
        total = 0
        for request in self.requests:
            try:
                if json.loads(request["body"])["alert"]["deviceId"] == device_id:
                    total += 1
            except (KeyError, ValueError):
                continue
        return total

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _SmtpStub:
    """本地 SMTP 桩：EHLO/AUTH/MAIL/RCPT/DATA，记录完整邮件原文。"""

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 vikings-smtp-stub ESMTP\r\n")
        in_data = False
        buffer: list[bytes] = []
        while True:
            line = await reader.readline()
            if not line:
                break
            if in_data:
                if line in (b".\r\n", b".\n"):
                    self.messages.append(b"".join(buffer))
                    buffer = []
                    in_data = False
                    writer.write(b"250 2.0.0 queued\r\n")
                else:
                    buffer.append(line)
                continue
            command = line.strip()
            upper = command.upper()
            if upper.startswith(b"EHLO"):
                writer.write(b"250-localhost\r\n250-SIZE 10485760\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif upper.startswith(b"HELO"):
                writer.write(b"250 localhost\r\n")
            elif upper.startswith(b"AUTH"):
                writer.write(b"235 2.7.0 Authentication successful\r\n")
            elif upper.startswith(b"MAIL FROM"):
                writer.write(b"250 2.1.0 OK\r\n")
            elif upper.startswith(b"RCPT TO"):
                writer.write(b"250 2.1.5 OK\r\n")
            elif upper == b"DATA":
                writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                in_data = True
            elif upper == b"QUIT":
                writer.write(b"221 2.0.0 Bye\r\n")
                break
            elif upper == b"RSET":
                writer.write(b"250 2.0.0 OK\r\n")
            else:
                writer.write(b"250 2.0.0 OK\r\n")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


def _alert(
    device_id: str, metric: str = "voltage", level: str = "critical", alert_id: str | None = None
) -> dict:
    return {
        "alertId": alert_id or f"alt-{device_id}-{metric}",
        "deviceId": device_id,
        "deviceName": f"设备 {device_id}",
        "level": level,
        "metric": metric,
        "value": 2.9 if metric == "voltage" else 99.0,
        "threshold": 3.2 if metric == "voltage" else 80.0,
        "message": f"{metric} 越过阈值",
        "aiGenerated": False,
        "ts": int(time.time() * 1000),
    }


def _headers(username: str = "ops") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role='admin')}"}


async def _seed(factory) -> None:
    async with factory() as session:
        for device_id in ("dev-critical", "dev-warning", "dev-hot", "dev-mute", "dev-fail", "dev-block"):
            session.add(
                Device(
                    device_id=device_id,
                    name=f"通知设备 {device_id}",
                    group_name="默认分组",
                    status="online",
                )
            )
        # 静默场景的三条告警先入库，证明「不发通知但 App/REST 可见」
        for index in range(3):
            session.add(
                DeviceAlert(
                    alert_id=f"alt-dev-mute-temperature-{index}",
                    device_id="dev-mute",
                    level="critical",
                    metric="temperature",
                    value=85.0,
                    threshold=80.0,
                    message="温度过高",
                    ai_generated=False,
                    status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.add(
            DeviceAlert(
                alert_id="alt-dev-fail-rssi",
                device_id="dev-fail",
                level="critical",
                metric="rssi",
                value=-130.0,
                threshold=-110.0,
                message="信号极弱",
                ai_generated=False,
                status="open",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


async def _patch_config(client: AsyncClient, payload: dict) -> dict:
    response = await client.patch(
        "/api/system/notifications/config", json=payload, headers=_headers()
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _delivery_rows(device_id: str) -> list[NotificationDelivery]:
    async with SessionFactory() as session:
        rows = (
            (
                await session.execute(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.device_id == device_id)
                    .order_by(NotificationDelivery.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def main() -> None:
    await prepare_database()
    log_path = TMP / "gateway.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG)

    r1 = _HttpReceiver("receiver-1")
    r2 = _HttpReceiver("receiver-2")
    smtp = _SmtpStub()
    await smtp.start()
    print(f"[setup] 本地 HTTP 接收器 1: {r1.url}")
    print(f"[setup] 本地 HTTP 接收器 2: {r2.url}")
    print(f"[setup] 本地 SMTP 桩: 127.0.0.1:{smtp.port}（无 STARTTLS，AUTH PLAIN）")
    print(f"[setup] 临时数据库与日志: {TMP}")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await _seed(SessionFactory)

            print("\n=== 1. 配置写入 → 脱敏回显 → 审计 ===")
            data = await _patch_config(
                client,
                {
                    "enabled": True,
                    "webhook_enabled": True,
                    "webhook_url": r1.url,
                    "webhook_secret": WEBHOOK_SECRET,
                    "webhook_timeout_seconds": 5,
                    "webhook_max_attempts": 3,
                    "smtp_enabled": True,
                    "smtp_host": "127.0.0.1",
                    "smtp_port": smtp.port,
                    "smtp_username": "alerts@vikings.local",
                    "smtp_password": SMTP_PASSWORD,
                    "smtp_from": "alerts@vikings.local",
                    "smtp_to": "oncall@vikings.local",
                    "smtp_starttls": False,
                    "smtp_timeout_seconds": 5,
                    "smtp_max_attempts": 2,
                    "levels": ["critical"],
                    "mute_minutes": 10,
                    "retry_backoff_seconds": 0.2,
                },
            )
            fetched = (await client.get("/api/system/notifications/config", headers=_headers())).json()["data"]
            print("GET /config 回显:", json.dumps(fetched, ensure_ascii=False))
            check(
                "GET 配置不回显 URL/密钥/口令",
                fetched["webhook_url_configured"] is True
                and fetched["webhook_secret_configured"] is True
                and fetched["smtp_password_configured"] is True
                and WEBHOOK_SECRET not in json.dumps(fetched)
                and SMTP_PASSWORD not in json.dumps(fetched),
            )
            check("PATCH 响应不含明文密钥/口令", WEBHOOK_SECRET not in json.dumps(data) and SMTP_PASSWORD not in json.dumps(data))

            print("\n=== 2. critical 告警 → Webhook + SMTP 真实投递 ===")
            results = await notify_service.dispatch_alert(_alert("dev-critical"))
            print("dispatch 结果:", json.dumps(results, ensure_ascii=False))
            check("Webhook 真实收到", r1.count_for("dev-critical") == 1)
            check("SMTP 真实收到", len(smtp.messages) == 1)

            request = r1.requests[-1]
            payload = json.loads(request["body"])
            print("Webhook payload 样例:", json.dumps(payload, ensure_ascii=False, indent=2))
            expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), request["body"], hashlib.sha256).hexdigest()
            actual = request["headers"].get("X-Vikings-Signature")
            wrong = "sha256=" + hmac.new(b"wrong-secret", request["body"], hashlib.sha256).hexdigest()
            print(f"HMAC 预期: {expected}")
            print(f"HMAC 实际: {actual}")
            print(f"HMAC 错误密钥: {wrong}")
            check("HMAC 校验通过", actual == expected)
            check("HMAC 错误密钥不通过", actual != wrong)

            email = BytesParser(policy=policy.default).parsebytes(smtp.messages[-1])
            print("邮件头 Subject:", email["Subject"])
            print("邮件头 From:", email["From"])
            print("邮件头 To:", email["To"])
            check(
                "邮件主题含 CRITICAL",
                "CRITICAL" in str(email["Subject"]) and "dev-critical" in str(email["Subject"]),
            )

            print("\n=== 3. warning 告警 + 规则只配 critical → 不投递 ===")
            warning_result = await notify_service.dispatch_alert(
                _alert("dev-warning", level="warning")
            )
            print("dispatch 结果:", warning_result)
            check("warning 不投递", warning_result == [], "规则 levels=[critical]")
            check("Webhook 未新增", r1.count_for("dev-warning") == 0)
            check("SMTP 未新增", len(smtp.messages) == 1)

            print("\n=== 4. 配置热更新（不重启） ===")
            before_r1 = r1.count_for("dev-hot")
            await _patch_config(
                client, {"webhook_url": r2.url, "mute_minutes": 0}
            )
            await notify_service.dispatch_alert(_alert("dev-hot", metric="humidity"))
            print(f"Webhook URL 从接收器1 切到接收器2；接收器1 增量={r1.count_for('dev-hot') - before_r1}")
            check("热更新后新配置生效", r2.count_for("dev-hot") == 1 and r1.count_for("dev-hot") == before_r1)

            print("\n=== 5. 静默窗口：同设备同指标 3 条 → 只通知 1 次 ===")
            await _patch_config(client, {"webhook_url": r2.url, "mute_minutes": 10})
            before = r2.count_for("dev-mute")
            for _ in range(3):
                await notify_service.dispatch_alert(_alert("dev-mute", metric="temperature"))
            deliveries = await _delivery_rows("dev-mute")
            statuses = [row.status for row in deliveries]
            print("投递记录:", [(row.channel, row.status, row.attempts) for row in deliveries])
            check("仅真实投递 1 次", r2.count_for("dev-mute") - before == 1)
            check(
                "每通道 1 sent + 共 2 条 suppressed",
                statuses.count("sent") == 2 and statuses.count("suppressed") == 2,
                f"statuses={statuses}（sent=webhook+smtp 各 1 次）",
            )
            async with SessionFactory() as session:
                total = len(
                    (
                        await session.execute(
                            select(DeviceAlert).where(DeviceAlert.device_id == "dev-mute")
                        )
                    )
                    .scalars()
                    .all()
                )
            check("3 条告警在 REST/DB 仍可见", total == 3, f"DeviceAlert total={total}")

            print("\n=== 6. 接收端 500 → 重试与最终失败留痕 ===")
            r2.status = 500
            failed = await notify_service.dispatch_alert(_alert("dev-fail", metric="rssi"))
            print("dispatch 结果:", json.dumps(failed, ensure_ascii=False))
            rows = await _delivery_rows("dev-fail")
            print("投递记录:", [(row.channel, row.status, row.attempts, row.error) for row in rows])
            webhook_rows = [row for row in rows if row.channel == "webhook"]
            check(
                "Webhook 重试 3 次后记录最终失败",
                len(webhook_rows) == 1 and webhook_rows[0].status == "failed"
                and webhook_rows[0].attempts == 3 and webhook_rows[0].error == "HTTP 500",
                f"rows={[(row.channel, row.status, row.attempts, row.error) for row in rows]}",
            )
            async with SessionFactory() as session:
                alert_count = len(
                    (
                        await session.execute(
                            select(DeviceAlert).where(DeviceAlert.device_id == "dev-fail")
                        )
                    )
                    .scalars()
                    .all()
                )
            check("通知失败时告警仍正常入库", alert_count == 1)

            print("\n=== 7. MQTT 上报不等待通知（旁路异步） ===")
            r2.status = 200
            r2.delay = 1.2  # 接收器故意慢 1.2 秒
            received = {"report": 0, "alert": 0}

            class _FakeMqtt:
                async def publish(self, topic, payload):
                    received["report"] += 1

                async def publish_alert(self, payload):
                    received["alert"] += 1

            handler = MessageHandler()
            handler.bind(_FakeMqtt())
            report = {
                "deviceId": "dev-block",
                "temperature": 25.0,
                "humidity": 99.0,
                "voltage": 4.0,
                "rssi": -60,
                "battery": 80,
                "seq": 1,
                "ts": int(time.time() * 1000),
            }
            started = time.perf_counter()
            await handler("report", "dev-block", report)
            elapsed_ms = (time.perf_counter() - started) * 1000
            async with SessionFactory() as session:
                block_alerts = len(
                    (
                        await session.execute(
                            select(DeviceAlert).where(DeviceAlert.device_id == "dev-block")
                        )
                    )
                    .scalars()
                    .all()
                )
            check(
                "MQTT 处理耗时远小于 Webhook 延迟",
                elapsed_ms < 500,
                f"handler={elapsed_ms:.1f}ms, webhook delay={r2.delay * 1000:.0f}ms",
            )
            check("handler 返回时告警已入库", block_alerts >= 1)
            check("handler 返回时已发布 MQTT 告警", received["alert"] >= 1)
            await asyncio.sleep(2.0)  # 等旁路任务完成
            check(
                "后台通知最终仍送达",
                r2.count_for("dev-block") >= 1,
                f"receiver count={r2.count_for('dev-block')}",
            )

            print("\n=== 8. 脱敏：日志与审计中搜不到密钥/口令 ===")
            log_text = log_path.read_text(encoding="utf-8", errors="ignore")
            check(
                "日志不含 Webhook 密钥",
                WEBHOOK_SECRET not in log_text,
                f"log lines={len(log_text.splitlines())}",
            )
            check("日志不含 SMTP 口令", SMTP_PASSWORD not in log_text)
            check("日志不含完整 Webhook URL", r1.url not in log_text and r2.url not in log_text)
            async with SessionFactory() as session:
                audits = (await session.execute(select(AuditLog))).scalars().all()
            audit_text = json.dumps(
                [(row.action, row.detail) for row in audits], ensure_ascii=False
            )
            print("审计动作:", [row.action for row in audits])
            print("审计 detail 样例:", json.dumps(audits[-1].detail, ensure_ascii=False) if audits else "{}")
            check(
                "审计不含密钥/口令明文",
                WEBHOOK_SECRET not in audit_text and SMTP_PASSWORD not in audit_text,
            )
            print("日志文件:", log_path)

    finally:
        r1.close()
        r2.close()
        await smtp.stop()

    failed_checks = [name for name, ok, _ in checks if not ok]
    print("\n=== 汇总 ===")
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed_checks)} 项，失败 {len(failed_checks)} 项")
    if failed_checks:
        print("失败项:", failed_checks)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
