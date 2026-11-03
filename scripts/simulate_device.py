#!/usr/bin/env python3
"""设备端联调模拟器（仅用于本地联调与冒烟测试）。

真实设备端由 device_app（Flutter）承担，本脚本只复刻其协议行为，
方便在没有手机/模拟器时验证网关链路：
    唤醒 → 上线(retained) → 上报 → 短窗口接收指令 → 休眠下线

用法：
    python scripts/simulate_device.py --device-id dev-0001 --count 3 --interval 5
    python scripts/simulate_device.py --device-id dev-0001 --voltage 2.9   # 触发严重告警
"""

import argparse
import asyncio
import json
import random
import time

import aiomqtt


def now_ms() -> int:
    return int(time.time() * 1000)


class SimulatedDevice:
    def __init__(self, args) -> None:
        self.device_id = args.device_id
        self.host = args.host
        self.port = args.port
        self.count = args.count
        self.interval = args.interval
        self.voltage = args.voltage
        self.temperature = args.temperature
        self.firmware = args.firmware
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
            will=will,
            keepalive=30,
        ) as client:
            for round_no in range(1, self.count + 1):
                await self._publish_status(client, "online", "boot")
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
    args = parser.parse_args()
    asyncio.run(SimulatedDevice(args).run())


if __name__ == "__main__":
    main()
