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
    # v1.35.0 设备标签（多值，按字母序）；列表接口批量填充，默认空数组保持旧结构
    tags: List[str] = []

    model_config = {"from_attributes": True}


class DeviceTagOut(BaseModel):
    """标签列表项：标签名与持有该标签的设备数（v1.35.0）。"""

    tag: str
    device_count: int


class DeviceTagBatchRequest(BaseModel):
    """批量打标签/移除标签：逐设备返回真实状态，不静默成功。"""

    device_ids: List[str] = Field(min_length=1, max_length=200)
    add: List[str] = Field(default_factory=list, max_length=50)
    remove: List[str] = Field(default_factory=list, max_length=50)


class DeviceTagBatchItem(BaseModel):
    device_id: str
    # updated / unchanged / not_found / over_limit
    status: str
    added: List[str] = []
    removed: List[str] = []
    tag_count: int = 0
    error: Optional[str] = None


class DeviceTagBatchResult(BaseModel):
    results: List[DeviceTagBatchItem]
    updated: int
    unchanged: int
    not_found: int
    over_limit: int
    added: int
    removed: int


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

    # v1.33.0 SLA 与值班交接：sla_state 为服务端统一口径
    # met / late / pending / overdue / unknown（历史确认无 acked_at）
    assignee: Optional[str] = None
    sla_minutes: Optional[int] = None
    sla_deadline_at: Optional[datetime] = None
    sla_state: Optional[str] = None
    # 未处理：距截止还剩多少秒（负数表示已超出）；已确认：实际响应秒数
    sla_remaining_seconds: Optional[int] = None
    sla_response_seconds: Optional[int] = None
    escalation_count: int = 0

    # v1.34.0：命中的规则快照（历史告警为 None，前端按「默认阈值（历史）」展示）
    matched_rule: Optional[Dict[str, Any]] = None

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


# ==================== 告警 SLA 与值班交接（v1.33.0） ====================


class SlaConfigOut(BaseModel):
    critical_minutes: int
    warning_minutes: int
    max_escalations: int
    escalation_interval_minutes: int
    updated_at: Optional[datetime] = None
    updated_by: str = "system"


class SlaConfigUpdate(BaseModel):
    """只允许修改四个阈值；约束防止把 SLA 配成 0 分钟或无限升级。"""

    critical_minutes: Optional[int] = Field(default=None, ge=1, le=10080)
    warning_minutes: Optional[int] = Field(default=None, ge=1, le=10080)
    max_escalations: Optional[int] = Field(default=None, ge=0, le=10)
    escalation_interval_minutes: Optional[int] = Field(default=None, ge=1, le=10080)


class AlertHandoverRequest(BaseModel):
    """值班交接入参：把未处理告警的责任人从 `from_user` 交接到 `to`。

    `from_user` 省略时默认当前登录操作人；`include_unassigned` 默认 true，
    表示没有责任人的未处理告警也一并交给下一班（不留无人认领的告警）。
    """

    to: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    from_user: Optional[str] = Field(default=None, min_length=2, max_length=64)
    include_unassigned: bool = True
    note: Optional[str] = Field(default=None, max_length=ACK_NOTE_MAX_LENGTH)

    @field_validator("note", mode="before")
    @classmethod
    def _clean_note(cls, value):
        return _normalize_ack_note(value)


class AlertHandoverResult(BaseModel):
    from_user: str
    to: str
    count: int
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    notification: str = "queued"


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


class BatteryTrendPointOut(BaseModel):
    """电池健康看板：某一天的整机队平均电量（真实数据，无上报的日期不返回）。"""

    day_start: datetime
    avg_battery: float
    samples: int


class BatteryRiskDeviceOut(BaseModel):
    """电池健康看板：单台设备的续航画像。

    `battery` / `voltage` / `last_report_at` 取窗口内最新一条上报；
    `drain_per_day` 为窗口内日平均电量的最小二乘斜率（百分点/天，仅正衰减时给出）；
    `days_to_critical` = 最新电量到严重阈值（15%）按当前衰减速度的预计天数。
    """

    device_id: str
    name: str
    group_name: str
    status: str
    battery: Optional[int] = None
    voltage: Optional[float] = None
    last_report_at: Optional[datetime] = None
    drain_per_day: Optional[float] = None
    days_to_critical: Optional[float] = None
    risk: Literal["critical", "warning", "healthy", "unknown"]
    open_alert_count: int = 0


class BatteryGroupSummaryOut(BaseModel):
    """电池健康看板：按设备分组汇总的风险分层。"""

    group_name: str
    total: int
    critical: int
    warning: int
    healthy: int
    unknown: int
    avg_battery: Optional[float] = None


class BatteryHealthOut(BaseModel):
    """电池健康与续航看板（v1.32.0）。

    口径均为真实上报数据：窗口内无任何记录的设备记为 `unknown`，不做臆测补值。
    """

    device_total: int
    battery_known: int
    critical: int
    warning: int
    healthy: int
    unknown: int
    avg_battery: Optional[float] = None
    # 分层阈值随响应返回，前端只展示不自行硬编码
    warning_percent: int
    critical_percent: int
    window_days: int
    window_start: datetime
    trend: List[BatteryTrendPointOut] = Field(default_factory=list)
    groups: List[BatteryGroupSummaryOut] = Field(default_factory=list)
    watchlist: List[BatteryRiskDeviceOut] = Field(default_factory=list)
    # 处于 critical + warning 的设备总数（watchlist 只返回前 limit 台）
    watchlist_total: int = 0


class NotificationConfigUpdate(BaseModel):
    """通知配置更新（v1.29.0）：未传字段保持不变。

    敏感字段（webhook_url / webhook_secret / smtp_password）语义：
    - 不传或 `null`：保持原值；
    - 空字符串：清空；
    - 有值：覆盖。
    """

    enabled: Optional[bool] = None
    webhook_enabled: Optional[bool] = None
    webhook_url: Optional[str] = Field(default=None, max_length=1024)
    webhook_secret: Optional[str] = Field(default=None, max_length=128)
    webhook_timeout_seconds: Optional[float] = Field(default=None, ge=1, le=60)
    webhook_max_attempts: Optional[int] = Field(default=None, ge=1, le=10)

    smtp_enabled: Optional[bool] = None
    smtp_host: Optional[str] = Field(default=None, max_length=255)
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_username: Optional[str] = Field(default=None, max_length=255)
    smtp_password: Optional[str] = Field(default=None, max_length=255)
    smtp_from: Optional[str] = Field(default=None, max_length=255)
    smtp_to: Optional[str] = Field(default=None, max_length=1024)
    smtp_starttls: Optional[bool] = None
    smtp_timeout_seconds: Optional[float] = Field(default=None, ge=1, le=60)
    smtp_max_attempts: Optional[int] = Field(default=None, ge=1, le=10)

    levels: Optional[List[Literal["warning", "critical"]]] = None
    groups: Optional[List[str]] = None
    device_allow: Optional[List[str]] = None
    device_deny: Optional[List[str]] = None
    mute_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    retry_backoff_seconds: Optional[float] = Field(default=None, ge=0, le=60)


class NotificationConfigOut(BaseModel):
    """通知配置对外视图：URL/密钥/口令只返回「是否已配置」，绝不回显明文。"""

    enabled: bool
    webhook_enabled: bool
    webhook_url_configured: bool
    webhook_secret_configured: bool
    webhook_timeout_seconds: float
    webhook_max_attempts: int
    smtp_enabled: bool
    smtp_host: Optional[str] = None
    smtp_port: int
    smtp_username: Optional[str] = None
    smtp_password_configured: bool
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None
    smtp_starttls: bool
    smtp_timeout_seconds: float
    smtp_max_attempts: int
    levels: List[str]
    groups: List[str]
    device_allow: List[str]
    device_deny: List[str]
    mute_minutes: int
    retry_backoff_seconds: float
    updated_at: Optional[datetime] = None
    updated_by: str = "system"


class NotificationRuleCreate(BaseModel):
    """新建通知规则（v1.31.0）。

    `priority` 数字越小越优先；同一告警只命中第一条匹配规则。
    """

    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    priority: int = Field(default=100, ge=1, le=1000)
    levels: List[Literal["warning", "critical"]] = Field(default_factory=lambda: ["critical"])
    groups: List[str] = Field(default_factory=list)
    device_allow: List[str] = Field(default_factory=list)
    device_deny: List[str] = Field(default_factory=list)
    webhook_enabled: bool = False
    webhook_url: Optional[str] = Field(default=None, max_length=1024)
    webhook_secret: Optional[str] = Field(default=None, max_length=128)
    smtp_enabled: bool = False
    smtp_to: Optional[str] = Field(default=None, max_length=1024)


class NotificationRuleUpdate(BaseModel):
    """更新规则：未传字段保持不变；敏感字段传空串表示清空。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=1, le=1000)
    levels: Optional[List[Literal["warning", "critical"]]] = None
    groups: Optional[List[str]] = None
    device_allow: Optional[List[str]] = None
    device_deny: Optional[List[str]] = None
    webhook_enabled: Optional[bool] = None
    webhook_url: Optional[str] = Field(default=None, max_length=1024)
    webhook_secret: Optional[str] = Field(default=None, max_length=128)
    smtp_enabled: Optional[bool] = None
    smtp_to: Optional[str] = Field(default=None, max_length=1024)


class NotificationRuleOut(BaseModel):
    """规则对外视图：Webhook URL / 密钥只返回「是否已配置」；收件人沿用 v1.29 口径可见。"""

    id: int
    name: str
    enabled: bool
    priority: int
    levels: List[str]
    groups: List[str]
    device_allow: List[str]
    device_deny: List[str]
    webhook_enabled: bool
    webhook_url_configured: bool
    webhook_secret_configured: bool
    smtp_enabled: bool
    smtp_to: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: str = "system"


class NotificationDeliveryOut(BaseModel):
    id: int
    alert_id: str
    device_id: str
    metric: str
    level: str
    channel: str
    status: str
    attempts: int
    # v1.33.0：alert / escalation / handover，用于历史页区分通知类型
    kind: str = "alert"
    error: Optional[str] = None
    # v1.31.0：路由快照与重试/耗时信息
    rule_id: Optional[int] = None
    rule_name: Optional[str] = None
    group_name: Optional[str] = None
    duration_ms: int = 0
    next_attempt_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class NotificationDeliveryPageOut(BaseModel):
    """投递历史分页：与服务端同一套筛选语义，total 供前端分页器使用。"""

    items: List[NotificationDeliveryOut]
    total: int
    page: int
    page_size: int


class NotificationTestRequest(BaseModel):
    channel: Literal["webhook", "smtp", "all"] = "all"
    # 指定规则可测试该规则的接收人；不传则用优先级最高且已配置该通道的规则
    rule_id: Optional[int] = None


class NotificationTestResult(BaseModel):
    channel: str
    status: str
    attempts: int
    error: Optional[str] = None


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


class AlertRuleCreate(BaseModel):
    """新建告警规则（v1.34.0）：字段范围按指标物理意义约束，越界返回 400。

    NaN/Inf 不在 Pydantic 层拦截（带 NaN 的 422 响应本身无法序列化），
    由服务层统一校验并返回可读的 400。
    """

    name: str = Field(min_length=1, max_length=64)
    metric: Literal["voltage", "temperature", "humidity", "rssi"]
    op: Literal["lt", "gt"]
    threshold: float
    level: Literal["warning", "critical"]
    scope_type: Literal["global", "group", "device", "tag"] = "global"
    scope_value: Optional[str] = Field(default=None, max_length=64)
    # v1.35.0 多值范围（值之间为「或」）；与 scope_value 二选一，传数组时优先
    scope_values: Optional[List[str]] = Field(default=None, max_length=50)
    enabled: bool = True
    consecutive_count: int = Field(default=1, ge=1, le=20)
    priority: int = Field(default=100, ge=1, le=1000)


class AlertRuleUpdate(BaseModel):
    """更新告警规则：未传字段保持不变；其余约束与新建一致。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    metric: Optional[Literal["voltage", "temperature", "humidity", "rssi"]] = None
    op: Optional[Literal["lt", "gt"]] = None
    threshold: Optional[float] = None
    level: Optional[Literal["warning", "critical"]] = None
    scope_type: Optional[Literal["global", "group", "device", "tag"]] = None
    # 显式传空串表示把范围改回全局；null 表示保持原值（与通知规则的「未传不变」一致）
    scope_value: Optional[str] = Field(default=None, max_length=64)
    scope_values: Optional[List[str]] = Field(default=None, max_length=50)
    enabled: Optional[bool] = None
    consecutive_count: Optional[int] = Field(default=None, ge=1, le=20)
    priority: Optional[int] = Field(default=None, ge=1, le=1000)


class AlertRuleEnabledUpdate(BaseModel):
    """启用/停用：独立端点，避免把「只改开关」误做成整条规则覆盖。"""

    enabled: bool


class AlertRuleOut(BaseModel):
    id: int
    name: str
    metric: str
    op: str
    threshold: float
    level: str
    scope_type: str
    scope_value: Optional[str] = None
    # 多值范围：旧客户端读 scope_value，新客户端优先读 scope_values
    scope_values: Optional[List[str]] = None
    enabled: bool
    consecutive_count: int
    priority: int
    is_default: bool
    updated_by: str = "system"
    updated_at: Optional[datetime] = None
    # 前端展示「最近修改人/时间」时无需再查审计

    model_config = {"from_attributes": True}


class AlertRuleHistoryEntry(BaseModel):
    """规则变更历史：直接读既有审计，不新造平行记录（v1.35.0）。

    `changes` 为逐字段前后值，规则字段本身不含密钥，可安全展示；
    敏感内容（口令/令牌）不会进入规则审计。
    """

    id: int
    action: str
    operator: str
    created_at: datetime
    fields: List[str] = []
    changes: Dict[str, Dict[str, Any]] = {}


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
    # v1.33.0：未处理中已超过响应时限的条数（其余未处理按 pending 计）
    overdue_open: int = 0


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
