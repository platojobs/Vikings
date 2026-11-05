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
import sqlite3
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
        # ---- P2-4 / v1.24.0：短 TTL access + 可吊销 refresh ----
        self.refresh_token = ""
        self.access_refresh_count = 0
        self.device_id = f"e2e-{uuid.uuid4().hex[:8]}"
        # 第二个设备：专门用于验证跨设备 ACL 与凭证轮换，不参与业务链路
        self.probe_device_id = f"e2e-probe-{uuid.uuid4().hex[:8]}"
        self.collector: Optional[WsCollector] = None
        self.alert_id: Optional[str] = None
        self.cmd_id: Optional[str] = None
        self.ack_seen: Optional[Dict[str, Any]] = None
        self.interrupted = False
        # ---- P1-7 / v1.23.0：OTA 全链路与故障注入 ----
        # 网关重启必须复用同一个临时库/同一份 env，才符合"网关重启而非换环境"的语义
        self.gateway_env: Optional[Dict[str, str]] = None
        self.ota_task_id: Optional[str] = None
        self.ota_cmd_id: Optional[str] = None
        self.ota_ack_states: List[str] = []
        self.restart_device_id = f"e2e-restart-{uuid.uuid4().hex[:8]}"
        self.fail_device_id = f"e2e-fail-{uuid.uuid4().hex[:8]}"
        self.reconnect_device_id = f"e2e-reconnect-{uuid.uuid4().hex[:8]}"
        self.silent_device_id = f"e2e-silent-{uuid.uuid4().hex[:8]}"
        self.restart_task_id: Optional[str] = None
        self.restart_cmd_id: Optional[str] = None
        self.restart_probe: Any = None
        self.probes: List[Any] = []
        self.db_lock_conn: Optional[sqlite3.Connection] = None


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
        # Mosquitto 对 world-readable 的 ACL 会告警，未来版本将拒绝加载
        os.chmod(mosq_dir / name, 0o600)

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


def start_gateway(ctx: Context, *, reuse_db: bool = False) -> str:
    if port_open(8000):
        raise RuntimeError(
            "8000 已被占用；E2E 需要自起网关以加载本次临时 Broker 凭证与临时数据库，"
            "请先停止 start_all / 本机 uvicorn 后重试"
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / ("gateway-restart.log" if reuse_db else "gateway.log")
    if reuse_db:
        if not ctx.temp_db or not ctx.gateway_env:
            raise RuntimeError("网关重启前缺少临时库或环境变量，拒绝凭空重启")
        db_path = ctx.temp_db
        env = ctx.gateway_env
    else:
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
                # 指令终态兜底：阈值要大于本场景 OTA 时长（含网关重启窗口），
                # 巡检周期压到 2s 让超时用例可控；超时用例另用回拨 updated_at 复现
                "COMMAND_TIMEOUT_SECONDS": os.environ.get(
                    "E2E_COMMAND_TIMEOUT_SECONDS", "120"
                ),
                # 仅供"缺陷回归演示"等场景临时调大；默认 2s 保证超时用例 15s 内可判
                "COMMAND_SWEEP_INTERVAL_SECONDS": os.environ.get(
                    "E2E_COMMAND_SWEEP_INTERVAL_SECONDS", "2"
                ),
                "ADMIN_USERNAME": ctx.args.username,
                "ADMIN_PASSWORD": ctx.args.password,
                # v1.24.0：把 access TTL 压到 5s，让验收真实经历「access 过期 →
                # 自动 refresh → 原请求重放」；refresh TTL 仍为默认 14 天
                "ACCESS_TOKEN_TTL_SECONDS": os.environ.get(
                    "E2E_ACCESS_TOKEN_TTL_SECONDS", "5"
                ),
            }
        )
        ctx.gateway_env = env

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
                    action = "重启" if reuse_db else "启动"
                    return f"脚本{action}网关（临时 SQLite：{Path(db_path).name}）"
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        raise RuntimeError("网关 25s 内未就绪")
    except Exception as exc:
        raise RuntimeError(f"网关启动失败：{exc}\n{log_tail(log_path)}") from exc


def stop_gateway_process(ctx: Context) -> None:
    """停止网关进程但保留临时库与 env，供"重启后自愈"场景复用。"""
    proc = ctx.gateway_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    ctx.gateway_proc = None
    wait_port(8000, opened=False, timeout=10)


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
def refresh_access_token(ctx: Context) -> bool:
    """E2E 客户端侧自动续期：用 refresh 换新 access，成功返回 True。"""
    assert ctx.client is not None
    if not ctx.refresh_token:
        return False
    resp = ctx.client.post(
        "/api/auth/refresh", json={"refresh_token": ctx.refresh_token}
    )
    if resp.status_code != 200:
        return False
    data = (resp.json() or {}).get("data") or {}
    token = data.get("token")
    refresh_token = data.get("refresh_token")
    if not token or not refresh_token:
        return False
    ctx.token = str(token)
    ctx.refresh_token = str(refresh_token)
    ctx.access_refresh_count += 1
    return True


def api(ctx: Context, method: str, path: str, **kwargs: Any) -> Any:
    assert ctx.client is not None
    allow_refresh = kwargs.pop("_allow_refresh", True)
    headers = kwargs.pop("headers", {})
    if ctx.token:
        headers["Authorization"] = f"Bearer {ctx.token}"
    resp = ctx.client.request(method, path, headers=headers, **kwargs)
    if (
        resp.status_code == 401
        and allow_refresh
        and not path.startswith("/api/auth/")
        and refresh_access_token(ctx)
    ):
        # 与 Web/移动端一致：refresh 成功后用新 access 重放原请求一次
        return api(ctx, method, path, _allow_refresh=False, **kwargs)
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
    ctx.refresh_token = str(data.get("refresh_token") or "")
    if not ctx.refresh_token:
        raise RuntimeError("登录响应缺少 refresh_token（网关未启用 v1.24.0 认证生命周期）")
    role = (data.get("user") or {}).get("role")
    if role != "admin":
        raise RuntimeError(f"验收需要 admin 账号，当前 role={role}")
    return (
        f"admin 登录成功（access TTL={data.get('expires_in')}s、"
        f"token 长度 {len(ctx.token)}、refresh 长度 {len(ctx.refresh_token)}）"
    )


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

    def publish_json(self, topic: str, payload: Dict[str, Any], *, qos: int = 1) -> None:
        info = self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos)
        info.wait_for_publish(timeout=5)

    def wait_message(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        timeout: float,
        description: str,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in list(self.messages):
                try:
                    payload = json.loads(item["payload"])
                except (ValueError, TypeError):
                    continue
                if predicate({"topic": item["topic"], "payload": payload}):
                    return {"topic": item["topic"], "payload": payload}
            time.sleep(0.05)
        raise TimeoutError(f"{timeout}s 内未收到{description}")

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


def verify_credential_rotation_api(ctx: Context) -> str:
    """真实 Broker 上通过管理端点轮换凭证：旧凭证被拒、新凭证可用，并记录设备状态。"""
    device_id = f"e2e-cred-{uuid.uuid4().hex[:8]}"
    initial_password = _create_device_with_credentials(ctx, device_id, "凭证轮换验收设备")
    first_probe = _open_device_probe(ctx, device_id, initial_password)
    try:
        before = _wait_device_status(ctx, device_id, "online", 8)

        rotation = api(ctx, "POST", f"/api/devices/{device_id}/mqtt-credential")
        if not rotation.get("reloaded"):
            raise RuntimeError(
                f"轮换后 Broker 未重载：{rotation.get('reload_detail')}"
            )
        if rotation.get("operation") != "rotate":
            raise RuntimeError(f"凭证操作类型不正确：{rotation.get('operation')}")
        new_password = str(rotation.get("password") or "")
        if not new_password or new_password == initial_password:
            raise RuntimeError("轮换响应未返回新口令")

        # 不弱化既有断言：旧凭证必须被真实 Broker 拒绝，新凭证必须能连上
        old_rc = _connect_rc(device_id, initial_password, "tcp")
        new_rc = _connect_rc(device_id, new_password, "tcp")
        if old_rc == 0:
            raise RuntimeError(f"轮换后旧凭证仍可连接，CONNACK rc={old_rc}")
        if new_rc != 0:
            raise RuntimeError(f"轮换后新凭证连接失败，CONNACK rc={new_rc}")
        # 已建立的旧会话不会因密码文件重载被强制断开（这是 Broker 的真实语义），
        # 因此这里记录「轮换前 online → 旧会话关闭 → 新凭证重新上线」。
        first_probe.close()
        second_probe = _open_device_probe(ctx, device_id, new_password)
        after = _wait_device_status(ctx, device_id, "online", 8)

        detail = api(ctx, "GET", f"/api/devices/{device_id}")
        if "password" in detail:
            raise RuntimeError("设备详情泄露了凭证口令")
        prefix = detail.get("mqtt_credential_fingerprint_prefix")
        if not prefix or len(prefix) != 8:
            raise RuntimeError(f"设备详情未返回 8 位指纹前缀：{prefix}")
        if detail.get("mqtt_credential_status") != "active":
            raise RuntimeError(f"轮换后凭证状态不正确：{detail.get('mqtt_credential_status')}")

        audit = api(
            ctx,
            "GET",
            "/api/audit-logs",
            params={"action": "device.credential_issue", "page": 1, "pageSize": 50},
        )
        rows = [
            row for row in (audit.get("items") or [])
            if row.get("target_id") == device_id
            and (row.get("detail") or {}).get("operation") == "rotate"
        ]
        if not rows:
            raise RuntimeError(f"审计日志缺少 {device_id} 的轮换记录")
        rotation_detail = rows[0].get("detail") or {}
        if rotation_detail.get("result") != "reload_ok" or not rotation_detail.get("reloaded"):
            raise RuntimeError(f"轮换审计未记录重载结果：{rows[0]}")
        if new_password in json.dumps(rows, ensure_ascii=False):
            raise RuntimeError("审计日志泄露了凭证口令")
        second_probe.close()
        api(ctx, "DELETE", f"/api/devices/{device_id}")
        return (
            f"轮换前设备 {before.get('status')}；旧凭证 rc={old_rc} 被拒、"
            f"新凭证 rc={new_rc} 可用；轮换后设备 {after.get('status')}；"
            f"详情仅暴露指纹前缀 {prefix}…，审计 result={rotation_detail.get('result')}"
        )
    finally:
        first_probe.close()


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
            # 6 轮覆盖后续 OTA 窗口（含 2s 休眠），避免测试步骤增加后模拟器提前退出
            "--count",
            "6",
            "--interval",
            "2",
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
    single_note = "E2E 单条确认：已远程检查设备"
    acked = api(
        ctx,
        "POST",
        f"/api/alerts/{ctx.alert_id}/ack",
        json={"note": single_note},
    )
    if acked.get("status") != "acked":
        raise RuntimeError(f"确认后状态不是 acked：{acked}")
    if acked.get("acked_by") != ctx.args.username:
        raise RuntimeError(f"acked_by 未记录真实操作人：{acked}")
    if acked.get("ack_note") != single_note:
        raise RuntimeError(f"单条确认备注未落库：{acked}")
    if not acked.get("acked_at"):
        raise RuntimeError(f"单条确认缺少 acked_at：{acked}")
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
    return (
        f"告警 {ctx.alert_id} 已 acked，记录 acked_by={acked.get('acked_by')}、"
        f"acked_at、备注；open 列表不再返回，acked 列表可查"
    )


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
    bulk_note = "E2E 批量确认：湿度越限，已派单检查"
    result = api(
        ctx,
        "POST",
        "/api/alerts/bulk-ack",
        json={
            "alert_ids": [new_alert_id, missing_id],
            "note": bulk_note,
        },
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
    acked_rows = {
        row["alert_id"]: row for row in acked_data.get("items") or []
    }
    row = acked_rows.get(new_alert_id)
    if row is None:
        raise RuntimeError("批量确认后告警未出现在 acked 列表")
    if row.get("acked_by") != ctx.args.username:
        raise RuntimeError(f"批量确认 acked_by 未记录真实操作人：{row}")
    if row.get("ack_note") != bulk_note:
        raise RuntimeError(f"批量确认备注未落库：{row}")
    if not row.get("acked_at"):
        raise RuntimeError(f"批量确认缺少 acked_at：{row}")

    # 旧客户端/重复提交不带备注：幂等，且不覆盖首次处置信息
    retry = api(
        ctx,
        "POST",
        "/api/alerts/bulk-ack",
        json={"alert_ids": [new_alert_id]},
    )
    if retry.get("already_acked") != [new_alert_id]:
        raise RuntimeError(f"重复批量确认未按 already_acked 返回：{retry}")
    refreshed = api(
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
    refreshed_row = next(
        (
            item
            for item in refreshed.get("items") or []
            if item["alert_id"] == new_alert_id
        ),
        None,
    )
    if refreshed_row is None:
        raise RuntimeError("重复确认后告警从 acked 列表消失")
    if refreshed_row.get("ack_note") != bulk_note:
        raise RuntimeError(
            f"不带备注的重复确认覆盖了首次备注：{refreshed_row}"
        )
    return (
        f"新告警 {new_alert_id}（humidity）经筛选可见；批量确认 acked=1、"
        f"not_found=1（部分失败可见）；acked_by={row.get('acked_by')}、"
        f"备注与 acked_at 已记录；旧客户端重试不覆盖处置信息；"
        f"open 不再返回，acked 可查"
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


# ---------------------------------------------------------------------------
# P1-7 / v1.23.0：OTA 全链路与故障注入
# ---------------------------------------------------------------------------
def _register_device(ctx: Context, device_id: str, name: str, firmware: str = "1.0.0") -> None:
    api(
        ctx,
        "POST",
        "/api/devices",
        json={
            "device_id": device_id,
            "name": name,
            "model": "VG-S100",
            "firmware_version": firmware,
            "group_name": "E2E验收组",
        },
    )


def _create_device_with_credentials(
    ctx: Context, device_id: str, name: str, firmware: str = "1.0.0"
) -> str:
    """REST 注册设备并真实签发 MQTT 凭证（顺带覆盖签发/热加载链路）。"""
    _register_device(ctx, device_id, name, firmware)
    credential = api(ctx, "POST", f"/api/devices/{device_id}/mqtt-credential")
    if not credential.get("reloaded"):
        raise RuntimeError(
            f"设备 {device_id} 凭证未热加载：{credential.get('reload_detail')}"
        )
    return str(credential["password"])


def _open_device_probe(
    ctx: Context,
    device_id: str,
    password: str,
    *,
    publish_online: bool = True,
    client_id: Optional[str] = None,
    firmware: str = "1.0.0",
) -> Any:
    probe = _MqttProbe(
        device_id,
        password,
        client_id=client_id or f"e2e-dev-{device_id}",
    )
    ctx.probes.append(probe)
    try:
        rc = probe.connect()
        if rc != 0:
            raise RuntimeError(f"设备 {device_id} MQTT 连接失败，CONNACK rc={rc}")
        granted = probe.subscribe(f"device/{device_id}/cmd")
        if 128 in granted:
            raise RuntimeError(f"设备 {device_id} 无法订阅自己的 cmd，SUBACK={granted}")
        if publish_online:
            probe.publish_json(
                f"device/{device_id}/status",
                {
                    "deviceId": device_id,
                    "status": "online",
                    "firmwareVersion": firmware,
                    "reason": "e2e",
                    "ts": int(time.time() * 1000),
                },
            )
        return probe
    except Exception:
        probe.close()
        raise


def _wait_device_status(
    ctx: Context, device_id: str, expected: str, timeout: float = 10
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    detail: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        detail = api(ctx, "GET", f"/api/devices/{device_id}")
        if detail.get("status") == expected:
            return detail
        time.sleep(0.2)
    raise RuntimeError(
        f"设备 {device_id} 在 {timeout}s 内未变为 {expected}，最近状态={detail}"
    )


def _create_ota_task(ctx: Context, device_id: str, target: str, description: str) -> Dict[str, Any]:
    return api(
        ctx,
        "POST",
        "/api/ota/tasks",
        json={
            "deviceIds": [device_id],
            "targetVersion": target,
            "url": f"https://updates.example.com/vikings-{target}.bin",
            "description": description,
        },
    )


def _wait_ota_task(
    ctx: Context,
    task_id: str,
    predicate: Callable[[Dict[str, Any]], bool],
    timeout: float,
    description: str,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    detail: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = api(ctx, "GET", f"/api/ota/tasks/{task_id}")
        if predicate(detail):
            return detail
        time.sleep(0.2)
    raise RuntimeError(f"{timeout}s 内未达成{description}，最近任务详情={detail}")


def _ota_ack_payload(
    cmd_id: str, state: str, progress: int, message: str
) -> Dict[str, Any]:
    return {
        "cmdId": cmd_id,
        "type": "ota",
        "state": state,
        "progress": progress,
        "message": message,
        "ts": int(time.time() * 1000),
    }


def verify_ota_full_chain(ctx: Context) -> str:
    """OTA 主链路：创建任务 → 网关下发 → 模拟设备分片进度 → success → REST 一致。"""
    assert ctx.collector is not None
    target = "1.1.0"
    created = _create_ota_task(ctx, ctx.device_id, target, "E2E OTA 全链路")
    task_id = str(created.get("task_id") or "")
    devices = created.get("devices") or []
    if not task_id or len(devices) != 1:
        raise RuntimeError(f"OTA 任务创建响应异常：{created}")
    if int(created.get("total", 0)) != 1:
        raise RuntimeError(f"OTA 任务 total 异常：{created}")
    if int(created.get("success", 0)) != 0 or int(created.get("failed", 0)) != 0:
        raise RuntimeError(f"OTA 任务创建时不应已有终态：{created}")
    cmd_id = str(devices[0].get("cmd_id") or "")
    ctx.ota_task_id = task_id
    ctx.ota_cmd_id = cmd_id

    received = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/cmd/ack"
        and m["payload"].get("cmdId") == cmd_id
        and m["payload"].get("state") == "received",
        12,
        "OTA received 回执",
    )
    success = ctx.collector.wait_message(
        lambda m: m["topic"] == f"device/{ctx.device_id}/cmd/ack"
        and m["payload"].get("cmdId") == cmd_id
        and m["payload"].get("state") == "success",
        20,
        "OTA success 回执",
    )
    acks = [
        m["payload"]
        for m in list(ctx.collector.messages)
        if m["topic"] == f"device/{ctx.device_id}/cmd/ack"
        and m["payload"].get("cmdId") == cmd_id
    ]
    progress_values = [
        int(ack.get("progress", 0)) for ack in acks if ack.get("state") == "progress"
    ]
    if len(progress_values) < 2:
        raise RuntimeError(f"设备未回报足够的分片进度：{progress_values}")
    if progress_values != sorted(progress_values):
        raise RuntimeError(f"OTA 进度回退或乱序：{progress_values}")
    if int(success["payload"].get("progress", 0)) != 100:
        raise RuntimeError(f"OTA success 回执 progress 不是 100：{success['payload']}")
    ctx.ota_ack_states = [ack.get("state", "") for ack in acks]

    detail = _wait_ota_task(
        ctx,
        task_id,
        lambda d: int(d.get("success", 0)) == 1
        and int(d.get("failed", 0)) == 0
        and (d.get("devices") or [{}])[0].get("state") == "success"
        and int((d.get("devices") or [{}])[0].get("progress", 0)) == 100,
        20,
        "OTA 任务 success=1 且进度=100",
    )
    device = api(ctx, "GET", f"/api/devices/{ctx.device_id}")
    if device.get("firmware_version") != target:
        raise RuntimeError(
            f"OTA 成功后设备固件版本未更新：{device.get('firmware_version')} != {target}"
        )
    return (
        f"任务 {task_id} 真实下发到 {ctx.device_id}；设备回执 "
        f"received→progress{progress_values}→success(100)；"
        f"REST 任务 success=1 failed=0，固件已更新为 {target}"
    )


def verify_ota_audit(ctx: Context) -> str:
    if not ctx.ota_task_id:
        raise RuntimeError("OTA 主链路未记录 task_id，无法校验审计")
    page = api(
        ctx,
        "GET",
        "/api/audit-logs",
        params={"action": "ota.create", "page": 1, "pageSize": 50},
    )
    row = next(
        (
            item
            for item in page.get("items") or []
            if item.get("target_id") == ctx.ota_task_id
        ),
        None,
    )
    if row is None:
        raise RuntimeError(f"审计日志缺少 ota.create 记录：{ctx.ota_task_id}")
    if row.get("operator") != ctx.args.username:
        raise RuntimeError(f"OTA 审计操作人不正确：{row}")
    detail = row.get("detail") or {}
    if detail.get("targetVersion") != "1.1.0" or int(detail.get("deviceCount", 0)) != 1:
        raise RuntimeError(f"OTA 审计明细不完整：{row}")
    return (
        f"审计记录 action=ota.create target={ctx.ota_task_id} "
        f"operator={row.get('operator')} targetVersion={detail.get('targetVersion')}"
    )


def verify_ota_device_reported_failure(ctx: Context) -> str:
    """设备主动回执 failed：任务必须进入 failed 终态，不能永久 running。"""
    password = _create_device_with_credentials(ctx, ctx.fail_device_id, "OTA 失败设备")
    probe = _open_device_probe(ctx, ctx.fail_device_id, password, firmware="1.0.0")
    _wait_device_status(ctx, ctx.fail_device_id, "online", 8)
    target = "1.2.0"
    created = _create_ota_task(ctx, ctx.fail_device_id, target, "E2E OTA 设备失败路径")
    task_id = str(created.get("task_id") or "")
    cmd_id = str((created.get("devices") or [{}])[0].get("cmd_id") or "")
    if not task_id or not cmd_id:
        raise RuntimeError(f"失败路径 OTA 任务创建异常：{created}")

    probe.wait_message(
        lambda m: m["topic"] == f"device/{ctx.fail_device_id}/cmd"
        and m["payload"].get("cmdId") == cmd_id,
        10,
        "失败路径 OTA 指令",
    )
    probe.publish_json(
        f"device/{ctx.fail_device_id}/cmd/ack",
        _ota_ack_payload(cmd_id, "received", 0, "指令已接收"),
    )
    probe.publish_json(
        f"device/{ctx.fail_device_id}/cmd/ack",
        _ota_ack_payload(cmd_id, "failed", 40, "固件校验失败（设备侧）"),
    )
    detail = _wait_ota_task(
        ctx,
        task_id,
        lambda d: int(d.get("failed", 0)) == 1
        and int(d.get("success", 0)) == 0
        and (d.get("devices") or [{}])[0].get("state") == "failed",
        15,
        "OTA 任务 failed=1 终态",
    )
    device_row = (detail.get("devices") or [{}])[0]
    if "校验失败" not in str(device_row.get("message") or ""):
        raise RuntimeError(f"失败原因未保留：{device_row}")
    failed_device = api(ctx, "GET", f"/api/devices/{ctx.fail_device_id}")
    if failed_device.get("firmware_version") != "1.0.0":
        raise RuntimeError(f"OTA 失败却改写了固件版本：{failed_device.get('firmware_version')}")
    return (
        f"任务 {task_id} 由设备回执 failed(progress=40)：任务 failed=1 success=0，"
        f"设备状态={device_row.get('state')}，不会永久 running；"
        f"设备固件仍为 1.0.0，未被误标为 {target}"
    )


def verify_ota_timeout_terminal(ctx: Context) -> str:
    """无回执设备：超时巡检把 issued 指令判 failed，任务离开 running。

    为了不让 E2E 等满生产阈值，这里只回拨该指令的 updated_at（时间维度），
    巡检逻辑、SQL 过滤与汇总刷新都走真实后台协程。
    """
    _register_device(ctx, ctx.silent_device_id, "OTA 无回执设备")
    target = "1.4.0"
    created = _create_ota_task(ctx, ctx.silent_device_id, target, "E2E OTA 超时路径")
    task_id = str(created.get("task_id") or "")
    cmd_id = str((created.get("devices") or [{}])[0].get("cmd_id") or "")
    if not task_id or not cmd_id:
        raise RuntimeError(f"超时路径 OTA 任务创建异常：{created}")
    if not ctx.temp_db:
        raise RuntimeError("缺少临时库路径，无法回拨超时时钟")

    conn = sqlite3.connect(ctx.temp_db, timeout=5)
    try:
        updated = conn.execute(
            "UPDATE device_command SET updated_at='2000-01-01 00:00:00.000000' WHERE cmd_id=?",
            (cmd_id,),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"回拨超时时钟失败：cmd_id={cmd_id} rowcount={updated.rowcount}")
        conn.commit()
    finally:
        conn.close()

    detail = _wait_ota_task(
        ctx,
        task_id,
        lambda d: int(d.get("failed", 0)) == 1
        and int(d.get("success", 0)) == 0
        and (d.get("devices") or [{}])[0].get("state") == "failed",
        15,
        "超时巡检将 OTA 任务置为 failed",
    )
    device_row = (detail.get("devices") or [{}])[0]
    if "超时" not in str(device_row.get("message") or ""):
        raise RuntimeError(f"超时失败原因不可见：{device_row}")
    return (
        f"任务 {task_id} 下发到无回执设备后，后台巡检在 15s 内将指令置 failed；"
        f"任务 failed=1 success=0（不再永久 running），原因={device_row.get('message')}"
    )


def verify_device_reconnect(ctx: Context) -> str:
    """设备自身 MQTT 断连后重连（Broker 全程保持运行），重连后指令链路继续可用。"""
    password = _create_device_with_credentials(
        ctx, ctx.reconnect_device_id, "MQTT 重连设备"
    )
    probe = _open_device_probe(ctx, ctx.reconnect_device_id, password, firmware="1.0.0")
    _wait_device_status(ctx, ctx.reconnect_device_id, "online", 8)

    probe.publish_json(
        f"device/{ctx.reconnect_device_id}/status",
        {
            "deviceId": ctx.reconnect_device_id,
            "status": "offline",
            "firmwareVersion": "1.0.0",
            "reason": "device-sleep",
            "ts": int(time.time() * 1000),
        },
    )
    probe.close()
    _wait_device_status(ctx, ctx.reconnect_device_id, "offline", 8)
    if not port_open(MQTT_PORT):
        raise RuntimeError("设备断开期间 Broker 不应停机（本场景是设备侧重连）")

    reconnected = _open_device_probe(
        ctx,
        ctx.reconnect_device_id,
        password,
        client_id=f"e2e-dev-{ctx.reconnect_device_id}-2",
    )
    _wait_device_status(ctx, ctx.reconnect_device_id, "online", 10)
    if not gateway_mqtt_connected(ctx):
        raise RuntimeError("设备重连后网关 MQTT 状态不正确")

    command = api(
        ctx,
        "POST",
        f"/api/devices/{ctx.reconnect_device_id}/commands",
        json={"type": "config", "payload": {"reportIntervalSec": 15}},
    )
    cmd_id = str(command.get("cmd_id") or "")
    if not cmd_id:
        raise RuntimeError(f"重连后指令下发响应异常：{command}")
    reconnected.wait_message(
        lambda m: m["topic"] == f"device/{ctx.reconnect_device_id}/cmd"
        and m["payload"].get("cmdId") == cmd_id,
        10,
        "重连后的 config 指令",
    )
    reconnected.publish_json(
        f"device/{ctx.reconnect_device_id}/cmd/ack",
        {
            "cmdId": cmd_id,
            "type": "config",
            "state": "success",
            "progress": 100,
            "message": "重连后执行完成",
            "ts": int(time.time() * 1000),
        },
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = api(
            ctx,
            "GET",
            f"/api/devices/{ctx.reconnect_device_id}/commands",
            params={"limit": 20},
        )
        row = next((item for item in rows if item.get("cmd_id") == cmd_id), None)
        if row and row.get("state") == "success":
            return (
                "设备主动断开→网关 REST 状态 offline→设备用同一凭证重连并上线；"
                f"重连后下发 config cmd={cmd_id} 仍能收到并回执 success（Broker 未停机）"
            )
        time.sleep(0.3)
    raise RuntimeError("设备重连后指令未在 10s 内变为 success")


def ota_restart_prepare(ctx: Context) -> str:
    """网关重启场景前置：OTA 进行到 30%，设备与回执均已在库。"""
    password = _create_device_with_credentials(
        ctx, ctx.restart_device_id, "OTA 重启设备"
    )
    probe = _open_device_probe(ctx, ctx.restart_device_id, password, firmware="1.0.0")
    _wait_device_status(ctx, ctx.restart_device_id, "online", 8)
    ctx.restart_probe = probe

    target = "1.3.0"
    created = _create_ota_task(ctx, ctx.restart_device_id, target, "E2E OTA 网关重启")
    task_id = str(created.get("task_id") or "")
    cmd_id = str((created.get("devices") or [{}])[0].get("cmd_id") or "")
    if not task_id or not cmd_id:
        raise RuntimeError(f"重启场景 OTA 任务创建异常：{created}")
    ctx.restart_task_id = task_id
    ctx.restart_cmd_id = cmd_id

    probe.wait_message(
        lambda m: m["topic"] == f"device/{ctx.restart_device_id}/cmd"
        and m["payload"].get("cmdId") == cmd_id,
        10,
        "重启场景 OTA 指令",
    )
    probe.publish_json(
        f"device/{ctx.restart_device_id}/cmd/ack",
        _ota_ack_payload(cmd_id, "received", 0, "指令已接收"),
    )
    probe.publish_json(
        f"device/{ctx.restart_device_id}/cmd/ack",
        _ota_ack_payload(cmd_id, "progress", 30, "下载固件 30%"),
    )
    detail = _wait_ota_task(
        ctx,
        task_id,
        lambda d: (d.get("devices") or [{}])[0].get("state") == "progress"
        and int((d.get("devices") or [{}])[0].get("progress", 0)) == 30,
        10,
        "OTA 进度 30% 已落库",
    )
    return (
        f"任务 {task_id} 设备 {ctx.restart_device_id} 已回执 progress=30；"
        f"REST success={detail.get('success')} failed={detail.get('failed')}"
    )


def gateway_stop_mid_ota(ctx: Context) -> str:
    """在 OTA 进度 30% 时停网关；停机期间的设备回执/上报按非持久会话丢弃。"""
    if not ctx.restart_task_id or not ctx.restart_cmd_id or ctx.restart_probe is None:
        raise RuntimeError("网关重启场景未正确前置")
    detail = api(ctx, "GET", f"/api/ota/tasks/{ctx.restart_task_id}")
    row = (detail.get("devices") or [{}])[0]
    if int(row.get("progress", 0)) != 30 or int(detail.get("success", 0)) != 0:
        raise RuntimeError(f"停机前进度不是 30%：{detail}")

    stop_gateway_process(ctx)
    if ctx.gateway_proc is not None or port_open(8000):
        raise RuntimeError("网关进程未真正停止")

    # 网关离线期间设备继续推进并补发 success，同时上报 seq=7777
    ctx.restart_probe.publish_json(
        f"device/{ctx.restart_device_id}/cmd/ack",
        _ota_ack_payload(ctx.restart_cmd_id, "progress", 60, "下载固件 60%"),
    )
    ctx.restart_probe.publish_json(
        f"device/{ctx.restart_device_id}/cmd/ack",
        _ota_ack_payload(ctx.restart_cmd_id, "success", 100, "升级完成（停机期间）"),
    )
    ctx.restart_probe.publish_json(
        f"device/{ctx.restart_device_id}/sensor/report",
        {
            "deviceId": ctx.restart_device_id,
            "temperature": 24.0,
            "humidity": 55.0,
            "voltage": 3.75,
            "rssi": -60,
            "battery": 80,
            "seq": 7777,
            "ts": int(time.time() * 1000),
        },
    )
    time.sleep(1.0)
    return (
        "网关已在 OTA progress=30% 时停止；设备随后发布的 progress=60、success=100 "
        "与 seq=7777 上报发生在网关离线期，按 MQTT 非持久会话语义丢弃"
    )


def gateway_restart_recover(ctx: Context) -> str:
    """重启同一临时库上的网关：持久进度不丢、停机数据明确丢弃、恢复后链路自愈。"""
    if not ctx.restart_task_id or not ctx.restart_cmd_id or ctx.restart_probe is None:
        raise RuntimeError("网关重启场景未正确前置")
    start_gateway(ctx, reuse_db=True)
    wait_readiness(ctx, expected=True, timeout=25)

    persisted = api(ctx, "GET", f"/api/ota/tasks/{ctx.restart_task_id}")
    row = (persisted.get("devices") or [{}])[0]
    if int(row.get("progress", 0)) != 30 or row.get("state") != "progress":
        raise RuntimeError(f"重启后进度未持久化到 30%：{persisted}")
    if int(persisted.get("success", 0)) != 0 or int(persisted.get("failed", 0)) != 0:
        raise RuntimeError(f"停机期间的 success 回执不应落库：{persisted}")

    latest = api(ctx, "GET", "/api/records/latest")
    if any(item.get("device_id") == ctx.restart_device_id for item in latest):
        raise RuntimeError("网关停机期间的 seq=7777 上报不应落库")

    # 设备侧重发终态与上报，验证恢复后真正自愈
    ctx.restart_probe.publish_json(
        f"device/{ctx.restart_device_id}/cmd/ack",
        _ota_ack_payload(ctx.restart_cmd_id, "success", 100, "重启后补发成功"),
    )
    ctx.restart_probe.publish_json(
        f"device/{ctx.restart_device_id}/sensor/report",
        {
            "deviceId": ctx.restart_device_id,
            "temperature": 24.5,
            "humidity": 56.0,
            "voltage": 3.75,
            "rssi": -58,
            "battery": 81,
            "seq": 7778,
            "ts": int(time.time() * 1000),
        },
    )
    detail = _wait_ota_task(
        ctx,
        ctx.restart_task_id,
        lambda d: int(d.get("success", 0)) == 1
        and int(d.get("failed", 0)) == 0
        and (d.get("devices") or [{}])[0].get("state") == "success",
        12,
        "重启后 OTA 任务 success=1",
    )
    device = api(ctx, "GET", f"/api/devices/{ctx.restart_device_id}")
    if device.get("firmware_version") != "1.3.0":
        raise RuntimeError(f"重启后 OTA 成功未更新固件：{device.get('firmware_version')}")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = api(ctx, "GET", "/api/records/latest")
        if any(item.get("device_id") == ctx.restart_device_id for item in latest):
            return (
                "网关用同一临时库重启；OTA 进度保持 30%、停机期间 success/seq=7777 明确丢弃；"
                "恢复后设备补发 success 使任务转为 success=1，固件更新 1.3.0，"
                "seq=7778 上报重新入库（链路自愈）"
            )
        time.sleep(0.3)
    raise RuntimeError("恢复后设备上报未在 10s 内入库")


def wait_readiness(ctx: Context, *, expected: bool, timeout: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(GATEWAY_URL + "/api/health/ready", timeout=5)
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            time.sleep(0.3)
            continue
        ready = resp.status_code == 200
        if ready == expected:
            return body
        time.sleep(0.3)
    raise RuntimeError(
        f"/api/health/ready 未在 {timeout}s 内变为 {'ready' if expected else 'not_ready'}：{body}"
    )


def verify_db_outage_recovery(ctx: Context) -> str:
    """/ready 必须真实反映数据库不可用；/health 仍 200；恢复后继续服务。"""
    if not ctx.temp_db:
        raise RuntimeError("缺少临时库路径，无法注入数据库故障")
    if ctx.gateway_proc is None or ctx.gateway_proc.poll() is not None:
        raise RuntimeError("数据库故障注入前网关进程不存活")

    # 用独占锁让读者拿不到锁：这是 SQLite 下最接近"数据库短暂不可用"的真实故障
    lock = sqlite3.connect(ctx.temp_db, timeout=0)
    lock.execute("PRAGMA busy_timeout=0")
    acquired = False
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                lock.execute("BEGIN EXCLUSIVE")
                acquired = True
                break
            except sqlite3.OperationalError:
                time.sleep(0.2)
        if not acquired:
            raise RuntimeError("6s 内无法获取 SQLite 独占锁（后台任务占用过久）")
        ctx.db_lock_conn = lock

        health = httpx.get(GATEWAY_URL + "/api/health", timeout=5)
        if health.status_code != 200:
            raise RuntimeError(f"数据库不可用时 /api/health 未保持 200：{health.status_code}")
        if ctx.gateway_proc.poll() is not None:
            raise RuntimeError("数据库不可用导致网关进程退出")

        ready = httpx.get(GATEWAY_URL + "/api/health/ready", timeout=10)
        if ready.status_code != 503:
            raise RuntimeError(
                f"数据库不可用时 /api/health/ready 未返回 503：{ready.status_code} {ready.text}"
            )
        body = ready.json()
        database = ((body.get("data") or {}).get("components") or {}).get("database") or {}
        if database.get("ok") is not False:
            raise RuntimeError(f"/ready 未指出数据库失败：{database}")
        if not database.get("error"):
            raise RuntimeError(f"/ready 数据库失败缺少错误摘要：{database}")
    finally:
        try:
            lock.rollback()
        except sqlite3.Error:
            pass
        lock.close()
        ctx.db_lock_conn = None

    wait_readiness(ctx, expected=True, timeout=15)
    api(ctx, "GET", "/api/devices", params={"page": 1, "pageSize": 20})
    return (
        "数据库被独占锁住时：/api/health 仍 200（存活），/api/health/ready 返回 503 且 "
        "components.database.ok=false、error 可见，网关进程未退出；释放后 /ready 回 200，"
        "REST 设备列表继续可用"
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
AUTH_TEST_PASSWORD = "Session!Pass2026"
AUTH_TEST_PASSWORD_2 = "Changed!Pass2026"


def _login_as(ctx: Context, username: str, password: str) -> Dict[str, Any]:
    """独立账号登录，不覆盖 ctx.token（管理员会话仍需用于后续吊销操作）。"""
    assert ctx.client is not None
    resp = ctx.client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"账号 {username} 登录失败：HTTP {resp.status_code} {resp.text}")
    data = (resp.json() or {}).get("data") or {}
    if not data.get("token") or not data.get("refresh_token"):
        raise RuntimeError(f"账号 {username} 登录响应缺少 access/refresh")
    return data


def _refresh_code(ctx: Context, refresh_token: str) -> int:
    assert ctx.client is not None
    return ctx.client.post(
        "/api/auth/refresh", json={"refresh_token": refresh_token}
    ).status_code


def verify_auth_lifecycle(ctx: Context) -> str:
    """v1.24.0：真实走过 refresh 轮换、登出、管理员吊销、改密吊销。

    前置：access TTL 被压到 5s，主流程中出现过「401 → 自动 refresh → 重放」。
    """
    assert ctx.client is not None
    if ctx.access_refresh_count <= 0:
        raise RuntimeError("主流程未触发 access 过期自动续期，短 TTL 未生效")
    renewed = ctx.access_refresh_count

    username = f"e2e-auth-{uuid.uuid4().hex[:6]}"
    api(
        ctx,
        "POST",
        "/api/users",
        json={"username": username, "password": AUTH_TEST_PASSWORD, "role": "viewer"},
    )

    # 1) 管理员吊销：同一账号两个会话全部失效
    s1 = _login_as(ctx, username, AUTH_TEST_PASSWORD)
    s2 = _login_as(ctx, username, AUTH_TEST_PASSWORD)
    revoked = api(ctx, "POST", f"/api/users/{username}/sessions/revoke")
    if int(revoked.get("revoked") or 0) < 2:
        raise RuntimeError(f"管理员吊销条数不足：{revoked}")
    for item in (s1, s2):
        if _refresh_code(ctx, item["refresh_token"]) != 401:
            raise RuntimeError("管理员吊销后 refresh 仍可续期")

    # 2) 明确窗口：吊销 refresh 不会让已签发 access 立即失效
    me = ctx.client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {s1['token']}"}
    )
    if me.status_code != 200:
        raise RuntimeError(
            f"吊销 refresh 后 access 应立即返回 200（剩余 TTL 内仍有效），实际 {me.status_code}"
        )

    # 3) 轮换单次使用 + 重放检测
    s3 = _login_as(ctx, username, AUTH_TEST_PASSWORD)
    rotated = ctx.client.post(
        "/api/auth/refresh", json={"refresh_token": s3["refresh_token"]}
    )
    if rotated.status_code != 200:
        raise RuntimeError(f"正常 refresh 失败：HTTP {rotated.status_code}")
    new_refresh = ((rotated.json() or {}).get("data") or {}).get("refresh_token")
    if not new_refresh or new_refresh == s3["refresh_token"]:
        raise RuntimeError("refresh 未轮换（新令牌缺失或与旧令牌相同）")
    if _refresh_code(ctx, s3["refresh_token"]) != 401:
        raise RuntimeError("轮换后的旧 refresh 仍可用")
    if _refresh_code(ctx, new_refresh) != 401:
        raise RuntimeError("重放旧 refresh 后未按策略吊销该账号全部会话")

    # 4) 登出：当前 refresh 立即失效
    s4 = _login_as(ctx, username, AUTH_TEST_PASSWORD)
    out = ctx.client.post(
        "/api/auth/logout", json={"refresh_token": s4["refresh_token"]}
    )
    if out.status_code != 200 or not (out.json() or {}).get("data", {}).get("revoked"):
        raise RuntimeError(f"登出未吊销 refresh：HTTP {out.status_code} {out.text}")
    if _refresh_code(ctx, s4["refresh_token"]) != 401:
        raise RuntimeError("登出后 refresh 仍可续期")

    # 5) 改密：该用户全部 refresh 自动吊销
    s5 = _login_as(ctx, username, AUTH_TEST_PASSWORD)
    api(
        ctx,
        "PATCH",
        f"/api/users/{username}",
        json={"password": AUTH_TEST_PASSWORD_2},
    )
    if _refresh_code(ctx, s5["refresh_token"]) != 401:
        raise RuntimeError("改密后旧 refresh 仍可续期")

    api(ctx, "DELETE", f"/api/users/{username}")
    return (
        f"自动续期 {renewed} 次；管理员吊销 {revoked['revoked']} 条；"
        f"轮换/重放/登出/改密均按 401 拒绝；吊销后 access 剩余 TTL 内仍 200（已文档化）"
    )


def cleanup(ctx: Context) -> None:
    started = time.monotonic()
    notes: List[str] = []
    if ctx.db_lock_conn is not None:
        try:
            ctx.db_lock_conn.rollback()
            ctx.db_lock_conn.close()
            notes.append("数据库故障注入锁已释放")
        except sqlite3.Error:
            pass
        ctx.db_lock_conn = None
    for probe in ctx.probes:
        probe.close()
    if ctx.probes:
        notes.append(f"{len(ctx.probes)} 个设备探针已断开")
        ctx.probes.clear()
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
        rep.run("credential_rotation_api", lambda: verify_credential_rotation_api(ctx))
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
        # OTA 必须在模拟器唤醒窗口内下发（与 command 一样依赖 6s 监听窗口）
        rep.run("ota_window", lambda: wait_report_round(ctx, 3))
        rep.run("ota_full_chain", lambda: verify_ota_full_chain(ctx))
        rep.run("ota_audit", lambda: verify_ota_audit(ctx))
        rep.run("stop_simulator", lambda: stop_simulator(ctx))
        # 失败路径：设备主动回执 failed / 设备无回执触发超时终态
        rep.run("ota_device_failure", lambda: verify_ota_device_reported_failure(ctx))
        rep.run("ota_timeout_terminal", lambda: verify_ota_timeout_terminal(ctx))
        # 故障注入：设备侧重连（Broker 不停）、网关重启、数据库短暂不可用
        rep.run("device_reconnect", lambda: verify_device_reconnect(ctx))
        rep.run("ota_restart_prepare", lambda: ota_restart_prepare(ctx))
        rep.run("gateway_stop_mid_ota", lambda: gateway_stop_mid_ota(ctx))
        rep.run("gateway_restart_recover", lambda: gateway_restart_recover(ctx))
        rep.run("db_outage_recovery", lambda: verify_db_outage_recovery(ctx))
        rep.run("degrade_disconnect", lambda: degrade_disconnect(ctx))
        rep.run("degrade_polling_rest", lambda: degrade_polling_rest(ctx))
        rep.run("recover_broker", lambda: recover_broker(ctx))
        rep.run("recover_ws", lambda: recover_ws(ctx))
        rep.run("recover_real_data", lambda: recover_real_data(ctx))
        # 最后跑认证生命周期：会创建/删除临时账号并吊销会话，不影响前面业务链路
        rep.run("auth_token_lifecycle", lambda: verify_auth_lifecycle(ctx))
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
