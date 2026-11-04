"""指令/OTA 状态机单测（v1.23.0）。

覆盖此前完全缺失的核心链路语义：
- OTA 成功链路：received → progress → success，任务汇总与设备固件版本一致；
- 终态不可回退：success/failed 之后的乱序回执不得改写结果；
- 设备回报失败：任务终态 failed，而不是永久 running；
- 下发失败：指令与服务端立即计入 failed；
- 超时兜底：无回执的 issued/progress 指令被巡检置 failed，迟到 success 不复活；
- 跨设备回执：设备只能推进自己的指令。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models  # noqa: F401  注册 ORM 元数据
from app.background_tasks import sweep_stale_commands
from app.models import Device, DeviceCommand
from app.schemas import CommandAckMessage
from app.services import command_service
from db_utils import create_test_engine


def _ack(cmd_id: str, state: str, progress: int = 0, message: str | None = None) -> CommandAckMessage:
    return CommandAckMessage(
        cmdId=cmd_id,
        type="ota",
        state=state,
        progress=progress,
        message=message,
        ts=datetime.now(timezone.utc),
    )


def _run(tmp_path, scenario):
    async def wrapper():
        engine = await create_test_engine(tmp_path, "command.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await scenario(factory)
        finally:
            await engine.dispose()

    asyncio.run(wrapper())


async def _create_device(session, device_id: str, firmware: str = "1.0.0") -> None:
    session.add(Device(device_id=device_id, name="测试设备 " + device_id, firmware_version=firmware))
    await session.commit()


async def _create_single_task(factory, device_id: str = "dev-1"):
    published: list[dict] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append(payload)

    async with factory() as session:
        await _create_device(session, device_id)
        task = await command_service.create_ota_task(
            session,
            publish,
            [device_id],
            "1.1.0",
            "https://example.com/fw.bin",
            "单测 OTA",
            "tester",
        )
    return task, published


def test_ota_success_flow_updates_task_progress_and_firmware(tmp_path):
    async def scenario(factory):
        task, published = await _create_single_task(factory)
        assert task.total == 1 and task.success == 0 and task.failed == 0
        cmd_id = published[0]["cmdId"]

        async with factory() as session:
            await command_service.apply_ack(session, _ack(cmd_id, "received"), device_id="dev-1")
            await command_service.apply_ack(
                session, _ack(cmd_id, "progress", 40, "下载固件"), device_id="dev-1"
            )
            await command_service.apply_ack(
                session, _ack(cmd_id, "progress", 80, "写入固件"), device_id="dev-1"
            )
            await command_service.apply_ack(
                session, _ack(cmd_id, "success", 100, "升级完成"), device_id="dev-1"
            )
            await session.commit()

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            assert cmd.state == "success"
            assert cmd.progress == 100
            assert cmd.message == "升级完成"
            detail = await command_service.get_ota_task(session, task.task_id)
            assert detail.success == 1 and detail.failed == 0
            assert detail.devices[0].state == "success" and detail.devices[0].progress == 100
            device = await session.get(Device, "dev-1")
            assert device.firmware_version == "1.1.0"

    _run(tmp_path, scenario)


def test_terminal_state_does_not_regress_on_late_ack(tmp_path):
    """修复点：success 之后的迟到 failed 不得把成功任务改回失败。"""

    async def scenario(factory):
        task, published = await _create_single_task(factory)
        cmd_id = published[0]["cmdId"]

        async with factory() as session:
            await command_service.apply_ack(
                session, _ack(cmd_id, "success", 100, "升级完成"), device_id="dev-1"
            )
            await session.commit()
        async with factory() as session:
            await command_service.apply_ack(
                session, _ack(cmd_id, "failed", 100, "迟到的失败回执"), device_id="dev-1"
            )
            await session.commit()

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            detail = await command_service.get_ota_task(session, task.task_id)
            assert cmd.state == "success"
            assert cmd.message == "升级完成"
            assert detail.success == 1 and detail.failed == 0
            assert (await session.get(Device, "dev-1")).firmware_version == "1.1.0"

    _run(tmp_path, scenario)


def test_device_reported_failure_is_terminal(tmp_path):
    async def scenario(factory):
        task, published = await _create_single_task(factory)
        cmd_id = published[0]["cmdId"]

        async with factory() as session:
            await command_service.apply_ack(
                session, _ack(cmd_id, "progress", 40, "下载中"), device_id="dev-1"
            )
            await command_service.apply_ack(
                session, _ack(cmd_id, "failed", 40, "固件校验失败"), device_id="dev-1"
            )
            await session.commit()

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            detail = await command_service.get_ota_task(session, task.task_id)
            assert cmd.state == "failed" and cmd.message == "固件校验失败"
            assert detail.failed == 1 and detail.success == 0
            assert detail.devices[0].state == "failed"

    _run(tmp_path, scenario)


def test_dispatch_failure_counts_as_task_failed(tmp_path):
    async def scenario(factory):
        async def broken_publish(topic: str, payload: dict) -> None:
            raise RuntimeError("Broker 未连接")

        async with factory() as session:
            await _create_device(session, "dev-1")
            task = await command_service.create_ota_task(
                session, broken_publish, ["dev-1"], "1.1.0", "https://example.com/fw.bin", None, "tester"
            )

        assert task.total == 1
        assert task.failed == 1 and task.success == 0
        assert task.devices[0].state == "failed"
        assert "下发失败" in task.description

    _run(tmp_path, scenario)


def test_ota_rejects_unknown_device_without_zombie_task(tmp_path):
    async def scenario(factory):
        async def publish(topic: str, payload: dict) -> None:
            return None

        async with factory() as session:
            await _create_device(session, "dev-1")
            try:
                await command_service.create_ota_task(
                    session, publish, ["dev-1", "ghost"], "1.1.0", "https://example.com/fw.bin", None, "tester"
                )
            except ValueError as exc:
                assert "ghost" in str(exc)
            else:  # pragma: no cover - 防御性
                raise AssertionError("不存在的设备必须拒绝创建任务")

    _run(tmp_path, scenario)


def test_sweep_stale_commands_marks_failed_and_refreshes_task(tmp_path):
    async def scenario(factory):
        task, published = await _create_single_task(factory)
        cmd_id = published[0]["cmdId"]

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            cmd.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await session.commit()

        changed = await sweep_stale_commands(session_factory=factory, timeout_seconds=60)
        assert changed == 1

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            detail = await command_service.get_ota_task(session, task.task_id)
            assert cmd.state == "failed"
            assert "超时" in cmd.message
            assert detail.failed == 1 and detail.success == 0

        # 超时 failed 是终态：迟到的 success 不复活
        async with factory() as session:
            await command_service.apply_ack(
                session, _ack(cmd_id, "success", 100, "迟到成功"), device_id="dev-1"
            )
            await session.commit()
        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            detail = await command_service.get_ota_task(session, task.task_id)
            assert cmd.state == "failed"
            assert detail.failed == 1 and detail.success == 0

    _run(tmp_path, scenario)


def test_sweep_ignores_fresh_commands(tmp_path):
    async def scenario(factory):
        task, published = await _create_single_task(factory)
        changed = await sweep_stale_commands(session_factory=factory, timeout_seconds=60)
        assert changed == 0
        async with factory() as session:
            cmd = await session.get(DeviceCommand, published[0]["cmdId"])
            assert cmd.state == "issued"
            detail = await command_service.get_ota_task(session, task.task_id)
            assert detail.failed == 0

    _run(tmp_path, scenario)


def test_ack_cannot_advance_another_devices_command(tmp_path):
    async def scenario(factory):
        async def publish(topic: str, payload: dict) -> None:
            return None

        async with factory() as session:
            await _create_device(session, "dev-1")
            await _create_device(session, "dev-2")
            task = await command_service.create_ota_task(
                session, publish, ["dev-1"], "1.1.0", "https://example.com/fw.bin", None, "tester"
            )
            cmd_id = task.devices[0].cmd_id

        async with factory() as session:
            result = await command_service.apply_ack(
                session, _ack(cmd_id, "success", 100, "冒名回执"), device_id="dev-2"
            )
            await session.commit()
        assert result is None

        async with factory() as session:
            cmd = await session.get(DeviceCommand, cmd_id)
            assert cmd.state == "issued"
            detail = await command_service.get_ota_task(session, task.task_id)
            assert detail.success == 0

    _run(tmp_path, scenario)
