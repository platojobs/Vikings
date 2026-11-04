"""baseline：v1.18.0 及之前的完整数据库结构。

背景：v1.18.0 之前生产库由 `infra/postgres/init.sql` 与启动时 `create_all`
共同维护，且 `device` 表存在手写补列。本迁移把「当时的结构」固化为唯一基线，
后续所有变更只允许通过新的 revision 表达。

对已存在的生产库：
- 直接执行 `alembic upgrade head` 即可：本迁移检测到 `device` 表已存在时
  **不会重建表**，只把 baseline 记录为已应用，随后继续执行后续 revision；
- 也可以显式标记：`alembic stamp 0001_baseline`（见 docs/operations.md）。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite 用 JSON 文本，PostgreSQL 用 JSONB（可索引、写入更快）
JSONPayload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
# SQLite 的 BIGINT 主键不会自增，降级为 INTEGER 才等价
BigIntId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

CORE_TABLES = {
    "device",
    "sensor_record",
    "device_alert",
    "device_command",
    "ota_task",
    "rule_config",
    "audit_log",
    "app_user",
}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    if "device" in existing:
        missing = CORE_TABLES - existing
        if missing:
            raise RuntimeError(
                "检测到 device 表但缺少其余核心表："
                f"{sorted(missing)}。数据库处于半升级状态，请人工核对后再迁移。"
            )
        # 既有库（init.sql / create_all 建过表）：不重建，仅由 Alembic 记录本版本
        print("[baseline] 检测到既有 schema，跳过建表，直接标记为已应用")
        return

    op.create_table(
        "device",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model", sa.String(64), nullable=False, server_default="VG-S100"),
        sa.Column("firmware_version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("group_name", sa.String(64), nullable=False, server_default="默认分组"),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_online_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mqtt_username", sa.String(64), nullable=True),
        sa.Column("mqtt_credential_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mqtt_credential_fingerprint", sa.String(64), nullable=True),
        sa.Column("mqtt_credential_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mqtt_credential_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "sensor_record",
        sa.Column("id", BigIntId, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("temperature", sa.Numeric(10, 2), nullable=False),
        sa.Column("humidity", sa.Numeric(10, 2), nullable=False),
        sa.Column("voltage", sa.Numeric(10, 2), nullable=False),
        sa.Column("rssi", sa.Integer(), nullable=False),
        sa.Column("battery", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "seq", "ts", name="uq_sensor_device_seq_ts"),
    )
    op.create_index("idx_sensor_device_ts", "sensor_record", ["device_id", "ts"])

    op.create_table(
        "device_alert",
        sa.Column("id", BigIntId, primary_key=True, autoincrement=True),
        sa.Column("alert_id", sa.String(64), nullable=False, unique=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("threshold", sa.Numeric(10, 2), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("ai_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_alert_created", "device_alert", ["created_at"])
    op.create_index("idx_alert_device_metric", "device_alert", ["device_id", "metric", "created_at"])

    op.create_table(
        "device_command",
        sa.Column("cmd_id", sa.String(64), primary_key=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("payload", JSONPayload, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("state", sa.String(16), nullable=False, server_default="issued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.String(255), nullable=True),
        sa.Column("ota_task_id", sa.String(64), nullable=True),
        sa.Column("operator", sa.String(64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_command_device_created", "device_command", ["device_id", "created_at"])

    op.create_table(
        "ota_task",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("target_version", sa.String(32), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "rule_config",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", BigIntId, primary_key=True, autoincrement=True),
        sa.Column("operator", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("detail", JSONPayload, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_audit_created", "audit_log", ["created_at"])
    op.create_index("idx_audit_action_created", "audit_log", ["action", "created_at"])

    op.create_table(
        "app_user",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    """回退到空库。

    ⚠️ 生产环境不要执行 `alembic downgrade base`：这会删除全部业务表。
    回滚版本时请用升级前的 pg_dump（见 docs/operations.md）。
    """
    for table in (
        "app_user",
        "audit_log",
        "rule_config",
        "ota_task",
        "device_command",
        "device_alert",
        "sensor_record",
        "device",
    ):
        op.drop_table(table)
