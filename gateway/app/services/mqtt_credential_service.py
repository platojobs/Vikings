"""设备/客户端 MQTT 凭证签发与轮换。

Broker 侧唯一可信校验来源是 `infra/mosquitto/passwd`（PBKDF2-SHA512 hash）；
网关只保存 `username / version / fingerprint / 时间` 元数据，绝不保存明文口令。
"""

from __future__ import annotations

import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from app.config import get_settings
from app.mqtt.credentials import (
    fingerprint,
    generate_password,
    upsert_credential,
)
from app.models import Device

# 客户端只读主体可订阅的 Topic；与 infra/mosquitto/acl.client 保持一致
CLIENT_READ_TOPICS = [
    "device/+/sensor/report",
    "device/+/status",
    "device/+/cmd/ack",
    "device/system/alert",
    "device/system/group",
]

CLIENT_ALIASES = {"web": "web", "mobile": "mobile"}


class MqttCredentialError(RuntimeError):
    """凭证签发失败（文件不可写 / 身份未配置 / Broker 重载失败）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_password_file() -> Path:
    return get_settings().mqtt_password_path


def reload_broker() -> tuple[bool, str]:
    """让 Broker 重新读取 passwd/acl；返回 (是否成功, 说明)。"""
    settings = get_settings()
    command = settings.mqtt_reload_command.strip()
    if command:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "重载命令执行失败").strip()
        return True, f"已执行：{command}"

    pid_file = Path(__file__).resolve().parents[3] / ".run" / "mosquitto.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGHUP)
            return True, f"已向 Mosquitto（pid={pid}）发送 SIGHUP"
        except (ValueError, ProcessLookupError, PermissionError) as exc:
            return False, f"发送 SIGHUP 失败：{exc}"

    return False, (
        "未找到 .run/mosquitto.pid，且未配置 MQTT_RELOAD_COMMAND；"
        "请手动重载 Broker 后新凭证才会生效"
    )


def issue_device_credential(device: Device) -> Dict[str, object]:
    """签发/轮换设备凭证：username=device_id，旧口令被覆盖。

    注意：必须先 commit 设备元数据再调用 reload，否则重载后仍是旧口令。
    调用方负责数据库提交与审计。
    """
    password_file = _resolve_password_file()
    username = device.device_id
    password = generate_password()
    try:
        upsert_credential(password_file, username, password)
    except OSError as exc:
        raise MqttCredentialError(
            f"无法写入 Broker 密码文件 {password_file}：{exc}；"
            "请确认网关进程对 Broker 配置目录有写权限，或在宿主机执行 "
            "scripts/mqtt_credentials.py 后重载 Broker"
        ) from exc

    now = _now()
    previous_version = int(device.mqtt_credential_version or 0)
    device.mqtt_username = username
    device.mqtt_credential_version = previous_version + 1
    device.mqtt_credential_fingerprint = fingerprint(password)
    if device.mqtt_credential_issued_at is None:
        device.mqtt_credential_issued_at = now
    device.mqtt_credential_rotated_at = now

    return {
        "device_id": username,
        "username": username,
        "password": password,
        "version": device.mqtt_credential_version,
        "issued_at": device.mqtt_credential_issued_at,
        "rotated_at": now,
        "password_file": str(password_file),
    }


def client_credential(client: str) -> Dict[str, object]:
    """登录用户换取客户端只读 MQTT 凭证；口令只来自服务端环境变量。"""
    settings = get_settings()
    alias = CLIENT_ALIASES.get(client)
    if alias is None:
        raise MqttCredentialError("client 只支持 web / mobile")
    if alias == "web":
        username = settings.mqtt_web_username
        password = settings.mqtt_web_password
    else:
        username = settings.mqtt_mobile_username
        password = settings.mqtt_mobile_password
    if not password:
        raise MqttCredentialError(
            f"{alias} 客户端 MQTT 凭证未配置；请设置 MQTT_{alias.upper()}_PASSWORD "
            "并由 scripts/mqtt_credentials.py 写入 Broker"
        )
    return {
        "client": alias,
        "username": username,
        "password": password,
        "read_only": True,
        "topics": CLIENT_READ_TOPICS,
    }


def credential_metadata(device: Device) -> Dict[str, Optional[object]]:
    """给管理端展示的凭证元数据（不含口令）。"""
    return {
        "mqtt_username": device.mqtt_username,
        "mqtt_credential_version": device.mqtt_credential_version,
        "mqtt_credential_fingerprint": device.mqtt_credential_fingerprint,
        "mqtt_credential_issued_at": device.mqtt_credential_issued_at,
        "mqtt_credential_rotated_at": device.mqtt_credential_rotated_at,
    }
