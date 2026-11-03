"""Pydantic 模型：MQTT 消息体 + REST 出入参。

MQTT 侧模型承担「消息校验」职责，任何字段缺失/越界都在消费入口被拦截，
不让脏数据流进数据库。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")


def to_utc_datetime(value) -> datetime:
    """统一时间语义：毫秒时间戳 / ISO 字符串 → 带时区的 UTC datetime。

    数据库统一存 UTC，前端按本地时区展示，避免跨时区部署出现 8 小时偏移。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if text.isdigit():
            dt = datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(text)
    else:
        raise ValueError("无法解析的时间格式")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ==================== MQTT 消息体 ====================


class SensorReportMessage(BaseModel):
    """device/{deviceId}/sensor/report"""

    device_id: str = Field(alias="deviceId", min_length=1, max_length=64)
    temperature: float = Field(ge=-40, le=85)
    humidity: float = Field(ge=0, le=100)
    voltage: float = Field(ge=0, le=6)
    rssi: int = Field(ge=-150, le=0)
    battery: int = Field(ge=0, le=100)
    seq: int = Field(ge=0)
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        return to_utc_datetime(v)


class DeviceStatusMessage(BaseModel):
    """device/{deviceId}/status"""

    device_id: str = Field(alias="deviceId", min_length=1, max_length=64)
    status: str = Field(pattern="^(online|offline)$")
    firmware_version: Optional[str] = Field(default=None, alias="firmwareVersion", max_length=32)
    reason: Optional[str] = Field(default=None, max_length=32)
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        return to_utc_datetime(v)


class CommandAckMessage(BaseModel):
    """device/{deviceId}/cmd/ack"""

    cmd_id: str = Field(alias="cmdId", min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=16)
    state: str = Field(pattern="^(received|progress|success|failed)$")
    progress: int = Field(default=0, ge=0, le=100)
    message: Optional[str] = Field(default=None, max_length=255)
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        return to_utc_datetime(v)


# ==================== REST 出入参 ====================


class ApiResponse(BaseModel, Generic[T]):
    """统一响应体：code=0 成功，其余为业务错误码。"""

    code: int = 0
    message: str = "ok"
    data: Optional[T] = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserInfo(BaseModel):
    username: str
    role: str = "admin"


class LoginResponse(BaseModel):
    token: str
    user: UserInfo


class DeviceOut(BaseModel):
    device_id: str
    name: str
    model: str
    firmware_version: str
    status: str
    last_report_at: Optional[datetime] = None
    last_online_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SensorRecordOut(BaseModel):
    id: int
    device_id: str
    temperature: float
    humidity: float
    voltage: float
    rssi: int
    battery: int
    seq: int
    ts: datetime

    model_config = {"from_attributes": True}


class DeviceDetailOut(DeviceOut):
    latest: Optional[SensorRecordOut] = None


class AlertOut(BaseModel):
    alert_id: str
    device_id: str
    level: str
    metric: str
    value: float
    threshold: float
    message: str
    ai_generated: bool
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class CommandCreateRequest(BaseModel):
    type: str = Field(pattern="^(calibrate|ota|reboot|config)$")
    payload: Dict[str, Any] = Field(default_factory=dict)


class CommandOut(BaseModel):
    cmd_id: str
    device_id: str
    type: str
    payload: Dict[str, Any]
    state: str
    progress: int
    message: Optional[str] = None
    ota_task_id: Optional[str] = None
    operator: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class OtaTaskCreateRequest(BaseModel):
    device_ids: List[str] = Field(alias="deviceIds", min_length=1)
    target_version: str = Field(alias="targetVersion", min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=512)
    description: Optional[str] = Field(default=None, max_length=255)


class OtaDeviceState(BaseModel):
    device_id: str
    cmd_id: str
    state: str
    progress: int
    message: Optional[str] = None


class OtaTaskOut(BaseModel):
    task_id: str
    target_version: str
    url: str
    description: Optional[str] = None
    total: int
    success: int
    failed: int
    created_at: datetime
    devices: List[OtaDeviceState] = Field(default_factory=list)


class StatsOverviewOut(BaseModel):
    device_total: int
    device_online: int
    device_offline: int
    report_today: int
    alert_open: int


class PageOut(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    page_size: int


# ==================== 阈值在线配置 ====================


class ThresholdsUpdateRequest(BaseModel):
    """PUT /api/rules/thresholds 入参：只更新传入的键，缺省保持现状。

    键名与 Settings.threshold_* 一一对应，取值范围按物理意义约束。
    """

    voltage_warning: Optional[float] = Field(default=None, ge=2.0, le=4.5)
    voltage_critical: Optional[float] = Field(default=None, ge=2.0, le=4.5)
    temperature_warning_high: Optional[float] = Field(default=None, ge=-40, le=85)
    temperature_critical_high: Optional[float] = Field(default=None, ge=-40, le=85)
    temperature_warning_low: Optional[float] = Field(default=None, ge=-40, le=85)
    temperature_critical_low: Optional[float] = Field(default=None, ge=-40, le=85)
    humidity_warning: Optional[float] = Field(default=None, ge=0, le=100)
    humidity_critical: Optional[float] = Field(default=None, ge=0, le=100)
    rssi_warning: Optional[float] = Field(default=None, ge=-150, le=0)
    rssi_critical: Optional[float] = Field(default=None, ge=-150, le=0)


# ==================== 审计日志 ====================


class AuditLogOut(BaseModel):
    id: int
    operator: str
    action: str
    target_type: str
    target_id: str
    detail: Dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


# ==================== 数据聚合 ====================


class AggregatePoint(BaseModel):
    """时间桶聚合结果：每个桶给出三指标的 avg/min/max。"""

    bucket_start: datetime
    count: int
    temperature_avg: float
    temperature_min: float
    temperature_max: float
    humidity_avg: float
    humidity_min: float
    humidity_max: float
    voltage_avg: float
    voltage_min: float
    voltage_max: float


# ==================== 告警统计 ====================


class AlertSummaryOut(BaseModel):
    open: int
    critical_open: int
    today: int
