#!/usr/bin/env python3
"""真实 Broker 端到端验收：REST + MQTT over WebSocket + 设备模拟器 + 降级恢复。

覆盖 CI 无法验证的真实链路：
  1. 环境与端口前置检查；
  2. 启动 Mosquitto 1883/9001 与网关 8000（要求端口全部空闲，使用临时凭证）；
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
import signal
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

# 复用网关内的凭证工具（纯标准库），保证 E2E 与生产生成同一格式的 PBKDF2 hash
sys.path.insert(0, str(GATEWAY_DIR))
from app.mqtt.credentials import (  # noqa: E402
    generate_password,
    upsert_credential,
    write_password_file,
)

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
        self.broker_conf: Optional[Path] = None
        # Broker 已关闭匿名：E2E 自建临时凭证，绝不使用匿名连接
        self.mqtt: Dict[str, str] = {}
        self.gateway_python: Optional[str] = None
        self.client: Optional[httpx.Client] = None
        self.token = ""
        self.device_id = f"e2e-{uuid.uuid4().hex[:8]}"
        # 第二个设备：专门用于验证跨设备 ACL 与凭证轮换，不参与业务链路
        self.probe_device_id = f"e2e-probe-{uuid.uuid4().hex[:8]}"
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


def provision_mqtt_credentials(ctx: Context) -> str:
    """为本次 E2E 生成独立 Broker 配置与凭证。

    - 网关：iot-gateway（跨设备订阅/下发指令）
    - 客户端：web-console（9001 只读）
    - 设备：deviceId（只能操作自己的子树）
    所有口令只写入临时目录与进程 env，不进入仓库。
    """
    if not MOSQUITTO_CONF.exists():
        raise RuntimeError(f"缺少 Broker 配置：{MOSQUITTO_CONF}")
    workdir = Path(tempfile.mkdtemp(prefix="vikings-e2e-broker-"))
    mosq_dir = workdir / "infra" / "mosquitto"
    (mosq_dir / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(MOSQUITTO_CONF, mosq_dir / "mosquitto.conf")
    for name in ("acl.device", "acl.client"):
        source = ROOT / "infra" / "mosquitto" / name
        if not source.exists():
            raise RuntimeError(f"缺少 ACL 文件：{source}")
        shutil.copy2(source, mosq_dir / name)

    password_file = mosq_dir / "passwd"
    write_password_file(password_file, {})
    ctx.mqtt = {
        "gateway": generate_password(),
        "web": generate_password(),
        "mobile": generate_password(),
        "device": generate_password(),
        "probe": generate_password(),
    }
    upsert_credential(password_file, "iot-gateway", ctx.mqtt["gateway"])
    upsert_credential(password_file, "web-console", ctx.mqtt["web"])
    upsert_credential(password_file, "mobile-console", ctx.mqtt["mobile"])
    upsert_credential(password_file, ctx.device_id, ctx.mqtt["device"])
    upsert_credential(password_file, ctx.probe_device_id, ctx.mqtt["probe"])

    ctx.broker_workdir = workdir
    ctx.broker_conf = mosq_dir / "mosquitto.conf"
    return (
        f"临时凭证已生成：gateway/web/mobile + device({ctx.device_id})"
        f" + probe({ctx.probe_device_id})；Broker 工作目录 {workdir}"
    )


def start_broker(ctx: Context) -> str:
    if ctx.mqtt and ctx.broker_conf is None:
        raise RuntimeError("未生成 Broker 凭证，拒绝启动匿名 Broker")
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
    if "per_listener_settings true" not in conf or "allow_anonymous true" in conf:
        raise RuntimeError("Broker 配置必须按监听禁用匿名（per_listener_settings + allow_anonymous false）")

    # E2E 必须独占 Broker：要用本次临时凭证/ACL，复用外部 Broker 无法保证一致
    if port_open(MQTT_PORT) or port_open(MQTT_WS_PORT):
        raise RuntimeError(
            "1883/9001 已被占用；E2E 需要独占 Broker 以加载临时凭证与 ACL，"
            "请先停止 start_all / 本机 Mosquitto 后重试"
        )

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
        [mosquitto, "-c", str(ctx.broker_conf)],
        # 配置使用相对路径（passwd/acl.*/data/），工作目录必须是配置所在目录
        cwd=str(ctx.broker_conf.parent),
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
        raise RuntimeError(
            "8000 已被占用；E2E 需要自起网关以加载本次临时 Broker 凭证与临时数据库，"
            "请先停止 start_all / 本机 uvicorn 后重试"
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "gateway.log"
    fd, db_path = tempfile.mkstemp(prefix="vikings-e2e-", suffix=".db")
    os.close(fd)
    ctx.temp_db = db_path

    env = os.environ.copy()
    broker_reload = (
        f"kill -HUP {ctx.broker_proc.pid}" if ctx.broker_proc else ""
    )
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "MQTT_HOST": MQTT_HOST,
            "MQTT_PORT": str(MQTT_PORT),
            "MQTT_USERNAME": "iot-gateway",
            "MQTT_PASSWORD": ctx.mqtt["gateway"],
            "MQTT_PASSWORD_FILE": str(
                (ctx.broker_workdir or Path()) / "infra" / "mosquitto" / "passwd"
            ),
            "MQTT_RELOAD_COMMAND": broker_reload,
            "MQTT_WEB_USERNAME": "web-console",
            "MQTT_WEB_PASSWORD": ctx.mqtt["web"],
            "MQTT_MOBILE_USERNAME": "mobile-console",
            "MQTT_MOBILE_PASSWORD": ctx.mqtt["mobile"],
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
    def __init__(self, client_id: str, username: str, password: str) -> None:
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
        # 9001 只读主体：能订阅设备 Topic，但 ACL 无任何 write 规则
        self.client.username_pw_set(username, password)
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
                ("device/system/group", 1),
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


class _MqttProbe:
    """最小 MQTT 探针：记录 CONNACK/SUBACK 与收到的消息，用于真实鉴权断言。"""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = "",
        *,
        transport: str = "tcp",
        client_id: Optional[str] = None,
    ) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.rc: Optional[int] = None
        self.connected = False
        self.granted: List[int] = []
        self.transport = transport
        self._result = threading.Event()
        self._subacked = threading.Event()
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id or f"e2e-auth-{uuid.uuid4().hex[:8]}",
            transport=transport,
            protocol=mqtt.MQTTv311,
        )
        if transport == "websockets":
            self.client.ws_set_options(path="/mqtt")
        if username is not None:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.rc = int(getattr(reason_code, "value", reason_code))
        self.connected = not reason_code.is_failure
        self._result.set()

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        self.granted = list(granted_qos)
        self._subacked.set()

    def _on_message(self, client, userdata, msg):
        self.messages.append({"topic": msg.topic, "payload": msg.payload.decode("utf-8", "replace")})

    def connect(self, *, timeout: float = 6.0) -> int:
        port = MQTT_WS_PORT if self.transport == "websockets" else MQTT_PORT
        self.client.connect(MQTT_HOST, port, keepalive=15)
        self.client.loop_start()
        if not self._result.wait(timeout):
            raise RuntimeError(f"MQTT 探针 {timeout}s 内未收到 CONNACK")
        return int(self.rc or 0)

    def subscribe(self, topic: str, *, timeout: float = 6.0) -> List[int]:
        self._subacked.clear()
        self.client.subscribe(topic, qos=1)
        if not self._subacked.wait(timeout):
            raise RuntimeError(f"订阅 {topic} 后 {timeout}s 内未收到 SUBACK")
        return self.granted

    def publish(self, topic: str, payload: str, *, qos: int = 0) -> None:
        self.client.publish(topic, payload, qos=qos)

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001 - 清理阶段不再抛出
            pass


def _connect_rc(username: Optional[str], password: Optional[str], transport: str) -> int:
    probe = _MqttProbe(username, password, transport=transport)
    try:
        return probe.connect()
    finally:
        probe.close()


def verify_mqtt_auth_isolation(ctx: Context) -> str:
    """真实 Broker 鉴权验收：匿名拒绝 / 跨设备 ACL / 客户端只读 / 轮换失效。"""
    if ctx.broker_proc is None:
        raise RuntimeError("E2E 必须托管 Broker 才能执行凭证轮换")
    assert ctx.broker_workdir is not None

    # 1) 匿名连接 1883 / 9001 必须被 CONNACK 拒绝
    # paho v2 在 MQTT 3.1.1 下把 CONNACK 5(Not authorized) 映射为 ReasonCode 135(0x87)
    rc_1883 = _connect_rc(None, None, "tcp")
    rc_9001 = _connect_rc(None, None, "websockets")
    if rc_1883 in (0,) or rc_1883 not in (5, 135):
        raise RuntimeError(f"匿名 TCP 1883 未被正确拒绝，CONNACK rc={rc_1883}")
    if rc_9001 in (0,) or rc_9001 not in (5, 135):
        raise RuntimeError(f"匿名 WebSocket 9001 未被正确拒绝，CONNACK rc={rc_9001}")

    device = _MqttProbe(
        ctx.probe_device_id, ctx.mqtt["probe"], client_id=f"auth-probe-{ctx.probe_device_id}"
    )
    probe_sub = _MqttProbe(
        ctx.device_id, ctx.mqtt["device"], client_id=f"auth-dev-{ctx.device_id}"
    )
    web = _MqttProbe("web-console", ctx.mqtt["web"], transport="websockets")
    mobile = _MqttProbe("mobile-console", ctx.mqtt["mobile"], transport="websockets")
    try:
        # 2) 设备使用自己的凭证：订阅自己的 cmd、发布自己的 report 均成功
        if device.connect() != 0:
            raise RuntimeError("设备凭证连接失败")
        device_granted = device.subscribe(f"device/{ctx.probe_device_id}/cmd")
        if 128 in device_granted:
            raise RuntimeError(f"设备无法订阅自己的 cmd，SUBACK={device_granted}")
        device.publish(
            f"device/{ctx.probe_device_id}/sensor/report",
            json.dumps({"deviceId": ctx.probe_device_id, "seq": 1, "ts": int(time.time() * 1000)}),
        )

        if probe_sub.connect() != 0:
            raise RuntimeError("probe 设备凭证连接失败")
        probe_granted = probe_sub.subscribe(f"device/{ctx.device_id}/sensor/report")
        if 128 in probe_granted:
            raise RuntimeError(f"probe 无法订阅自己的 report，SUBACK={probe_granted}")

        # 3) 设备 A 订阅设备 B 的 Topic：Mosquitto 对 ACL 拒绝的订阅返回
        #    SUBACK granted（协议允许），但消息必须在投递阶段被 ACL 过滤。
        cross_granted = device.subscribe(f"device/{ctx.device_id}/sensor/report")
        probe_sub.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps({"marker": "cross-read-probe"}),
        )
        time.sleep(1.2)
        if any("cross-read-probe" in m["payload"] for m in device.messages):
            raise RuntimeError("设备 A 越权订阅收到了设备 B 的上报，ACL 未生效")

        # 4) 设备 A 向设备 B 的 Topic 发布：B 收不到（Broker ACL 丢弃）
        device.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps({"marker": "cross-device-write"}),
        )
        time.sleep(1.2)
        if any("cross-device-write" in m["payload"] for m in probe_sub.messages):
            raise RuntimeError("设备 A 越权发布到了设备 B 的 Topic，且被 B 收到")

        # 5) Web 客户端：可订阅只读 Topic，但发布设备上报 Topic 被拒
        if web.connect() != 0:
            raise RuntimeError("web-console 只读凭证连接失败")
        web_granted = web.subscribe(f"device/{ctx.device_id}/sensor/report")
        if 128 in web_granted:
            raise RuntimeError(f"web-console 无法订阅只读 Topic，SUBACK={web_granted}")

        # 6) 移动端是独立只读主体：能订阅，不能发布
        if mobile.connect() != 0:
            raise RuntimeError("mobile-console 只读凭证连接失败")
        mobile_granted = mobile.subscribe("device/+/sensor/report")
        if 128 in mobile_granted:
            raise RuntimeError(f"mobile-console 无法订阅只读 Topic，SUBACK={mobile_granted}")

        # 设备 B 正常上报：网关、Web、移动端都应收到，设备 A 不得收到
        probe_sub.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps({"marker": "read-allowed"}),
        )
        time.sleep(1.2)
        if not any("read-allowed" in m["payload"] for m in web.messages):
            raise RuntimeError("web-console 未收到只读 Topic 消息")
        if not any("read-allowed" in m["payload"] for m in mobile.messages):
            raise RuntimeError("mobile-console 未收到只读 Topic 消息")
        if any("read-allowed" in m["payload"] for m in device.messages):
            raise RuntimeError("设备 A 收到了设备 B 的只读 Topic 消息")

        web.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps({"marker": "web-write-denied"}),
        )
        mobile.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps({"marker": "mobile-write-denied"}),
        )
        time.sleep(1.2)
        if any(
            "web-write-denied" in m["payload"] or "mobile-write-denied" in m["payload"]
            for m in probe_sub.messages
        ):
            raise RuntimeError("客户端只读身份越权发布设备上报 Topic，且被设备收到")

        # 7) 轮换 probe 设备凭证：SIGHUP 后旧凭证失败、新凭证可用
        password_file = ctx.broker_workdir / "infra" / "mosquitto" / "passwd"
        state_file = ctx.broker_workdir / "e2e-credentials.json"
        rotated = subprocess.run(
            [
                gateway_python(ctx),
                "scripts/mqtt_credentials.py",
                "--password-file",
                str(password_file),
                "--state-file",
                str(state_file),
                "rotate",
                "--subject",
                f"device:{ctx.probe_device_id}",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if rotated.returncode != 0:
            raise RuntimeError(f"凭证轮换脚本失败：{rotated.stderr.strip()}")
        new_password = str(json.loads(rotated.stdout)["password"])
        os.kill(ctx.broker_proc.pid, signal.SIGHUP)
        time.sleep(0.8)

        old_rc = _connect_rc(ctx.probe_device_id, ctx.mqtt["probe"], "tcp")
        new_rc = _connect_rc(ctx.probe_device_id, new_password, "tcp")
        if old_rc == 0:
            raise RuntimeError(f"轮换后旧凭证仍可连接，CONNACK rc={old_rc}")
        if new_rc != 0:
            raise RuntimeError(f"轮换后新凭证连接失败，CONNACK rc={new_rc}")
        ctx.mqtt["probe"] = new_password
    finally:
        for probe in (device, probe_sub, web, mobile):
            probe.close()

    return (
        f"匿名 1883/9001 均被拒(rc={rc_1883}/{rc_9001})；"
        f"设备A订阅B的SUBACK={cross_granted}；A→B发布未被B收到；"
        f"web/mobile可订阅(SUBACK={web_granted}/{mobile_granted})但发布被拒；"
        f"轮换后旧凭证rc={old_rc}、新凭证rc={new_rc}"
    )


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
            "--username",
            ctx.device_id,
            "--password",
            ctx.mqtt["device"],
        ],
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return f"模拟器启动 device={ctx.device_id}（voltage=2.90 触发 critical 告警）"


def verify_realtime_report(ctx: Context) -> str:
    assert ctx.collector is not None
    # 只统计真正的设备上报（带 seq/ts）。鉴权隔离步骤也会向同一 Topic 发送
    # marker 探针消息，避免误取无 ts 的探针报文导致延迟计算失真。
    item = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/sensor/report"
        and int(m["payload"].get("seq", 0)) >= 1
        and int(m["payload"].get("ts", 0)) > 0,
        12,
        "设备实时上报",
    )
    lag_ms = int(item["received"] * 1000) - int(item["payload"]["ts"])
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


def verify_alert_center_bulk_ack(ctx: Context) -> str:
    """告警中心验收：真实上报产生新告警 → REST 筛选可见 → 批量确认后 open 不再返回。

    用湿度 99（critical 阈值 97）触发一条与既有电压告警不同 metric 的新告警，
    避免命中规则引擎的同设备同指标抑制窗口。批量确认故意混入一个不存在的 ID，
    验证部分失败在真实链路上同样逐类可见。
    """
    assert ctx.collector is not None
    seq = 8888
    publisher = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"e2e-alert-{uuid.uuid4().hex[:6]}",
        transport="tcp",
        protocol=mqtt.MQTTv311,
    )
    publisher.username_pw_set(ctx.device_id, ctx.mqtt["device"])
    publisher.connect(MQTT_HOST, MQTT_PORT, keepalive=15)
    publisher.loop_start()
    try:
        publisher.publish(
            f"device/{ctx.device_id}/sensor/report",
            json.dumps(
                {
                    "deviceId": ctx.device_id,
                    "temperature": 24.0,
                    "humidity": 99.0,
                    "voltage": 3.75,
                    "rssi": -60,
                    "battery": 80,
                    "seq": seq,
                    "ts": int(time.time() * 1000),
                }
            ),
            qos=1,
        ).wait_for_publish(timeout=5)
    finally:
        publisher.loop_stop()
        publisher.disconnect()

    item = ctx.collector.wait_message(
        lambda m: m["topic"] == "device/system/alert"
        and m["payload"].get("deviceId") == ctx.device_id
        and m["payload"].get("metric") == "humidity",
        15,
        "告警中心可用的新告警",
    )
    new_alert_id = item["payload"].get("alertId")
    if not new_alert_id:
        raise RuntimeError(f"湿度告警缺少 alertId：{item['payload']}")

    # 告警中心列表：deviceId + level + status + 分页参数，等待入库后可见
    deadline = time.monotonic() + 10
    visible: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        page = api(
            ctx,
            "GET",
            "/api/alerts",
            params={
                "deviceId": ctx.device_id,
                "level": "critical",
                "status": "open",
                "page": 1,
                "pageSize": 5,
            },
        )
        rows = page.get("items") or []
        visible = next((row for row in rows if row.get("alert_id") == new_alert_id), None)
        if visible is not None:
            break
        time.sleep(0.3)
    if visible is None:
        raise RuntimeError(f"告警中心筛选未返回新告警 {new_alert_id}")
    if int(page.get("total", 0)) < 1:
        raise RuntimeError(f"告警中心筛选 total 异常：{page}")

    missing_id = f"alt-e2e-missing-{uuid.uuid4().hex[:6]}"
    result = api(
        ctx,
        "POST",
        "/api/alerts/bulk-ack",
        json={"alert_ids": [new_alert_id, missing_id]},
    )
    if result.get("acked") != [new_alert_id]:
        raise RuntimeError(f"批量确认 acked 不符合预期：{result}")
    if result.get("not_found") != [missing_id]:
        raise RuntimeError(f"批量确认未把不存在 ID 暴露为 not_found：{result}")
    if result.get("requested") != 2:
        raise RuntimeError(f"批量确认 requested 不符合预期：{result}")

    open_data = api(
        ctx,
        "GET",
        "/api/alerts",
        params={"deviceId": ctx.device_id, "status": "open", "pageSize": 50},
    )
    open_ids = [row["alert_id"] for row in open_data.get("items") or []]
    if new_alert_id in open_ids:
        raise RuntimeError("批量确认后告警仍出现在 open 列表")
    acked_data = api(
        ctx,
        "GET",
        "/api/alerts",
        params={
            "deviceId": ctx.device_id,
            "level": "critical",
            "status": "acked",
            "pageSize": 50,
        },
    )
    acked_ids = [row["alert_id"] for row in acked_data.get("items") or []]
    if new_alert_id not in acked_ids:
        raise RuntimeError("批量确认后告警未出现在 acked 列表")
    return (
        f"新告警 {new_alert_id}（humidity）经筛选可见；批量确认 acked=1、"
        f"not_found=1（部分失败可见）；open 不再返回，acked 可查"
    )


def verify_group_change(ctx: Context) -> str:
    """改分组 → WS 收到 device/system/group → 设备详情与分组概览口径一致。"""
    assert ctx.collector is not None
    detail = api(ctx, "GET", f"/api/devices/{ctx.device_id}")
    previous = detail.get("group_name") or ""
    target = "E2E验收组"
    if previous == target:
        target = "E2E验收组2"

    groups_before = api(ctx, "GET", "/api/devices/groups")
    total_before = sum(int(item.get("total", 0)) for item in groups_before)

    updated = api(
        ctx,
        "PATCH",
        f"/api/devices/{ctx.device_id}",
        json={"group_name": target},
    )
    if updated.get("group_name") != target:
        raise RuntimeError(f"PATCH 后设备分组未更新：{updated.get('group_name')}")

    item = ctx.collector.wait_message(
        lambda m: m["topic"] == "device/system/group"
        and m["payload"].get("deviceId") == ctx.device_id,
        10,
        "设备分组变更事件",
    )
    payload = item["payload"]
    if payload.get("groupName") != target:
        raise RuntimeError(f"事件 groupName 与 REST 不一致：{payload}")
    if payload.get("previousGroupName") != previous:
        raise RuntimeError(f"事件 previousGroupName 不符合预期：{payload}")
    if payload.get("operator") != "admin":
        raise RuntimeError(f"事件 operator 不符合预期：{payload}")
    if not isinstance(payload.get("ts"), int):
        raise RuntimeError(f"事件 ts 不是毫秒整数：{payload}")

    detail_after = api(ctx, "GET", f"/api/devices/{ctx.device_id}")
    if detail_after.get("group_name") != target:
        raise RuntimeError(f"REST 设备详情分组不一致：{detail_after.get('group_name')}")

    groups_after = api(ctx, "GET", "/api/devices/groups")
    target_bucket = next(
        (g for g in groups_after if g.get("group_name") == target), None
    )
    if target_bucket is None or int(target_bucket.get("total", 0)) < 1:
        raise RuntimeError(f"分组概览未统计新分组：{groups_after}")
    total_after = sum(int(g.get("total", 0)) for g in groups_after)
    if total_after != total_before:
        raise RuntimeError(
            f"分组变更后设备总数不一致：{total_before} → {total_after}"
        )
    return (
        f"分组 {previous or '未分组'} → {target}；WebSocket 收到 device/system/group "
        f"（operator={payload.get('operator')}）；REST 设备详情与分组概览一致，"
        f"总设备 {total_after} 台"
    )


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
    publisher.username_pw_set(ctx.device_id, ctx.mqtt["device"])
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
        rep.run("mqtt_credentials", lambda: provision_mqtt_credentials(ctx))
        rep.run("broker", lambda: start_broker(ctx))
        rep.run("gateway", lambda: start_gateway(ctx))
        ctx.client = httpx.Client(base_url=GATEWAY_URL, timeout=10)
        rep.run("auth_login", lambda: login(ctx))
        ctx.collector = WsCollector(
            f"e2e-ws-{uuid.uuid4().hex[:8]}",
            "web-console",
            ctx.mqtt["web"],
        )
        rep.run("ws_connected", lambda: connect_ws(ctx))
        rep.run("mqtt_auth_isolation", lambda: verify_mqtt_auth_isolation(ctx))
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
        rep.run("alert_center_bulk_ack", lambda: verify_alert_center_bulk_ack(ctx))
        rep.run("group_change", lambda: verify_group_change(ctx))
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
    if "per_listener_settings true" not in conf:
        raise RuntimeError("Broker 配置缺少 per_listener_settings true（1883/9001 需独立 ACL）")
    if "allow_anonymous true" in conf:
        raise RuntimeError("Broker 配置仍存在 allow_anonymous true；拒绝匿名接入")
    if conf.count("allow_anonymous false") < 2:
        raise RuntimeError("Broker 必须对 1883 与 9001 两个监听都显式 allow_anonymous false")
    ports = {p: port_open(p) for p in (1883, 9001, 8000, 5173)}
    return (
        f"mosquitto/{Path(interpreter).name} 可用；端口状态 "
        + ", ".join(f"{p}={'占用' if v else '空闲'}" for p, v in ports.items())
        + f"；将使用临时库与临时 Broker 数据目录，deviceId={ctx.device_id}"
    )


if __name__ == "__main__":
    sys.exit(main())
