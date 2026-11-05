"""Pydantic 模型：MQTT 消息体 + REST 出入参。

MQTT 侧模型承担「消息校验」职责，任何字段缺失/越界都在消费入口被拦截，
不让脏数据流进数据库。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

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
    """登录 / 刷新响应（v1.24.0）。

    `token` 仍是短 TTL 访问令牌，保留字段名以兼容既有客户端；
    `refresh_token` 为一次性轮换凭证，客户端必须单独保存，不得随每次请求发送。
    """

    token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserInfo


class RefreshRequest(BaseModel):
    """POST /api/auth/refresh 与 /api/auth/logout 的入参。"""

    refresh_token: str = Field(min_length=8, max_length=256)


class SessionRevokeOut(BaseModel):
    """管理员吊销某用户全部会话的结果。"""

    username: str
    revoked: int


class DeviceOut(BaseModel):
    device_id: str
    name: str
    model: str
    firmware_version: str
    group_name: str = "默认分组"
    status: str
    last_report_at: Optional[datetime] = None
    last_online_at: Optional[datetime] = None
    # 仅 when withAlertCount=true 时填充；默认 None 保持既有返回结构不变
    open_alert_count: Optional[int] = None
    # 凭证元数据（不包含口令与完整指纹）：供运维查看签发/轮换状态
    mqtt_username: Optional[str] = None
    mqtt_credential_version: int = 0
    mqtt_credential_issued_at: Optional[datetime] = None
    mqtt_credential_rotated_at: Optional[datetime] = None
    mqtt_credential_revoked_at: Optional[datetime] = None
    # missing / active / revoked；由 Device 属性计算，不落库
    mqtt_credential_status: str = "missing"
    # 指纹短前缀（8 位），只用于人工对账，不回传完整指纹
    mqtt_credential_fingerprint_prefix: Optional[str] = None

    model_config = {"from_attributes": True}


class MqttCredentialOut(BaseModel):
    """设备凭证签发/轮换结果：口令只在此响应出现一次。"""

    device_id: str
    username: str
    password: str
    version: int
    issued_at: datetime
    rotated_at: datetime
    # issue（首次签发）/ rotate（覆盖已有有效凭证）
    operation: str
    reloaded: bool
    reload_detail: str


class MqttCredentialRevokeOut(BaseModel):
    """设备凭证吊销结果：不含口令，也不含完整指纹。"""

    device_id: str
    username: str
    version: int
    revoked_at: datetime
    reloaded: bool
    reload_detail: str


class MqttClientCredentialOut(BaseModel):
    """登录后下发的只读客户端凭证；Web / 移动端使用独立主体。"""

    client: str
    username: str
    password: str
    read_only: bool = True
    topics: List[str]


# ==================== 设备注册与分组 ====================

class DeviceCreateRequest(BaseModel):
    """手工注册设备：device_id 幂等，重复注册返回 409。"""

    device_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    model: str = Field(default="VG-S100", max_length=64)
    firmware_version: str = Field(default="1.0.0", max_length=32)
    group_name: str = Field(default="默认分组", max_length=64)


class DeviceUpdateRequest(BaseModel):
    """编辑设备元数据：设备 ID 不可改，状态仍由 MQTT 驱动。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model: Optional[str] = Field(default=None, max_length=64)
    firmware_version: Optional[str] = Field(default=None, max_length=32)
    group_name: Optional[str] = Field(default=None, max_length=64)


class DeviceBulkCreateRequest(BaseModel):
    """批量注册：单次最多 200 台，已存在的设备跳过而不是整批失败。"""

    devices: List[DeviceCreateRequest] = Field(min_length=1, max_length=200)


class DeviceBulkResult(BaseModel):
    created: List[str]
    skipped: List[str]


class DeviceGroupOut(BaseModel):
    group_name: str
    total: int
    online: int
    offline: int


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


ACK_NOTE_MAX_LENGTH = 500


def _normalize_ack_note(value: Optional[str]) -> Optional[str]:
    """备注只保留有内容的文本：去首尾空白，全空视为未填写。"""
    if value is None:
        return None
    text = value.strip()
    return text or None


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
    # v1.21.0 处置元数据；历史已确认行保持 None（无处置记录）
    ack_note: Optional[str] = None
    acked_by: Optional[str] = None
    acked_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AlertAckRequest(BaseModel):
    """单条确认入参（v1.21.0）：备注可选，旧客户端不传 body 照常工作。"""

    note: Optional[str] = Field(default=None, max_length=ACK_NOTE_MAX_LENGTH)

    @field_validator("note", mode="before")
    @classmethod
    def _clean_note(cls, value):
        return _normalize_ack_note(value)


class AlertBulkAckRequest(BaseModel):
    """批量确认入参：一次最多 100 条，重复 ID 在服务端去重。

    字段名沿用 REST 的 snake_case；移动端与 Web 后台都按此契约调用。
    v1.21.0 增加可选 `note`，同一批 open 告警写入相同的处置备注。
    """

    alert_ids: List[str] = Field(min_length=1, max_length=100)
    note: Optional[str] = Field(default=None, max_length=ACK_NOTE_MAX_LENGTH)

    @field_validator("note", mode="before")
    @classmethod
    def _clean_note(cls, value):
        return _normalize_ack_note(value)


class AlertBulkAckResult(BaseModel):
    """批量确认结果：逐类返回，部分失败对调用方可见。

    - acked：本次由 open 变为 acked 的告警；
    - already_acked：此前已确认，幂等跳过（不算失败）；
    - not_found：ID 不存在；
    - requested：去重后的请求条数。
    """

    acked: List[str]
    already_acked: List[str]
    not_found: List[str]
    requested: int


class CommandCreateRequest(BaseModel):
    type: str = Field(pattern="^(calibrate|ota|reboot|config)$")
    payload: Dict[str, Any] = Field(default_factory=dict)


class CommandBulkRequest(BaseModel):
    """批量指令（v1.28.0）：一次最多 200 台，逐台返回结果。

    `ota` 不在此端点受理：OTA 需要任务汇总与逐台进度，必须走 `/api/ota/tasks`。
    `request_id` 可选：调用方重放同一 ID（10 分钟内、同一操作人）返回首次结果，
    用于双击/网络重试时不重复下发。
    """

    device_ids: List[str] = Field(min_length=1, max_length=200)
    type: str = Field(pattern="^(calibrate|config|reboot)$")
    payload: Dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class CommandBulkItem(BaseModel):
    """批量指令中下发成功的设备。"""

    device_id: str
    cmd_id: str


class CommandBulkFailure(BaseModel):
    """批量指令中失败或跳过的设备；`reason` 为可直接展示的具体原因。"""

    device_id: str
    reason: str


class CommandBulkResult(BaseModel):
    """批量指令结果：成功 / 失败 / 跳过 三类，逐台可见。

    - success：指令已落库并投递到 MQTT；
    - failed：设备存在且可投递，但下发过程失败（如 Broker 断开）；
    - skipped：预检阶段被跳过（设备不存在 / 离线 / 无有效凭证）；
    - replayed：同一 `request_id` 的重复提交，返回首次结果且未重复下发。
    """

    batch_id: str
    type: str
    requested: int
    replayed: bool = False
    success: List[CommandBulkItem] = Field(default_factory=list)
    failed: List[CommandBulkFailure] = Field(default_factory=list)
    skipped: List[CommandBulkFailure] = Field(default_factory=list)


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
    device_ids: List[str] = Field(alias="deviceIds", min_length=1, max_length=200)
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


class TrendPointOut(BaseModel):
    """概览页 24 小时趋势的一个小时桶（真实数据，空桶计 0）。"""

    hour_start: datetime
    reports: int = 0
    alerts: int = 0


class AbnormalDeviceOut(BaseModel):
    """概览页异常设备：未处理告警或离线。"""

    device_id: str
    name: str
    status: str
    open_alert_count: int
    # alert / offline / alert+offline，客户端按此展示原因
    reason: str


class StatsOverviewOut(BaseModel):
    """GET /api/stats/overview：Web 指标卡与移动端概览共用一套口径。"""

    device_total: int
    device_online: int
    device_offline: int
    report_today: int
    alert_open: int
    # v1.27.0 概览页字段（在既有响应上追加，不新增第二套端点）
    device_credential_missing: int = 0
    alert_warning_open: int = 0
    alert_critical_open: int = 0
    trend: List[TrendPointOut] = Field(default_factory=list)
    # 窗口内是否覆盖满 24 小时；为 False 时客户端必须显示「数据不足」
    trend_complete: bool = False
    first_record_at: Optional[datetime] = None
    abnormal_devices: List[AbnormalDeviceOut] = Field(default_factory=list)


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


# ==================== 账号与权限 ====================

UserRole = Literal["admin", "operator", "viewer"]


class UserOut(BaseModel):
    username: str
    role: str
    active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreateRequest(BaseModel):
    """新建账号：用户名限定字母数字与 _.-，避免特殊字符带来的注入与展示问题。"""

    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    # 最小长度与弱口令由 user_service 按配置校验，返回可读的 400 文案
    password: str = Field(min_length=1, max_length=128)
    role: UserRole = "viewer"


class UserUpdateRequest(BaseModel):
    """更新账号：仅允许改角色、启停与重置口令，用户名与创建时间不可变。"""

    role: Optional[UserRole] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=1, max_length=128)


# ==================== 系统状态 ====================


class GatewayStatusOut(BaseModel):
    version: str
    started_at: datetime
    uptime_seconds: int


class MqttStatusOut(BaseModel):
    connected: bool
    host: str
    port: int
    messages_handled: int
    last_error: Optional[str] = None


class StorageStatusOut(BaseModel):
    """存储视角的容量快照：只给聚合结果，不下发原始时序数据。"""

    backend: str
    device_total: int
    device_online: int
    device_offline: int
    record_total: int
    record_oldest_at: Optional[datetime] = None
    record_newest_at: Optional[datetime] = None
    alert_total: int
    alert_open: int


class SchedulerStatusOut(BaseModel):
    """后台任务与业务参数，仅只读展示，修改仍走环境变量。"""

    offline_timeout_seconds: int
    offline_sweep_interval_seconds: int
    data_retention_days: int
    data_purge_interval_seconds: int
    alert_retention_days: int = 180
    audit_retention_days: int = 365
    alert_suppress_seconds: int


class NotifyStatusOut(BaseModel):
    webhook_enabled: bool
    webhook_target: str


class SystemStatusOut(BaseModel):
    gateway: GatewayStatusOut
    mqtt: MqttStatusOut
    storage: StorageStatusOut
    scheduler: SchedulerStatusOut
    notify: NotifyStatusOut
    generated_at: datetime
