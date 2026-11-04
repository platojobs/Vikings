"""Mosquitto 凭证工具：hash 生成、密码文件读写、随机口令。

只依赖标准库，便于脚本、网关进程与 E2E 复用，不引入额外运行时依赖。

Mosquitto 2.x 的 `sha512-pbkdf2` 密码文件格式为（默认 iterations=1000）：

    username:$7$1000$<base64 salt>$<base64 pbkdf2-sha512 digest>

盐固定使用 12 字节：Ubuntu 20.04/22.04/24.04 自带的 Mosquitto 2.0.x
只接受 `salt_len == 12`，而 2.1.x 同时接受 12 与 64，因此 12 字节是跨版本安全值。
摘要固定 64 字节（SHA-512 输出）。Broker 仍使用官方实现的校验逻辑，
网关与脚本不参与登录校验。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Mosquitto 2.x 默认 PBKDF2-SHA512 迭代次数；与 mosquitto_passwd 输出一致
PBKDF2_SHA512_ITERATIONS = 1000
# 12 字节：兼容 Mosquitto 2.0.x（只接受 12）与 2.1.x（接受 12/64）
SALT_BYTES = 12
HASH_BYTES = 64

# 固定服务/客户端身份名称：与 infra/mosquitto/acl.* 严格对应
SERVICE_GATEWAY = "iot-gateway"
SERVICE_WEB = "web-console"
SERVICE_MOBILE = "mobile-console"
SERVICE_IDENTITIES: Tuple[str, ...] = (SERVICE_GATEWAY, SERVICE_WEB, SERVICE_MOBILE)


def generate_password() -> str:
    """生成 32 字节 URL-safe 随机口令（256 bit，足够抵抗离线爆破）。"""
    return secrets.token_urlsafe(32)


def fingerprint(password: str) -> str:
    """凭证指纹：仅用于审计/对账，不可反推口令，也不作为 Broker 校验值。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def hash_password(password: str, iterations: int = PBKDF2_SHA512_ITERATIONS) -> str:
    """生成 Mosquitto `$7$` PBKDF2-SHA512 记录（不含用户名与冒号）。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, iterations, HASH_BYTES
    )
    return "$7${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_hash(password: str, stored: str) -> bool:
    """本地校验 `$7$` 记录，仅用于脚本/测试自查，Broker 侧不需要调用。"""
    try:
        _empty, prefix, iterations, salt_b64, digest_b64 = stored.split("$")
        if prefix != "7":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha512", password.encode("utf-8"), salt, int(iterations), len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def read_password_file(path: Path) -> Dict[str, str]:
    """读取密码文件为 username -> hash；忽略空行与注释。"""
    if not path.exists():
        return {}
    entries: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or ":" not in text:
            continue
        username, stored = text.split(":", 1)
        if username:
            entries[username] = stored
    return entries


def password_file_mode() -> int:
    """密码文件权限：默认 0600。

    Docker 生产编排中网关与 Broker 以不同 uid 运行，且共享同一份 passwd
    绑定挂载；Broker 需要能读、网关需要能写，因此通过环境变量放宽到 0644。
    文件只含 PBKDF2 hash，不含明文口令；默认值仍是更严格的 0600。
    """
    raw = os.environ.get("MQTT_PASSWORD_FILE_MODE", "").strip()
    if not raw:
        return 0o600
    try:
        mode = int(raw, 8)
    except ValueError:
        raise ValueError(f"MQTT_PASSWORD_FILE_MODE 不是合法的八进制权限：{raw}")
    if mode & 0o002:
        raise ValueError("MQTT_PASSWORD_FILE_MODE 不允许 world-writable")
    return mode


def write_password_file(path: Path, entries: Dict[str, str]) -> None:
    """原子写入密码文件；默认 0600，Broker 对 world-readable 会告警。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = "".join(f"{user}:{stored}\n" for user, stored in entries.items())
    tmp.write_text(body, encoding="utf-8")
    mode = password_file_mode()
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)


def upsert_credential(path: Path, username: str, password: str) -> str:
    """新增/替换账号口令，返回写入的 hash 记录（不落明文）。"""
    entries = read_password_file(path)
    stored = hash_password(password)
    entries[username] = stored
    write_password_file(path, entries)
    return stored


def remove_credential(path: Path, username: str) -> bool:
    entries = read_password_file(path)
    if username not in entries:
        return False
    entries.pop(username, None)
    write_password_file(path, entries)
    return True


def ensure_identity(path: Path, username: str) -> Tuple[str, bool]:
    """确保服务账号存在；返回 (口令, 是否新生成)。

    已存在的账号无法从 hash 反推口令，因此只在文件里没有该账号时才生成。
    口令由调用方持久化到本地状态文件（不入库），否则只能轮换。
    """
    entries = read_password_file(path)
    if username in entries:
        return "", False
    password = generate_password()
    upsert_credential(path, username, password)
    return password, True


def rotation_version(previous: Optional[int]) -> int:
    return int(previous or 0) + 1


def subject_username(subject: str) -> str:
    """`device:<deviceId>` → deviceId；其它 subject 原样返回。"""
    return subject.split(":", 1)[1] if subject.startswith("device:") else subject


def is_device_subject(subject: str) -> bool:
    return subject.startswith("device:")


def write_acl_files(device_acl: Path, client_acl: Path) -> None:
    """写入仓库内的只读 ACL 模板（无账号口令，可安全入库）。"""
    device_acl.parent.mkdir(parents=True, exist_ok=True)
    device_acl.write_text(DEVICE_ACL_CONTENT, encoding="utf-8")
    client_acl.write_text(CLIENT_ACL_CONTENT, encoding="utf-8")


DEVICE_ACL_CONTENT = """\
# 1883（设备 TCP）监听专用 ACL：
# 网关是唯一跨设备主体；设备用户名等于 deviceId，只能操作自己的子树。
# 设备不能写 cmd（云端指令只允许网关发布），不能碰 device/system/*。

user iot-gateway
topic read device/+/sensor/report
topic read device/+/status
topic read device/+/cmd/ack
topic write device/+/cmd
topic write device/system/alert
topic write device/system/group
topic readwrite gateway/status

pattern read device/%u/#
pattern write device/%u/sensor/report
pattern write device/%u/status
pattern write device/%u/cmd/ack
"""


CLIENT_ACL_CONTENT = """\
# 9001（WebSocket）监听专用 ACL：
# 移动端与 Web 后台是独立只读主体；这里没有任何 write 规则，
# 即使拿到设备口令也无法通过 9001 发布设备上报 Topic。

user web-console
topic read device/+/sensor/report
topic read device/+/status
topic read device/+/cmd/ack
topic read device/system/alert
topic read device/system/group

user mobile-console
topic read device/+/sensor/report
topic read device/+/status
topic read device/+/cmd/ack
topic read device/system/alert
topic read device/system/group
"""
