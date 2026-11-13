"""ORM 模型：与 infra/postgres/init.sql 保持一致。

核心业务三张表：device / sensor_record / device_alert
OTA 链路支撑两张表：device_command / ota_task
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# PostgreSQL 用 JSONB（可索引、写入更快），SQLite 自动退化为 JSON 文本
JSONPayload = JSON().with_variant(JSONB, "postgresql")


class Device(Base):
    """设备元数据表：状态完全由 MQTT 的 status/retained/LWT 报文驱动。"""

    __tablename__ = "device"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="VG-S100")
    firmware_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    # 设备分组：用于列表筛选与批量运维，默认归档到「默认分组」
    group_name: Mapped[str] = mapped_column(String(64), nullable=False, default="默认分组")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    last_report_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_online_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- MQTT 设备凭证元数据（Broker 侧口令文件存放 hash，本表不保存明文口令）----
    # username 恒等于 device_id（ACL 使用 %u 替换），凭证与设备 ID 强绑定
    mqtt_username: Mapped[str] = mapped_column(String(64), nullable=True)
    # 轮换版本号：每次签发/轮换 +1，便于审计与客户端判断是否需要更新
    mqtt_credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 口令 SHA-256 指纹（不可反推口令），用于对账「当前 Broker 凭证是哪一次签发」
    mqtt_credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=True)
    mqtt_credential_issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mqtt_credential_rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 吊销时间：NULL 表示凭证仍有效；吊销后需重新签发才会恢复接入能力
    mqtt_credential_revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def mqtt_credential_status(self) -> str:
        """管理端展示口径：missing（未签发）/ active（有效）/ revoked（已吊销）。"""
        if self.mqtt_credential_issued_at is None:
            return "missing"
        return "revoked" if self.mqtt_credential_revoked_at is not None else "active"

    @property
    def mqtt_credential_fingerprint_prefix(self) -> Optional[str]:
        """只暴露指纹短前缀（8 位十六进制），供人工对账，绝不返回完整指纹。"""
        if not self.mqtt_credential_fingerprint:
            return None
        return self.mqtt_credential_fingerprint[:8]


class DeviceTag(Base):
    """设备标签关联表（v1.35.0）：一台设备多个标签，一标签多设备。

    选择关联表而非 JSON 数组列：SQLite（测试）与 PostgreSQL（生产）行为一致，
    `(device_id, tag)` 复合主键强制唯一，`tag` 索引支持按标签筛选与标签计数；
    JSON 数组在 SQLite 上无法建索引，成员查询只能全表扫描且无法用主键约束重复。
    """

    __tablename__ = "device_tag"
    __table_args__ = (
        # 按标签反查设备（列表/告警/导出筛选的主路径）
        Index("idx_device_tag_tag", "tag", "device_id"),
    )

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default="system"
    )


class DeviceShadow(Base):
    """设备影子（v1.36.0）：期望状态 vs 上报状态 + 版本号 + 同步状态机。

    设计要点：
    - `desired`：云端期望设备达到的状态（如 `{"led": "on", "fan_speed": 3}`），
      由管理端写入；设备离线时也可写入，上线后由设备侧拉取并应用。
    - `reported`：设备实际应用并上报的状态；当前由 REST reported 端点触发更新。
    - `version`：每次 desired 变更 +1，设备侧据此判断是否是最新期望，避免乱序覆盖。
    - `sync_status`：由 shadow_service.compute_sync_status 派生，不落库，避免双写不一致。
      取 `device.status` 联合判断，区分 `pending` / `offline_pending` / `synced`。
    - 一行一设备，`device_id` 主键；不主动建外键，与 device_tag 一致的解耦风格。
    - 上限：单次最多 32 个键，键长 1~32，单值 JSON 序列化长度 ≤ 512；
      值必须是字符串 / 数字 / 布尔 / null，避免任意嵌套导致同步状态无法解释。
    """

    __tablename__ = "device_shadow"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    desired: Mapped[Dict[str, Any]] = mapped_column(
        JSONPayload, nullable=False, default=dict, server_default="{}"
    )
    reported: Mapped[Dict[str, Any]] = mapped_column(
        JSONPayload, nullable=False, default=dict, server_default="{}"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    desired_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    desired_updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reported_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 设备端上一次消费的 desired 版本号；0 表示尚未同步过任何版本
    reported_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DeviceShadowRevision(Base):
    """设备影子期望版本快照（v1.37.0）。

    每次 desired 真实变化后保存完整快照；回滚读取目标快照并生成一个新版本，
    不修改或删除既有历史。`(device_id, version)` 唯一，升级时仅为存量当前状态
    补 baseline，无法 reconstruct 的旧版本保持缺失。
    """

    __tablename__ = "device_shadow_revision"
    __table_args__ = (
        UniqueConstraint("device_id", "version", name="uq_shadow_revision_device_version"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    desired: Mapped[Dict[str, Any]] = mapped_column(
        JSONPayload, nullable=False, default=dict, server_default="{}"
    )
    changed_keys: Mapped[List[str]] = mapped_column(
        JSONPayload, nullable=False, default=list, server_default="[]"
    )
    # baseline / merge / clear / batch / rollback
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class SensorRecord(Base):
    """传感器时序数据表：设备每次唤醒上报一条。"""

    __tablename__ = "sensor_record"
    __table_args__ = (
        # 幂等去重：QoS1 可能重复投递，同设备同 seq 同 ts 视为同一条
        UniqueConstraint("device_id", "seq", "ts", name="uq_sensor_device_seq_ts"),
        Index("idx_sensor_device_ts", "device_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    humidity: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    voltage: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    rssi: Mapped[int] = mapped_column(Integer, nullable=False)
    battery: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceAlert(Base):
    """AI 异常诊断告警表。"""

    __tablename__ = "device_alert"
    __table_args__ = (
        Index("idx_alert_created", "created_at"),
        Index("idx_alert_device_metric", "device_id", "metric", "created_at"),
        # 告警中心默认按 status + 时间倒序；level 可单独筛选（v1.19.0）
        Index("idx_alert_status_created", "status", "created_at"),
        Index("idx_alert_level_created", "level", "created_at"),
        # 移动端/看板按设备聚合未处理告警（status + device_id）
        Index("idx_alert_status_device", "status", "device_id"),
        # v1.33.0：超时升级巡检与「已超时」筛选按 status + 截止时间扫描
        Index("idx_alert_status_sla_deadline", "status", "sla_deadline_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    # v1.34.0：命中的告警规则快照（规则 ID/名称、比较方向、阈值、级别、实际值、连续次数）。
    # 规则后续被修改或删除也不影响历史告警的「为什么触发」解释；v1.34.0 之前的历史行为 NULL。
    matched_rule: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONPayload, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 处置元数据（v1.21.0）：回答「谁、何时、为什么确认」。
    # 历史已确认行没有这些信息，保持 NULL，不做臆测回填。
    ack_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- SLA 与值班交接（v1.33.0）----
    # 当前责任人：创建时为空（未指派），值班交接时批量改写；与 acked_by 不同，
    # 它表示「谁现在负责处理」，不是「谁确认过」。
    assignee: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 响应时限快照：创建时按级别从 SLA 配置写入，之后修改配置不影响历史告警口径。
    sla_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sla_deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 已发出的超时升级次数：巡检以「条件自增」原子领取升级名额，防止重复轰炸。
    escalation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_escalated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DeviceCommand(Base):
    """云端指令及设备回执状态机：issued → received → progress → success/failed。"""

    __tablename__ = "device_command"
    __table_args__ = (Index("idx_command_device_created", "device_id", "created_at"),)

    cmd_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONPayload, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(String(255), nullable=True)
    ota_task_id: Mapped[str] = mapped_column(String(64), nullable=True)
    operator: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OtaTask(Base):
    """OTA 任务表：一次任务覆盖多台设备，汇总字段供列表页快速展示。"""

    __tablename__ = "ota_task"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RuleConfig(Base):
    """告警阈值运行时覆盖表：优先级高于环境变量，在线修改立即生效。

    key 与 Settings 的 threshold_* 字段同名，value 存 JSON 数字。
    """

    __tablename__ = "rule_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AlertRule(Base):
    """可配置告警阈值规则（v1.34.0）。

    一行一条规则：指标 + 比较方向 + 阈值 + 级别 + 生效范围。
    - `is_default=True` 的 10 条规则由 v1.34.0 迁移/启动补齐，值取自 v1.33.0 的
      环境变量或 rule_config 覆盖，保证升级前后判定结果一致；
    - 同一指标多条规则同时命中时取最高级别（critical 胜 warning），同级按
      `priority` 升序、再按 id 升序，每指标只产一条告警（见 docs/contracts.md）。
    """

    __tablename__ = "alert_rule"
    __table_args__ = (
        Index("idx_alert_rule_metric", "metric"),
        Index("idx_alert_rule_scope", "scope_type", "scope_value"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    # lt = 低于阈值触发，gt = 高于阈值触发
    op: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # 仅 warning / critical（沿用 v1.21.0 收窄后的级别口径）
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    # global / group / device / tag（v1.35.0 增加 tag）
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    # v1.34.0 单值范围：保留以兼容旧客户端与 downgrade；多值规则存首个值
    scope_value: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # v1.35.0 多值范围：global 为 None，其余为字符串数组（值之间为「或」）
    scope_values: Mapped[Optional[List[str]]] = mapped_column(JSONPayload, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 连续 N 次越限才触发（1 表示首次越限即触发，与 v1.33.0 行为一致）
    consecutive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 同级命中时的裁决顺序：数字越小越优先
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system", server_default="system")


class AuditLog(Base):
    """操作审计日志：记录所有会改变系统状态的管理动作，便于追溯。"""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_action_created", "action", "created_at"),
        # 审计列表支持按操作人筛选（v1.19.0）
        Index("idx_audit_operator_created", "operator", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail: Mapped[Dict[str, Any]] = mapped_column(JSONPayload, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshToken(Base):
    """刷新令牌表：服务端保存令牌指纹（不存明文），支持登出/改密/管理员吊销。

    轮换语义：每次刷新都会把旧行标记为 revoked（reason=rotated）并签发新行，
    旧令牌不可再次使用；过期行保留用于审计，不做物理删除。
    """

    __tablename__ = "refresh_token"
    __table_args__ = (
        Index("idx_refresh_token_username", "username"),
        Index("idx_refresh_token_expires", "expires_at"),
    )

    # 令牌指纹（SHA-256 hex），主键即唯一索引；明文只在响应中出现一次
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # revoked_reason: logout / rotated / password_change / admin_revoke / reuse_detected
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 轮换后指向新令牌指纹，便于审计追链（不反向保存明文）
    replaced_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class NotificationConfig(Base):
    """告警通知配置（v1.29.0）：单行 id=1，运行时热读取，不依赖重启。

    Webhook URL/签名密钥、SMTP 口令属于敏感配置：只落库，接口返回时脱敏，
    审计与日志只记录字段名，不记录值。
    """

    __tablename__ = "notification_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    webhook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    webhook_timeout_seconds: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=10)
    webhook_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    smtp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    smtp_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    smtp_from: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 逗号分隔的收件人列表
    smtp_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    smtp_starttls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    smtp_timeout_seconds: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=10)
    smtp_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # 规则：级别（逗号分隔 warning/critical）、分组白名单、设备白/黑名单（逗号分隔）
    levels: Mapped[str] = mapped_column(String(32), nullable=False, default="critical")
    groups: Mapped[str] = mapped_column(Text, nullable=False, default="")
    device_allow: Mapped[str] = mapped_column(Text, nullable=False, default="")
    device_deny: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 同设备同指标在 N 分钟内只通知一次；0 表示不去重
    mute_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    retry_backoff_seconds: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=1)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class NotificationRule(Base):
    """通知规则（v1.31.0）：一条规则 = 一组接收人 + 级别门槛 + 分组范围。

    路由语义：按 `priority` 升序（数字越小越优先）、同优先级按 `id` 升序取
    **第一条命中的规则**；命中后不再尝试后续规则，避免同一告警重复打扰。
    """

    __tablename__ = "notification_rule"
    __table_args__ = (Index("idx_notify_rule_priority", "priority", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 数字越小优先级越高；同优先级按 id 升序（先创建者优先）
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # 触发条件（与 v1.29.0 组合规则一致）：级别、分组白名单、设备白/黑名单
    levels: Mapped[str] = mapped_column(String(32), nullable=False, default="critical")
    groups: Mapped[str] = mapped_column(Text, nullable=False, default="")
    device_allow: Mapped[str] = mapped_column(Text, nullable=False, default="")
    device_deny: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 该规则自己的接收人：Webhook URL/密钥、SMTP 收件人（SMTP 服务器沿用全局配置）
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    webhook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    smtp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    smtp_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class SlaConfig(Base):
    """告警 SLA 单行配置（v1.33.0）：按级别响应时限 + 超时升级策略。

    存库热读取，Web「系统设置」修改后下一轮巡检立即生效；修改不回写历史告警，
    因为每条告警在创建时已快照自己的 `sla_minutes` / `sla_deadline_at`。
    """

    __tablename__ = "sla_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 从告警创建到确认的响应时限；critical 默认更短，warning 更长
    critical_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    warning_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    # 升级上限：0 表示不升级；达到上限后停止升级但告警仍标记「已超时」
    max_escalations: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # 第 N 次升级的触发时间 = 截止时间 + (N-1) * 该间隔；默认 30 分钟
    escalation_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class NotificationDelivery(Base):
    """通知投递记录（v1.31.0 起同时是持久化重试队列）。

    - `pending`：已落库、等待投递或等待下一次重试（网关重启后由调度器续投）；
    - `sent` / `failed`：终态，`failed` 的 `error` 保留最后一次失败原因；
    - `suppressed`：被静默窗口或规则未命中抑制，不重试。

    去重：每条记录 `id` 即投递唯一键，Webhook 以 `X-Vikings-Delivery-Id` 下发，
    邮件使用确定性 `Message-ID`，接收端可按该键做幂等。
    """

    __tablename__ = "notification_delivery"
    __table_args__ = (
        Index("idx_notify_delivery_created", "created_at"),
        Index("idx_notify_device_metric_created", "device_id", "metric", "created_at"),
        # 重试调度器按状态 + 下次尝试时间扫描
        Index("idx_notify_delivery_status_next", "status", "next_attempt_at"),
        # 历史页按通道 + 时间筛选
        Index("idx_notify_delivery_channel_created", "channel", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    alert_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # webhook / smtp / mute（静默）/ rule（未命中规则）
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # pending / sent / failed / suppressed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 投递类型（v1.33.0）：alert=首条告警 / escalation=超时升级 / handover=交接汇总。
    # 与 channel 正交，便于历史页区分「这是升级通知还是首次告警」。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="alert", server_default="alert")
    # 附加载荷快照：升级的「第几次/原 SLA」或交接的「交接人/接收人/条数/时间窗口」。
    # 与告警正文分开存，网关重启后重试仍能还原完整通知内容。
    extra: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONPayload, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # 路由快照：规则与分组在投递发生时记录，规则后来改名/变更不影响历史解释
    rule_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rule_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # 累计投递耗时（毫秒，多次尝试累加），供历史页展示
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下一次重试时间；终态为 NULL
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AppUser(Base):
    """后台账号表：角色驱动菜单与接口权限，口令只存 PBKDF2 哈希。"""

    __tablename__ = "app_user"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
