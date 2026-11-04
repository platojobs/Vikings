"""ORM 模型：与 infra/postgres/init.sql 保持一致。

核心业务三张表：device / sensor_record / device_alert
OTA 链路支撑两张表：device_command / ota_task
"""

from datetime import datetime
from typing import Any, Dict, Optional

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 处置元数据（v1.21.0）：回答「谁、何时、为什么确认」。
    # 历史已确认行没有这些信息，保持 NULL，不做臆测回填。
    ack_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
