"""统计服务：仪表盘顶部指标卡。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.schemas import StatsOverviewOut
from app.services.alert_service import count_open_alerts
from app.services.device_service import count_report_today
from app.topics import DEVICE_STATUS_ONLINE


async def overview(session: AsyncSession) -> StatsOverviewOut:
    total = int((await session.execute(select(func.count()).select_from(Device))).scalar_one())
    online = int(
        (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.status == DEVICE_STATUS_ONLINE)
            )
        ).scalar_one()
    )
    return StatsOverviewOut(
        device_total=total,
        device_online=online,
        device_offline=total - online,
        report_today=await count_report_today(session),
        alert_open=await count_open_alerts(session),
    )
