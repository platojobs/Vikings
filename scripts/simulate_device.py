#!/usr/bin/env python3
"""设备端联调模拟器（仅用于本地联调与冒烟测试）。

真实设备端由 device_app（Flutter）承担，本脚本只复刻其协议行为，
方便在没有手机/模拟器时验证网关链路：
    唤醒 → 上线(retained) → 上报 → 短窗口接收指令 → 休眠下线

用法：
    python scripts/simulate_device.py --device-id dev-0001 --count 3 --interval 5
    python scripts/simulate_device.py --device-id dev-0001 --voltage 2.9   # 触发严重告警

Broker 已关闭匿名，必须提供设备凭证：
    python scripts/mqtt_credentials.py issue --subject device:dev-0001 --reload
    MQTT_DEVICE_PASSWORD='<由上一命令输出的一次性口令>' python scripts/simulate_device.py --device-id dev-0001
"""

import argparse
import asyncio
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import aiomqtt


def now_ms() -> int:
    return int(time.time() * 1000)


class SimulatedDevice:
    def __init__(self, args) -> None:
        self.device_id = args.device_id
        self.host = args.host
        self.port = args.port
        self.username = args.username or self.device_id
        self.password = args.password
        self.count = args.count
        self.interval = args.interval
        self.voltage = args.voltage
        self.temperature = args.temperature
        self.firmware = args.firmware
        self.api_url = args.api_url.rstrip("/")
        self.api_token = args.api_token
        self.api_timeout = args.api_timeout
        self.reported_state = {}
        self.seq = 0
        self.topic_report = "device/{}/sensor/report".format(self.device_id)
        self.topic_status = "device/{}/status".format(self.device_id)
        self.topic_cmd = "device/{}/cmd".format(self.device_id)
        self.topic_ack = "device/{}/cmd/ack".format(self.device_id)

    async def run(self) -> None:
        will = aiomqtt.Will(
            topic=self.topic_status,
            payload=json.dumps({"deviceId": self.device_id, "status": "offline", "reason": "lwt", "ts": now_ms()}),
            qos=1,
            retain=True,
        )
        async with aiomqtt.Client(
            hostname=self.host,
            port=self.port,
            identifier="sim-" + self.device_id,
            username=self.username,
            password=self.password,
            will=will,
            keepalive=30,
        ) as client:
            for round_no in range(1, self.count + 1):
                await self._publish_status(client, "online", "boot")
                await self._sync_shadow()
                await client.subscribe(self.topic_cmd, qos=1)
                self.seq += 1
                await self._publish_report(client)
                print("[{}] 第 {} 轮上报完成 seq={}".format(self.device_id, round_no, self.seq))

                # 唤醒窗口：等待云端指令（OTA 回放需要约 3 秒，留 6 秒窗口）
                await self._listen_commands(client, seconds=6)

                await self._publish_status(client, "offline", "sleep")
                await client.unsubscribe(self.topic_cmd)
                if round_no < self.count:
                    print("[{}] 休眠 {} 秒".format(self.device_id, self.interval))
                    await asyncio.sleep(self.interval)

    def _api_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """同步 HTTP 请求由 asyncio.to_thread 调用，避免阻塞 MQTT 事件循环。"""
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.api_url + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.api_token,
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.api_timeout) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = str(parsed.get("detail") or parsed.get("message") or detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise RuntimeError("网关 HTTP {}: {}".format(exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("网关连接失败: {}".format(exc)) from exc

        if envelope.get("code") != 0:
            raise RuntimeError("网关返回错误: {}".format(envelope.get("message", "未知错误")))
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("网关响应缺少 data 对象")
        return data

    async def _sync_shadow(self) -> None:
        """每次唤醒拉取并应用期望，再上报实际状态与已应用版本。"""
        if not self.api_url:
            return
        path = "/api/devices/{}/shadow".format(urllib.parse.quote(self.device_id, safe=""))
        try:
            shadow = await asyncio.to_thread(self._api_request, "GET", path)
            desired = shadow.get("desired", {})
            if not isinstance(desired, dict):
                raise RuntimeError("影子 desired 不是对象")
            version = int(shadow.get("version", 0) or 0)
            reported_version = int(shadow.get("reported_version", 0) or 0)
            diff_keys = shadow.get("diff_keys", [])
            if version == 0 and not desired:
                return
            if version <= reported_version and not diff_keys:
                return

            # 模拟器把当前期望视为已应用配置；真实硬件应在此写入自身配置存储。
            self.reported_state = dict(desired)
            await asyncio.to_thread(
                self._api_request,
                "POST",
                path + "/reported",
                {"reported": self.reported_state, "version": version},
            )
            print(
                "[{}] 影子已同步 version={} keys={}".format(
                    self.device_id, version, ",".join(sorted(desired)) or "-"
                )
            )
        except Exception as exc:
            print("[{}] 影子同步失败（继续本轮上报）: {}".format(self.device_id, exc))

    async def _publish_status(self, client, status: str, reason: str) -> None:
        payload = {
            "deviceId": self.device_id,
            "status": status,
            "firmwareVersion": self.firmware,
            "reason": reason,
            "ts": now_ms(),
        }
        await client.publish(self.topic_status, json.dumps(payload), qos=1, retain=True)

    async def _publish_report(self, client) -> None:
        payload = {
            "deviceId": self.device_id,
            "temperature": round(self.temperature + random.uniform(-1.5, 1.5), 2),
            "humidity": round(55 + random.uniform(-5, 5), 2),
            "voltage": round(self.voltage + random.uniform(-0.05, 0.05), 2),
            "rssi": random.randint(-85, -45),
            "battery": max(0, int((self.voltage - 3.0) / 1.2 * 100)),
            "seq": self.seq,
            "ts": now_ms(),
        }
        await client.publish(self.topic_report, json.dumps(payload), qos=1)

    async def _listen_commands(self, client, seconds: float) -> None:
        """模拟设备处理云端指令，OTA 场景回放进度。"""
        try:
            async with asyncio.timeout(seconds):
                async for message in client.messages:
                    cmd = json.loads(message.payload.decode())
                    await self._handle_command(client, cmd)
        except TimeoutError:
            return

    async def _handle_command(self, client, cmd: dict) -> None:
        cmd_id = cmd.get("cmdId", "")
        cmd_type = cmd.get("type", "")
        print("[{}] 收到指令 {} type={}".format(self.device_id, cmd_id, cmd_type))
        await self._ack(client, cmd_id, cmd_type, "received", 0, "指令已接收")

        if cmd_type == "ota":
            for progress in (10, 30, 55, 80, 100):
                await asyncio.sleep(0.4)
                state = "success" if progress == 100 else "progress"
                await self._ack(client, cmd_id, cmd_type, state, progress, "升级进度 {}%".format(progress))
            self.firmware = cmd.get("payload", {}).get("targetVersion", self.firmware)
        else:
            await self._ack(client, cmd_id, cmd_type, "success", 100, "执行完成")

    async def _ack(self, client, cmd_id: str, cmd_type: str, state: str, progress: int, message: str) -> None:
        payload = {
            "cmdId": cmd_id,
            "type": cmd_type,
            "state": state,
            "progress": progress,
            "message": message,
            "ts": now_ms(),
        }
        await client.publish(self.topic_ack, json.dumps(payload, ensure_ascii=False), qos=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="IoT 设备端联调模拟器")
    parser.add_argument("--device-id", default="dev-0001")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--count", type=int, default=3, help="唤醒上报轮数")
    parser.add_argument("--interval", type=int, default=5, help="休眠秒数")
    parser.add_argument("--voltage", type=float, default=3.75)
    parser.add_argument("--temperature", type=float, default=24.0)
    parser.add_argument("--firmware", default="1.0.0")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GATEWAY_API_URL", ""),
        help="可选：网关 REST 地址（如 http://127.0.0.1:8000），配置后每次唤醒同步影子",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("GATEWAY_API_TOKEN", ""),
        help="影子 REST 同步所需的 operator/admin access token，也可用 GATEWAY_API_TOKEN",
    )
    parser.add_argument("--api-timeout", type=float, default=5.0, help="影子 REST 请求超时秒数")
    parser.add_argument(
        "--username",
        default=os.environ.get("MQTT_DEVICE_USERNAME", ""),
        help="MQTT 用户名，默认等于 device-id（Broker ACL 用 %%u 绑定设备子树）",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("MQTT_DEVICE_PASSWORD", ""),
        help="MQTT 口令，也可用 MQTT_DEVICE_PASSWORD 环境变量；Broker 已禁用匿名",
    )
    args = parser.parse_args()
    if bool(args.api_url) != bool(args.api_token):
        parser.error("影子同步必须同时提供 --api-url 与 --api-token（或对应 GATEWAY_API_* 环境变量）")
    if not args.password:
        parser.error(
            "必须提供设备 MQTT 口令（--password 或 MQTT_DEVICE_PASSWORD）。"
            "先执行 python scripts/mqtt_credentials.py issue --subject device:<device-id> --reload"
        )
    args.username = args.username or args.device_id
    asyncio.run(SimulatedDevice(args).run())


if __name__ == "__main__":
    main()
