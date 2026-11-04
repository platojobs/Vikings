#!/usr/bin/env python3
"""真实 Broker 端到端验收：REST + MQTT over WebSocket + 设备模拟器 + 降级恢复。

覆盖 CI 无法验证的真实链路：
  1. 环境与端口前置检查；
  2. 启动（或复用）Mosquitto 1883/9001 与网关 8000；
  3. 模拟设备上线、上报、告警；
  4. 以移动看板相同方式连 WebSocket 9001 订阅实时增量；
  5. REST 下发指令并验证设备回执；
  6. REST 确认告警并验证状态 acked；
  7. 停 Broker 验证客户端断连与 REST 轮询兜底，重启后验证 WS 与网关恢复。

每次运行使用唯一 deviceId；默认启动临时 SQLite 库与独立进程，结束后清理，
不影响 `gateway/.env` 指向的生产库与镜像。

用法：
    cd /path/to/Vikings
    gateway/.venv/bin/python scripts/e2e/e2e_acceptance.py

退出码：0 全部 PASS；1 存在 FAIL 或执行中断。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = ROOT / "gateway"
VENV_PYTHON = GATEWAY_DIR / ".venv" / "bin" / "python"
MOSQUITTO_CONF = ROOT / "infra" / "mosquitto" / "mosquitto.conf"
LOG_DIR = ROOT / ".run" / "e2e"

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_WS_PORT = 9001
GATEWAY_URL = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    status: str
    detail: str
    elapsed: float = 0.0


class Reporter:
    def __init__(self) -> None:
        self.steps: List[StepResult] = []

    def add(self, name: str, status: str, detail: str, elapsed: float = 0.0) -> None:
        self.steps.append(StepResult(name, status, detail, elapsed))
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "-", "INFO": "·"}[status]
        print(f"[{icon}] {status:<4} {name}: {detail}")

    def run(self, name: str, fn: Callable[[], str]) -> Any:
        started = time.monotonic()
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - 验收脚本需要把异常转成 FAIL
            self.add(name, "FAIL", str(exc), time.monotonic() - started)
            raise
        self.add(name, "PASS", detail, time.monotonic() - started)
        return detail

    @property
    def failed(self) -> bool:
        return any(step.status == "FAIL" for step in self.steps)

    def print_summary(self) -> None:
        passed = sum(1 for step in self.steps if step.status == "PASS")
        failed = sum(1 for step in self.steps if step.status == "FAIL")
        print("\n" + "=" * 72)
        for step in self.steps:
            print(f"{step.status:<4}  {step.name:<28} {step.detail}")
        print("-" * 72)
        print(f"汇总：PASS={passed} FAIL={failed} 总耗时={sum(s.elapsed for s in self.steps):.1f}s")
        print("=" * 72)


# ---------------------------------------------------------------------------
# 环境检查与进程管理
# ---------------------------------------------------------------------------
class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.reporter = Reporter()
        self.own_broker = False
        self.own_gateway = False
        self.broker_proc: Optional[subprocess.Popen] = None
        self.gateway_proc: Optional[subprocess.Popen] = None
        self.simulator_proc: Optional[subprocess.Popen] = None
        self.log_handles: List[Any] = []
        self.temp_db: Optional[str] = None
        # Broker 的 persistence_location 是相对路径：把 cwd 指向临时目录，
        # 每次验收都从空的保留消息开始，且不污染仓库内 Broker 数据。
        self.broker_workdir: Optional[Path] = None
        self.gateway_python: Optional[str] = None
        self.client: Optional[httpx.Client] = None
        self.token = ""
        self.device_id = f"e2e-{uuid.uuid4().hex[:8]}"
        self.collector: Optional[WsCollector] = None
        self.alert_id: Optional[str] = None
        self.cmd_id: Optional[str] = None
        self.ack_seen: Optional[Dict[str, Any]] = None
        self.interrupted = False


def port_open(port: int, host: str = MQTT_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def wait_port(port: int, *, opened: bool, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port) == opened:
            return
        time.sleep(0.2)
    state = "打开" if opened else "释放"
    raise TimeoutError(f"端口 {port} 在 {timeout}s 内未{state}")


def log_tail(path: Path, lines: int = 12) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def find_mosquitto() -> Optional[str]:
    """优先 PATH；CI 的 apt 包常装到 /usr/sbin，补充兜底路径。"""
    found = shutil.which("mosquitto")
    if found:
        return found
    for candidate in ("/usr/sbin/mosquitto", "/usr/local/sbin/mosquitto"):
        if Path(candidate).exists():
            return candidate
    return None


def gateway_python(ctx: Context) -> str:
    """本地优先 gateway/.venv；CI 无 venv 时退回当前解释器（需已装齐依赖）。"""
    if ctx.gateway_python:
        return ctx.gateway_python
    candidates: List[str] = []
    if VENV_PYTHON.exists():
        candidates.append(str(VENV_PYTHON))
    candidates.append(sys.executable)
    errors: List[str] = []
    for exe in candidates:
        probe = subprocess.run(
            [exe, "-c", "import aiomqtt, paho.mqtt.client, websocket, uvicorn"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            ctx.gateway_python = exe
            return exe
        errors.append(f"{exe}: {probe.stderr.strip()}")
    raise RuntimeError(
        "缺少网关运行依赖（aiomqtt / paho-mqtt / websocket-client / uvicorn）；"
        "请安装 gateway/requirements.txt 与 scripts/e2e/requirements.txt。\n"
        + "\n".join(errors)
    )


def start_broker(ctx: Context) -> str:
    mosquitto = find_mosquitto()
    if not mosquitto:
        raise RuntimeError(
            "未安装 mosquitto（macOS: brew install mosquitto；"
            "Ubuntu: sudo apt-get install -y mosquitto）"
        )
    if not MOSQUITTO_CONF.exists():
        raise RuntimeError(f"缺少 Broker 配置：{MOSQUITTO_CONF}")
    conf = MOSQUITTO_CONF.read_text(encoding="utf-8")
    if "protocol websockets" not in conf or "9001" not in conf:
        raise RuntimeError("mosquitto.conf 未同时配置 protocol websockets 与 9001 端口")

    if port_open(MQTT_PORT) and port_open(MQTT_WS_PORT):
        ctx.reporter.add("broker", "INFO", "复用已运行的 1883/9001 Broker，不由脚本托管")
        return "复用现有 Broker"

    if port_open(MQTT_PORT) or port_open(MQTT_WS_PORT):
        raise RuntimeError("1883/9001 仅有一个被占用，环境不完整；请先停掉残留进程")

    if ctx.broker_workdir is None:
        ctx.broker_workdir = Path(tempfile.mkdtemp(prefix="vikings-e2e-broker-"))
    (ctx.broker_workdir / "infra" / "mosquitto" / "data").mkdir(
        parents=True, exist_ok=True
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "mosquitto.log"
    handle = log_path.open("w", encoding="utf-8")
    ctx.log_handles.append(handle)
    ctx.broker_proc = subprocess.Popen(
        [mosquitto, "-c", str(MOSQUITTO_CONF)],
        cwd=str(ctx.broker_workdir),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    ctx.own_broker = True
    try:
        wait_port(MQTT_PORT, opened=True, timeout=10)
        wait_port(MQTT_WS_PORT, opened=True, timeout=10)
    except Exception as exc:
        raise RuntimeError(f"Broker 启动失败：{exc}\n{log_tail(log_path)}") from exc
    return "脚本启动 Broker（1883 + 9001 websockets）"


def start_gateway(ctx: Context) -> str:
    if port_open(8000):
        ctx.reporter.add("gateway", "INFO", "复用已运行的 8000 网关，不由脚本托管")
        return "复用现有网关"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "gateway.log"
    fd, db_path = tempfile.mkstemp(prefix="vikings-e2e-", suffix=".db")
    os.close(fd)
    ctx.temp_db = db_path

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "MQTT_HOST": MQTT_HOST,
            "MQTT_PORT": str(MQTT_PORT),
            "ADMIN_USERNAME": ctx.args.username,
            "ADMIN_PASSWORD": ctx.args.password,
        }
    )
    handle = log_path.open("w", encoding="utf-8")
    ctx.log_handles.append(handle)
    ctx.gateway_proc = subprocess.Popen(
        [
            gateway_python(ctx),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(GATEWAY_DIR),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    ctx.own_gateway = True
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if ctx.gateway_proc.poll() is not None:
                raise RuntimeError("网关进程提前退出")
            try:
                resp = httpx.get(GATEWAY_URL + "/", timeout=2)
                if resp.status_code == 200:
                    return f"脚本启动网关（临时 SQLite：{Path(db_path).name}）"
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        raise RuntimeError("网关 25s 内未就绪")
    except Exception as exc:
        raise RuntimeError(f"网关启动失败：{exc}\n{log_tail(log_path)}") from exc


# ---------------------------------------------------------------------------
# WebSocket MQTT 订阅器（复用移动看板的传输方式）
# ---------------------------------------------------------------------------
class WsCollector:
    def __init__(self, client_id: str) -> None:
        self.messages: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.reconnected = threading.Event()
        self.connect_count = 0
        self.disconnect_count = 0
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="websockets",
            protocol=mqtt.MQTTv311,
        )
        self.client.ws_set_options(path="/mqtt")
        self.client.reconnect_delay_set(min_delay=1, max_delay=5)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            return
        self.connect_count += 1
        client.subscribe(
            [
                ("device/+/sensor/report", 1),
                ("device/+/status", 1),
                ("device/system/alert", 1),
                ("device/+/cmd/ack", 1),
            ]
        )
        self.connected.set()
        if self.connect_count > 1:
            self.reconnected.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self.disconnect_count += 1
        self.disconnected.set()
        if self.connect_count > 1:
            self.reconnected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        with self._lock:
            self.messages.append(
                {
                    "topic": msg.topic,
                    "payload": payload,
                    "received": time.time(),
                }
            )

    def start(self, timeout: float = 10) -> None:
        self.client.connect(MQTT_HOST, MQTT_WS_PORT, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            raise RuntimeError(f"WebSocket 9001 在 {timeout}s 内未完成 MQTT 握手")

    def wait_message(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        timeout: float,
        description: str,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for item in self.messages:
                    if predicate(item):
                        return item
            time.sleep(0.05)
        recent = [item["topic"] for item in self.messages[-8:]]
        raise TimeoutError(f"{timeout}s 内未收到{description}；最近消息：{recent}")

    def stop(self) -> None:
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001 - 清理阶段不掩盖原始结果
            pass
        self.client.loop_stop()


# ---------------------------------------------------------------------------
# REST 与业务步骤
# ---------------------------------------------------------------------------
def api(ctx: Context, method: str, path: str, **kwargs: Any) -> Any:
    assert ctx.client is not None
    headers = kwargs.pop("headers", {})
    if ctx.token:
        headers["Authorization"] = f"Bearer {ctx.token}"
    resp = ctx.client.request(method, path, headers=headers, **kwargs)
    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {path} 返回非 JSON（HTTP {resp.status_code}）") from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"{method} {path} 失败 HTTP {resp.status_code}: "
            f"{body.get('message') or body.get('detail') or body}"
        )
    if body.get("code") not in (0, None):
        raise RuntimeError(f"{method} {path} 业务失败: {body}")
    return body.get("data")


def login(ctx: Context) -> str:
    assert ctx.client is not None
    data = api(
        ctx,
        "POST",
        "/api/auth/login",
        json={"username": ctx.args.username, "password": ctx.args.password},
    )
    ctx.token = str(data["token"])
    role = (data.get("user") or {}).get("role")
    if role != "admin":
        raise RuntimeError(f"验收需要 admin 账号，当前 role={role}")
    return f"admin 登录成功（token 长度 {len(ctx.token)}）"


def start_simulator(ctx: Context) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "simulator.log"
    handle = log_path.open("w", encoding="utf-8")
    ctx.log_handles.append(handle)
    ctx.simulator_proc = subprocess.Popen(
        [
            gateway_python(ctx),
            "scripts/simulate_device.py",
            "--device-id",
            ctx.device_id,
            "--count",
            "3",
            "--interval",
            "3",
            "--voltage",
            "2.90",
        ],
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return f"模拟器启动 device={ctx.device_id}（voltage=2.90 触发 critical 告警）"


def verify_realtime_report(ctx: Context) -> str:
    assert ctx.collector is not None
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/sensor/report",
        12,
        "设备实时上报",
    )
    lag_ms = int(item["received"] * 1000 - int(item["payload"].get("ts", 0)))
    if lag_ms > 3000:
        raise RuntimeError(f"上报到 WebSocket 的延迟过高：{lag_ms}ms（期望 <3000ms）")
    return f"WebSocket 收到上报，端到端延迟约 {lag_ms}ms，seq={item['payload'].get('seq')}"


def verify_realtime_status(ctx: Context) -> str:
    assert ctx.collector is not None
    online = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/status"
        and m["payload"].get("status") == "online",
        10,
        "设备上线状态",
    )
    offline = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/status"
        and m["payload"].get("status") == "offline",
        14,
        "设备下线状态",
    )
    return (
        "WebSocket 收到上线/下线："
        f"online(reason={online['payload'].get('reason')}) → "
        f"offline(reason={offline['payload'].get('reason')})"
    )


def wait_report_round(ctx: Context, min_seq: int = 2) -> str:
    """等到设备下一轮上报：模拟器只在每次上报后保留 6 秒指令窗口，
    必须在这段时间内下发指令，否则设备处于休眠、收不到云端消息。"""
    assert ctx.collector is not None
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/sensor/report"
        and int(m["payload"].get("seq", 0)) >= min_seq,
        12,
        f"第 {min_seq} 轮上报（进入指令接收窗口）",
    )
    return f"设备进入第 {min_seq} 轮唤醒窗口，seq={item['payload'].get('seq')}"


def verify_rest_device(ctx: Context) -> str:
    # WS 上报先到、REST 建档/入库稍慢（CI 上更明显），短轮询等待而非一次判定
    deadline = time.monotonic() + 10
    device: Optional[Dict[str, Any]] = None
    reading: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        data = api(
            ctx,
            "GET",
            "/api/devices",
            params={"page": 1, "pageSize": 100, "keyword": ctx.device_id},
        )
        items = data.get("items") or []
        device = next(
            (d for d in items if d.get("device_id") == ctx.device_id), None
        )
        if device is not None:
            latest = api(ctx, "GET", "/api/records/latest")
            reading = next(
                (r for r in latest if r.get("device_id") == ctx.device_id), None
            )
            if reading is not None:
                break
        time.sleep(0.3)
    if device is None:
        raise RuntimeError("REST 设备列表未找到模拟设备（等待 10s 仍未建档）")
    if reading is None:
        raise RuntimeError("REST /api/records/latest 未返回模拟设备最新值（等待 10s）")
    return (
        f"REST 读到设备 status={device.get('status')}，"
        f"最新值 temperature={reading.get('temperature')} humidity={reading.get('humidity')}"
    )


def verify_realtime_alert(ctx: Context) -> str:
    assert ctx.collector is not None
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == "device/system/alert"
        and m["payload"].get("deviceId") == ctx.device_id,
        15,
        "critical 告警推送",
    )
    payload = item["payload"]
    ctx.alert_id = payload.get("alertId")
    if not ctx.alert_id:
        raise RuntimeError(f"告警推送缺少 alertId：{payload}")
    return (
        f"WebSocket 收到告警 alertId={ctx.alert_id} "
        f"level={payload.get('level')} metric={payload.get('metric')}"
    )


def dispatch_command(ctx: Context) -> str:
    data = api(
        ctx,
        "POST",
        f"/api/devices/{ctx.device_id}/commands",
        json={
            "type": "config",
            "payload": {"reportIntervalSec": 10, "sleepSec": 5},
        },
    )
    ctx.cmd_id = data.get("cmd_id")
    if not ctx.cmd_id:
        raise RuntimeError(f"指令下发响应缺少 cmd_id：{data}")
    return f"REST 下发 config 成功 cmd_id={ctx.cmd_id} state={data.get('state')}"


def verify_command_ack(ctx: Context) -> str:
    assert ctx.collector is not None
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/cmd/ack"
        and m["payload"].get("cmdId") == ctx.cmd_id,
        12,
        "设备指令回执",
    )
    ctx.ack_seen = item["payload"]
    if item["payload"].get("state") != "success":
        # 先收到 received/progress 也继续等 success
        ctx.collector.wait_message(
            lambda m: m["topic"] == f"device/{ctx.device_id}/cmd/ack"
            and m["payload"].get("cmdId") == ctx.cmd_id
            and m["payload"].get("state") == "success",
            12,
            "指令 success 回执",
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        commands = api(
            ctx,
            "GET",
            f"/api/devices/{ctx.device_id}/commands",
            params={"limit": 20},
        )
        row = next((c for c in commands if c.get("cmd_id") == ctx.cmd_id), None)
        if row and row.get("state") == "success":
            return (
                "模拟设备已回执 success；REST 指令历史 "
                f"state={row.get('state')} progress={row.get('progress')}"
            )
        time.sleep(0.3)
    raise RuntimeError("设备已回执，但 REST 指令历史未在 10s 内变为 success")


def ack_alert(ctx: Context) -> str:
    if not ctx.alert_id:
        data = api(
            ctx,
            "GET",
            "/api/alerts",
            params={"deviceId": ctx.device_id, "status": "open", "pageSize": 50},
        )
        items = data.get("items") or []
        if not items:
            raise RuntimeError("未找到可确认的未处理告警")
        ctx.alert_id = items[0]["alert_id"]
    acked = api(ctx, "POST", f"/api/alerts/{ctx.alert_id}/ack")
    if acked.get("status") != "acked":
        raise RuntimeError(f"确认后状态不是 acked：{acked}")
    open_data = api(
        ctx,
        "GET",
        "/api/alerts",
        params={"deviceId": ctx.device_id, "status": "open", "pageSize": 50},
    )
    open_ids = [item["alert_id"] for item in open_data.get("items") or []]
    if ctx.alert_id in open_ids:
        raise RuntimeError("确认后告警仍出现在 open 列表")
    acked_data = api(
        ctx,
        "GET",
        "/api/alerts",
        params={"deviceId": ctx.device_id, "status": "acked", "pageSize": 50},
    )
    acked_ids = [item["alert_id"] for item in acked_data.get("items") or []]
    if ctx.alert_id not in acked_ids:
        raise RuntimeError("确认后告警未出现在 acked 列表")
    return f"告警 {ctx.alert_id} 已 acked，open 列表不再返回，acked 列表可查"


def stop_simulator(ctx: Context) -> str:
    proc = ctx.simulator_proc
    if proc is None or proc.poll() is not None:
        return "模拟器已自行退出"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    return "模拟器已停止"


def gateway_mqtt_connected(ctx: Context) -> bool:
    status = api(ctx, "GET", "/api/system/status")
    return bool((status.get("mqtt") or {}).get("connected"))


def degrade_disconnect(ctx: Context) -> str:
    if not ctx.own_broker or ctx.broker_proc is None:
        raise RuntimeError("Broker 由外部托管，无法执行停 Broker 降级验证（请改用脚本托管）")
    assert ctx.collector is not None
    if not gateway_mqtt_connected(ctx):
        raise RuntimeError("降级前网关 MQTT 已断开，环境不稳定")
    ctx.collector.disconnected.clear()
    ctx.broker_proc.terminate()
    try:
        ctx.broker_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        ctx.broker_proc.kill()
        ctx.broker_proc.wait(timeout=5)
    wait_port(MQTT_PORT, opened=False, timeout=5)
    wait_port(MQTT_WS_PORT, opened=False, timeout=5)
    if not ctx.collector.disconnected.wait(10):
        raise RuntimeError("停 Broker 后 WebSocket 客户端未在 10s 内判定断连")
    return "Broker 已停止，WebSocket 客户端触发 on_disconnect（进入轮询降级前提）"


def degrade_polling_rest(ctx: Context) -> str:
    try:
        devices = api(ctx, "GET", "/api/devices", params={"page": 1, "pageSize": 100})
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Broker 停机时 REST 不可用，轮询兜底失败：{exc}") from exc
    count = len(devices.get("items") or [])
    return f"Broker 停机期间 REST 仍可用（设备 {count} 台），移动看板可切轮询"


def recover_broker(ctx: Context) -> str:
    return start_broker(ctx)


def recover_ws(ctx: Context) -> str:
    assert ctx.collector is not None
    if not ctx.collector.reconnected.wait(25):
        raise RuntimeError("Broker 恢复后 WebSocket 客户端 25s 内未重连")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if gateway_mqtt_connected(ctx):
            return "WebSocket 客户端已重连；网关 /api/system/status mqtt.connected=true"
        time.sleep(0.5)
    raise RuntimeError("WebSocket 已重连，但网关 MQTT 40s 内未恢复")


def recover_real_data(ctx: Context) -> str:
    assert ctx.collector is not None
    marker_seq = 9999
    publisher = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"e2e-pub-{uuid.uuid4().hex[:6]}",
        transport="tcp",
        protocol=mqtt.MQTTv311,
    )
    publisher.connect(MQTT_HOST, MQTT_PORT, keepalive=15)
    publisher.loop_start()
    try:
        publisher.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps(
                {
                    "deviceId": ctx.device_id,
                    "temperature": 24.0,
                    "humidity": 55.0,
                    "voltage": 3.75,
                    "rssi": -60,
                    "battery": 80,
                    "seq": marker_seq,
                    "ts": int(time.time() * 1000),
                }
            ),
            qos=1,
        ).wait_for_publish(timeout=5)
    finally:
        publisher.loop_stop()
        publisher.disconnect()
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/sensor/report"
        and int(m["payload"].get("seq", 0)) == marker_seq,
        15,
        "恢复后的实时上报",
    )
    return f"恢复后 WebSocket 重新收到实时上报 seq={item['payload'].get('seq')}"


# ---------------------------------------------------------------------------
# 清理与入口
# ---------------------------------------------------------------------------
def cleanup(ctx: Context) -> None:
    started = time.monotonic()
    notes: List[str] = []
    if ctx.collector is not None:
        ctx.collector.stop()
        ctx.collector = None
        notes.append("WS 客户端已停止")
    for name in ("simulator_proc", "gateway_proc", "broker_proc"):
        proc = getattr(ctx, name)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            notes.append(f"{name.replace('_proc', '')} 已停止")
        setattr(ctx, name, None)
    for handle in ctx.log_handles:
        try:
            handle.close()
        except OSError:
            pass
    if ctx.client is not None:
        ctx.client.close()
    if ctx.temp_db:
        for suffix in ("", "-wal", "-shm"):
            Path(ctx.temp_db + suffix).unlink(missing_ok=True)
    if ctx.broker_workdir:
        shutil.rmtree(ctx.broker_workdir, ignore_errors=True)
        ctx.broker_workdir = None

    if ctx.own_broker:
        try:
            wait_port(MQTT_PORT, opened=False, timeout=5)
            wait_port(MQTT_WS_PORT, opened=False, timeout=5)
        except TimeoutError as exc:
            notes.append(f"端口清理异常：{exc}")
    if ctx.own_gateway:
        try:
            wait_port(8000, opened=False, timeout=8)
        except TimeoutError as exc:
            notes.append(f"端口清理异常：{exc}")

    status = "PASS" if not any("异常" in note for note in notes) else "FAIL"
    ctx.reporter.add(
        "cleanup",
        status,
        "；".join(notes) if notes else "无需清理（未启动托管进程）",
        time.monotonic() - started,
    )


def connect_ws(ctx: Context) -> str:
    assert ctx.collector is not None
    ctx.collector.start(10)
    return f"MQTT over WebSocket 已连接 {MQTT_HOST}:{MQTT_WS_PORT}（与移动看板同一方式）"


def main() -> int:
    parser = argparse.ArgumentParser(description="Vikings 真实 Broker 端到端验收")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    args = parser.parse_args()

    ctx = Context(args)
    rep = ctx.reporter
    try:
        rep.run("env_preflight", lambda: preflight(ctx))
        rep.run("broker", lambda: start_broker(ctx))
        rep.run("gateway", lambda: start_gateway(ctx))
        ctx.client = httpx.Client(base_url=GATEWAY_URL, timeout=10)
        rep.run("auth_login", lambda: login(ctx))
        ctx.collector = WsCollector(f"e2e-ws-{uuid.uuid4().hex[:8]}")
        rep.run("ws_connected", lambda: connect_ws(ctx))
        rep.run("simulator", lambda: start_simulator(ctx))
        rep.run("realtime_report", lambda: verify_realtime_report(ctx))
        # 先查 REST 快照：此时设备仍在线，可同时验证设备与最新值
        rep.run("rest_device", lambda: verify_rest_device(ctx))
        rep.run("realtime_status", lambda: verify_realtime_status(ctx))
        rep.run("realtime_alert", lambda: verify_realtime_alert(ctx))
        # 指令必须在设备唤醒窗口内下发，先等下一轮上报
        rep.run("command_window", lambda: wait_report_round(ctx, 2))
        rep.run("command_dispatch", lambda: dispatch_command(ctx))
        rep.run("command_ack", lambda: verify_command_ack(ctx))
        rep.run("alert_ack", lambda: ack_alert(ctx))
        rep.run("stop_simulator", lambda: stop_simulator(ctx))
        rep.run("degrade_disconnect", lambda: degrade_disconnect(ctx))
        rep.run("degrade_polling_rest", lambda: degrade_polling_rest(ctx))
        rep.run("recover_broker", lambda: recover_broker(ctx))
        rep.run("recover_ws", lambda: recover_ws(ctx))
        rep.run("recover_real_data", lambda: recover_real_data(ctx))
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，确保清理与汇总
        print(f"\n执行中断：{exc}", file=sys.stderr)
    finally:
        cleanup(ctx)

    rep.print_summary()
    if rep.failed:
        print("结果：存在 FAIL，详见上方逐项输出与 .run/e2e/*.log")
        return 1
    print("结果：全部 PASS")
    return 0


def preflight(ctx: Context) -> str:
    if not find_mosquitto():
        raise RuntimeError(
            "未安装 mosquitto（macOS: brew install mosquitto；"
            "Ubuntu: sudo apt-get install -y mosquitto）"
        )
    interpreter = gateway_python(ctx)
    if not MOSQUITTO_CONF.exists():
        raise RuntimeError(f"缺少 Broker 配置：{MOSQUITTO_CONF}")
    conf = MOSQUITTO_CONF.read_text(encoding="utf-8")
    if "protocol websockets" not in conf or "listener 9001" not in conf:
        raise RuntimeError("Broker 配置缺少 `listener 9001` 或 `protocol websockets`")
    ports = {p: port_open(p) for p in (1883, 9001, 8000, 5173)}
    return (
        f"mosquitto/{Path(interpreter).name} 可用；端口状态 "
        + ", ".join(f"{p}={'占用' if v else '空闲'}" for p, v in ports.items())
        + f"；将使用临时库与临时 Broker 数据目录，deviceId={ctx.device_id}"
    )


if __name__ == "__main__":
    sys.exit(main())
