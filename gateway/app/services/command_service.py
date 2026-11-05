"""指令与 OTA 服务：负责指令落库、MQTT 下发、回执状态机推进与任务汇总。"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceCommand, OtaTask
from app.schemas import (
    CommandAckMessage,
    CommandBulkFailure,
    CommandBulkItem,
    CommandBulkResult,
    OtaDeviceState,
    OtaTaskOut,
)
from app.topics import command_topic

logger = logging.getLogger(__name__)

# 发布函数签名：publish(topic: str, payload: dict) -> None
Publisher = Callable[[str, Dict], Awaitable[None]]

# 批量指令的幂等窗口：同一操作人 + requestId 在窗口内只真正下发一次。
# 网关当前为单进程部署；若未来多副本，需要把这里换成数据库/Redis 共享存储。
_BULK_IDEMPOTENCY_TTL_SECONDS = 600
_bulk_results: Dict[str, Tuple[float, CommandBulkResult]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)



async def issue_command(
    session: AsyncSession,
    publisher: Publisher,
    device_id: str,
    cmd_type: str,
    payload: Dict,
    operator: str,
    ota_task_id: Optional[str] = None,
) -> Tuple[Optional[DeviceCommand], Optional[str]]:
    """创建指令 → 落库 → 通过 MQTT 下发。

    返回 (指令对象, 错误信息)。设备不存在或下发失败时返回错误信息，
    由 API 层转成对应 HTTP 语义。
    """
    device = await session.get(Device, device_id)
    if device is None:
        return None, "设备不存在: " + device_id

    cmd = DeviceCommand(
        cmd_id="cmd-" + uuid.uuid4().hex[:12],
        device_id=device_id,
        type=cmd_type,
        payload=payload,
        state="issued",
        progress=0,
        message="指令已下发，等待设备回执",
        ota_task_id=ota_task_id,
        operator=operator,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(cmd)
    # 先提交再发布：确保设备回执先到时能查到对应指令，避免状态更新丢失
    await session.commit()

    topic = command_topic(device_id)
    body = {
        "cmdId": cmd.cmd_id,
        "type": cmd.type,
        "payload": payload,
        "operator": operator,
        "ts": int(_now().timestamp() * 1000),
    }
    try:
        await publisher(topic, body)
    except Exception as exc:
        logger.error("指令下发失败 cmd=%s device=%s err=%s", cmd.cmd_id, device_id, exc)
        cmd.state = "failed"
        cmd.message = "MQTT 下发失败: " + str(exc)[:200]
        await session.flush()
        if ota_task_id:
            # 下发失败也是终态，必须立即计入任务 failed，否则任务会永远显示 running
            await refresh_task_summary(session, ota_task_id)
        await session.commit()
        return cmd, "MQTT 下发失败，请检查 Broker 连接"

    logger.info("指令已下发 cmd=%s device=%s type=%s", cmd.cmd_id, device_id, cmd_type)
    return cmd, None


async def bulk_issue_commands(
    session: AsyncSession,
    publisher: Publisher,
    device_ids: Sequence[str],
    cmd_type: str,
    payload: Dict,
    operator: str,
    request_id: Optional[str] = None,
) -> CommandBulkResult:
    """批量下发同一指令：预检 → 逐台下发 → 分类返回成功/失败/跳过。

    预检跳过是**批量场景**的显式语义（单设备端点保持原行为，不做预检）：
    - 设备不存在；
    - 未签发有效 MQTT 凭证（missing/revoked），设备根本无法接入；
    - 设备离线：MQTT 非持久会话，投递到离线设备只会等超时，不如直接告知。

    单台发布失败不会中断整批，失败原因逐台返回。
    """
    ordered_ids = list(dict.fromkeys(device_ids))
    cache_key = f"{operator}:{request_id}" if request_id else ""
    if cache_key:
        now = time.monotonic()
        cached = _bulk_results.get(cache_key)
        if cached and cached[0] > now:
            logger.info("批量指令重放 requestId=%s operator=%s", request_id, operator)
            return cached[1].model_copy(update={"replayed": True})
        if cached:
            _bulk_results.pop(cache_key, None)
        # 顺带清理过期项，避免进程内缓存无限增长
        for key, (expires, _) in list(_bulk_results.items()):
            if expires <= now:
                _bulk_results.pop(key, None)

    device_rows = {
        row.device_id: row
        for row in (
            (await session.execute(select(Device).where(Device.device_id.in_(ordered_ids)))).scalars().all()
        )
    }

    success: List[CommandBulkItem] = []
    failed: List[CommandBulkFailure] = []
    skipped: List[CommandBulkFailure] = []

    for device_id in ordered_ids:
        device = device_rows.get(device_id)
        if device is None:
            skipped.append(CommandBulkFailure(device_id=device_id, reason="设备不存在"))
            continue
        # 只跳过离线设备：在线设备必然已成功接入 Broker，说明凭证可用；
        # DB 中的凭证元数据可能因历史/命令行签发而不完整，不能据此误杀。
        if device.status != "online":
            reason = "设备离线，指令无法投递（MQTT 非持久会话）"
            if device.mqtt_credential_status != "active":
                reason = "设备离线，且未签发有效 MQTT 凭证，无法接入 Broker"
            skipped.append(CommandBulkFailure(device_id=device_id, reason=reason))
            continue

        cmd, err = await issue_command(
            session=session,
            publisher=publisher,
            device_id=device_id,
            cmd_type=cmd_type,
            payload=payload,
            operator=operator,
        )
        if cmd is None or err:
            failed.append(CommandBulkFailure(device_id=device_id, reason=err or "设备不存在"))
        else:
            success.append(CommandBulkItem(device_id=device_id, cmd_id=cmd.cmd_id))

    result = CommandBulkResult(
        batch_id="cmdbatch-" + uuid.uuid4().hex[:12],
        type=cmd_type,
        requested=len(ordered_ids),
        success=success,
        failed=failed,
        skipped=skipped,
    )
    if cache_key:
        _bulk_results[cache_key] = (time.monotonic() + _BULK_IDEMPOTENCY_TTL_SECONDS, result)
    logger.info(
        "批量指令 batch=%s type=%s requested=%d success=%d failed=%d skipped=%d",
        result.batch_id,
        cmd_type,
        result.requested,
        len(success),
        len(failed),
        len(skipped),
    )
    return result


async def apply_ack(
    session: AsyncSession,
    msg: CommandAckMessage,
    *,
    device_id: Optional[str] = None,
) -> Optional[DeviceCommand]:
    """处理设备回执：推进状态机与 OTA 进度，并同步任务汇总。

    `device_id` 由 MQTT 主题解析而来，用于校验回执只能推进自己的指令；
    不传时保持旧行为（仅单测/内部调用），网关实际处理始终传入。
    """
    cmd = await session.get(DeviceCommand, msg.cmd_id)
    if cmd is None:
        logger.warning("收到未知指令回执 cmd=%s device=%s", msg.cmd_id, device_id or "-")
        return None
    if device_id and cmd.device_id != device_id:
        logger.warning(
            "回执设备与指令归属不一致，已忽略 cmd=%s expected=%s actual=%s",
            msg.cmd_id,
            cmd.device_id,
            device_id,
        )
        return None

    previous_state = cmd.state
    # 终态不可回退：success/failed 之后的乱序或重复回执不再改状态。
    # 超时巡检写入的 failed 同样保持终态，重试应创建新指令而不是复活旧指令。
    if previous_state not in ("success", "failed"):
        # 状态只允许前进，防止乱序回执把 success 回退成 progress
        state_rank = {"issued": 0, "received": 1, "progress": 2, "success": 3, "failed": 3}
        if state_rank.get(msg.state, 0) >= state_rank.get(previous_state, 0):
            cmd.state = msg.state
        # success 即使设备没带 progress=100，也按已完成处理，避免状态与进度错位
        if cmd.state == "success":
            cmd.progress = 100
        else:
            cmd.progress = max(cmd.progress, msg.progress)
        if msg.message:
            cmd.message = msg.message
        cmd.updated_at = _now()
        await session.flush()

    changed = cmd.state != previous_state
    if changed and msg.type == "ota" and cmd.state == "success":
        # 升级成功后同步设备固件版本，后台列表页展示最新版本
        device = await session.get(Device, cmd.device_id)
        target_version = (cmd.payload or {}).get("targetVersion")
        if device is not None and target_version:
            device.firmware_version = str(target_version)[:32]
            await session.flush()

    if changed and msg.type == "ota" and cmd.ota_task_id and cmd.state in ("success", "failed"):
        # 必须先 flush 再汇总，否则统计查询读到的还是旧状态
        await refresh_task_summary(session, cmd.ota_task_id)

    return cmd


async def refresh_task_summary(session: AsyncSession, task_id: str) -> None:
    """重算 OTA 任务的成功/失败汇总。"""
    task = await session.get(OtaTask, task_id)
    if task is None:
        return
    stmt = select(DeviceCommand.state, func.count()).where(DeviceCommand.ota_task_id == task_id).group_by(DeviceCommand.state)
    rows = (await session.execute(stmt)).all()
    state_counts = {state: int(count) for state, count in rows}
    task.success = state_counts.get("success", 0)
    task.failed = state_counts.get("failed", 0)
    await session.flush()


async def list_commands(session: AsyncSession, device_id: str, limit: int = 50) -> List[DeviceCommand]:
    stmt = (
        select(DeviceCommand)
        .where(DeviceCommand.device_id == device_id)
        .order_by(DeviceCommand.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create_ota_task(
    session: AsyncSession,
    publisher: Publisher,
    device_ids: Sequence[str],
    target_version: str,
    url: str,
    description: Optional[str],
    operator: str,
) -> OtaTaskOut:
    """创建 OTA 任务：先校验设备存在，再建任务并逐台下发 ota 指令。

    设备不存在时直接抛 ValueError，而不是建出一个永远凑不齐 total 的僵尸任务
    （不存在的设备不会产生指令行，汇总永远停在 success=0/failed=0）。
    """
    ordered_ids = list(dict.fromkeys(device_ids))
    existing = set(
        (await session.execute(select(Device.device_id).where(Device.device_id.in_(ordered_ids)))).scalars().all()
    )
    missing = [device_id for device_id in ordered_ids if device_id not in existing]
    if missing:
        raise ValueError("设备不存在: " + ", ".join(missing[:5]))

    task = OtaTask(
        task_id="ota-" + uuid.uuid4().hex[:12],
        target_version=target_version,
        url=url,
        description=description,
        total=len(ordered_ids),
        success=0,
        failed=0,
        created_at=_now(),
    )
    session.add(task)
    await session.commit()

    payload = {"targetVersion": target_version, "url": url}
    error_msgs: List[str] = []
    for device_id in ordered_ids:
        _, err = await issue_command(
            session, publisher, device_id, "ota", payload, operator, ota_task_id=task.task_id
        )
        if err:
            error_msgs.append(device_id + ": " + err)

    if error_msgs:
        task.description = (description or "") + " | 部分下发失败: " + "; ".join(error_msgs)
        await session.commit()

    return await get_ota_task(session, task.task_id)


async def _devices_of_task(session: AsyncSession, task_id: str) -> List[OtaDeviceState]:
    stmt = (
        select(DeviceCommand)
        .where(DeviceCommand.ota_task_id == task_id)
        .order_by(DeviceCommand.created_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        OtaDeviceState(
            device_id=row.device_id,
            cmd_id=row.cmd_id,
            state=row.state,
            progress=row.progress,
            message=row.message,
        )
        for row in rows
    ]


async def get_ota_task(session: AsyncSession, task_id: str) -> Optional[OtaTaskOut]:
    task = await session.get(OtaTask, task_id)
    if task is None:
        return None
    devices = await _devices_of_task(session, task_id)
    return OtaTaskOut(
        task_id=task.task_id,
        target_version=task.target_version,
        url=task.url,
        description=task.description,
        total=task.total,
        success=task.success,
        failed=task.failed,
        created_at=task.created_at,
        devices=devices,
    )


async def list_ota_tasks(session: AsyncSession, page: int, page_size: int) -> Tuple[List[OtaTaskOut], int]:
    count_stmt = select(func.count()).select_from(OtaTask)
    total = int((await session.execute(count_stmt)).scalar_one())

    stmt = (
        select(OtaTask)
        .order_by(OtaTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = list((await session.execute(stmt)).scalars().all())

    result: List[OtaTaskOut] = []
    for task in tasks:
        result.append(
            OtaTaskOut(
                task_id=task.task_id,
                target_version=task.target_version,
                url=task.url,
                description=task.description,
                total=task.total,
                success=task.success,
                failed=task.failed,
                created_at=task.created_at,
                devices=await _devices_of_task(session, task.task_id),
            )
        )
    return result, total
