#!/usr/bin/env python3
"""Mosquitto 凭证签发 / 轮换 / 吊销工具。

用法（在仓库根目录执行）：

    # 1) 首次初始化：生成 passwd + 服务/客户端账号（不会覆盖已存在的账号）
    python scripts/mqtt_credentials.py init

    # 2) 为设备签发（用户名 = deviceId，凭证与 deviceId 绑定）
    python scripts/mqtt_credentials.py issue --subject device:dev-0001

    # 3) 轮换：生成新口令并替换旧口令（旧凭证在 Broker 重载后立即失效）
    python scripts/mqtt_credentials.py rotate --subject device:dev-0001 --reload

    # 4) 吊销
    python scripts/mqtt_credentials.py revoke --subject device:dev-0001 --reload

    # 5) 查看本地保存的账号（不含 hash）
    python scripts/mqtt_credentials.py show

安全约定：
- passwd 与本地状态文件默认位于 `infra/mosquitto/passwd` 与 `.run/mqtt/credentials.json`，
  均已被 .gitignore 覆盖，禁止提交。
- 本脚本只在本地状态文件里保存明文口令（权限 0600），供 start_all / 联调读取；
  Device 表只保存 username、版本、时间和 SHA-256 指纹，不保存明文。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from app.mqtt.credentials import (  # noqa: E402
    SERVICE_GATEWAY,
    SERVICE_IDENTITIES,
    SERVICE_MOBILE,
    SERVICE_WEB,
    ensure_identity,
    fingerprint,
    generate_password,
    is_device_subject,
    read_password_file,
    remove_credential,
    rotation_version,
    subject_username,
    upsert_credential,
)

DEFAULT_PASSWORD_FILE = ROOT / "infra" / "mosquitto" / "passwd"
DEFAULT_STATE_FILE = ROOT / ".run" / "mqtt" / "credentials.json"
DEFAULT_PID_FILE = ROOT / ".run" / "mosquitto.pid"
SERVICE_SUBJECTS = {
    SERVICE_GATEWAY: "gateway",
    SERVICE_WEB: "web",
    SERVICE_MOBILE: "mobile",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"services": {}, "devices": {}, "revoked": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] 状态文件无法解析，按空状态处理：{path}", file=sys.stderr)
        return {"services": {}, "devices": {}, "revoked": {}}
    data.setdefault("services", {})
    data.setdefault("devices", {})
    data.setdefault("revoked", {})
    return data


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def reload_broker(command: Optional[str]) -> None:
    """SIGHUP 让 Mosquitto 重新读取 passwd / acl；失败只提示，不吞掉成功结果。"""
    cmd = command or os.environ.get("MQTT_RELOAD_COMMAND", "").strip()
    if cmd:
        subprocess.run(cmd, shell=True, check=True)
        print(f"[info] 已执行重载命令：{cmd}")
        return
    if DEFAULT_PID_FILE.exists():
        pid = int(DEFAULT_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGHUP)
        print(f"[info] 已向 Mosquitto（pid={pid}）发送 SIGHUP")
        return
    print(
        "[warn] 未找到 Broker PID 也未配置 MQTT_RELOAD_COMMAND；"
        "请手动重载（本地：kill -HUP $(cat .run/mosquitto.pid)；"
        "Docker：docker compose -f infra/docker-compose.yml kill -s HUP mosquitto）",
        file=sys.stderr,
    )


def record_secret(
    state: Dict[str, Any], username: str, password: str, subject: str, version: int
) -> None:
    bucket = "devices" if is_device_subject(subject) else "services"
    old = state.get(bucket, {}).get(username, {})
    state[bucket][username] = {
        "subject": subject,
        "username": username,
        "password": password,
        "version": version,
        "fingerprint": fingerprint(password),
        "issued_at": old.get("issued_at") or now_iso(),
        "rotated_at": now_iso() if old else old.get("issued_at") or now_iso(),
    }
    state.get("revoked", {}).pop(username, None)


def cmd_init(args: argparse.Namespace) -> int:
    password_file = Path(args.password_file).resolve()
    state_file = Path(args.state_file).resolve()
    state = load_state(state_file)
    password_file.parent.mkdir(parents=True, exist_ok=True)
    if not password_file.exists():
        password_file.write_text("", encoding="utf-8")
        os.chmod(password_file, 0o600)

    created = []
    for username in SERVICE_IDENTITIES:
        secret, is_new = ensure_identity(password_file, username)
        if is_new:
            record_secret(state, username, secret, f"service:{username}", 1)
            created.append(username)
        else:
            print(f"[info] 账号已存在，保留原口令：{username}")

    for device_id in args.device_id or []:
        subject = f"device:{device_id}"
        entries = read_password_file(password_file)
        if device_id in entries:
            print(f"[info] 设备凭证已存在，保留原口令：{device_id}")
            continue
        secret = generate_password()
        upsert_credential(password_file, device_id, secret)
        record_secret(state, device_id, secret, subject, 1)
        created.append(device_id)

    save_state(state_file, state)
    print(
        json.dumps(
            {
                "password_file": str(password_file),
                "state_file": str(state_file),
                "created": created,
                "services": state["services"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_issue(args: argparse.Namespace, rotate: bool) -> int:
    subject = args.subject
    username = subject_username(subject)
    if not username:
        print("[fail] subject 不能为空", file=sys.stderr)
        return 2
    if is_device_subject(subject) and not all(
        ch.isalnum() or ch in "_-" for ch in username
    ):
        print("[fail] deviceId 只允许字母、数字、下划线与连字符", file=sys.stderr)
        return 2

    password_file = Path(args.password_file).resolve()
    state_file = Path(args.state_file).resolve()
    state = load_state(state_file)
    bucket = "devices" if is_device_subject(subject) else "services"
    previous = state.get(bucket, {}).get(username, {})
    version = rotation_version(previous.get("version"))

    secret = generate_password()
    upsert_credential(password_file, username, secret)
    record_secret(state, username, secret, subject, version)
    save_state(state_file, state)

    if args.reload:
        reload_broker(args.reload_command)

    print(
        json.dumps(
            {
                "subject": subject,
                "username": username,
                "password": secret,
                "version": version,
                "rotated": bool(previous) or rotate,
                "password_file": str(password_file),
                "reload": bool(args.reload),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    username = subject_username(args.subject)
    password_file = Path(args.password_file).resolve()
    state_file = Path(args.state_file).resolve()
    state = load_state(state_file)
    removed = remove_credential(password_file, username)
    bucket = "devices" if is_device_subject(args.subject) else "services"
    state.get(bucket, {}).pop(username, None)
    state.setdefault("revoked", {})[username] = {
        "subject": args.subject,
        "revoked_at": now_iso(),
    }
    save_state(state_file, state)
    if args.reload:
        reload_broker(args.reload_command)
    print(
        json.dumps(
            {
                "subject": args.subject,
                "username": username,
                "removed": removed,
                "reload": bool(args.reload),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if removed else 1


def cmd_show(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    state = load_state(state_file)
    print(
        json.dumps(
            {
                "state_file": str(state_file),
                "services": {
                    name: {
                        "username": item["username"],
                        "version": item.get("version"),
                        "rotated_at": item.get("rotated_at"),
                        "password": item.get("password"),
                    }
                    for name, item in state["services"].items()
                },
                "devices": {
                    name: {
                        "username": item["username"],
                        "version": item.get("version"),
                        "rotated_at": item.get("rotated_at"),
                        "password": item.get("password"),
                    }
                    for name, item in state["devices"].items()
                },
                "revoked": state["revoked"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mosquitto 凭证签发/轮换工具")
    parser.add_argument(
        "--password-file", default=str(DEFAULT_PASSWORD_FILE), help="Mosquitto 密码文件"
    )
    parser.add_argument(
        "--state-file", default=str(DEFAULT_STATE_FILE), help="本地凭证状态文件（含明文，勿入库）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化服务/客户端账号（幂等）")
    init.add_argument(
        "--device-id",
        action="append",
        default=[],
        help="顺便签发设备凭证，可重复传入（如 dev-0001）",
    )
    init.set_defaults(func=cmd_init)

    issue = sub.add_parser("issue", help="签发新凭证")
    issue.add_argument("--subject", required=True, help="device:<deviceId> 或服务账号名")
    issue.add_argument("--reload", action="store_true", help="写入后重载 Broker")
    issue.add_argument("--reload-command", default=None, help="自定义重载命令")
    issue.set_defaults(func=lambda a: cmd_issue(a, rotate=False))

    rotate = sub.add_parser("rotate", help="轮换凭证（旧口令立即失效）")
    rotate.add_argument("--subject", required=True, help="device:<deviceId> 或服务账号名")
    rotate.add_argument("--reload", action="store_true", help="写入后重载 Broker")
    rotate.add_argument("--reload-command", default=None, help="自定义重载命令")
    rotate.set_defaults(func=lambda a: cmd_issue(a, rotate=True))

    revoke = sub.add_parser("revoke", help="吊销凭证")
    revoke.add_argument("--subject", required=True, help="device:<deviceId> 或服务账号名")
    revoke.add_argument("--reload", action="store_true", help="写入后重载 Broker")
    revoke.add_argument("--reload-command", default=None, help="自定义重载命令")
    revoke.set_defaults(func=cmd_revoke)

    show = sub.add_parser("show", help="查看本地保存的账号与口令（仅调试）")
    show.set_defaults(func=cmd_show)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init":
        return cmd_init(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
