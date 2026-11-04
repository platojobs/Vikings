"""告警规则路由：阈值查询与在线更新。

GET 返回当前生效值并标注来源（override=在线修改 / env=环境变量），
PUT 在线更新并写入 rule_config 表持久化，规则引擎即时生效，无需重启。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, ThresholdsUpdateRequest
from app.security import ROLE_ADMIN, require_role, require_user
from app.services import audit_service, rule_service

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("/thresholds")
async def get_thresholds(_: dict = Depends(require_user)):
    """当前生效的告警阈值，每项标注来源（在线覆盖 or 环境变量）。"""
    return ApiResponse(data=rule_service.effective_view())


@router.put("/thresholds")
async def update_thresholds(
    body: ThresholdsUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """在线更新阈值：持久化到 rule_config 并即时刷新规则引擎缓存。"""
    updated = await rule_service.update_thresholds(session, body, user["sub"])
    if updated:
        await audit_service.log(
            session,
            operator=user["sub"],
            action=audit_service.ACTION_THRESHOLD_UPDATE,
            target_type="rule_config",
            detail={"changes": updated},
        )
    await session.commit()
    return ApiResponse(data={"updated": updated, "thresholds": rule_service.effective_view()})
