"""告警 SLA、超时升级与值班交接（v1.33.0）。

- device_alert：新增 assignee（当前责任人）、sla_minutes / sla_deadline_at（创建时
  的响应时限快照）、escalation_count / last_escalated_at（升级防重复计数）；
- sla_config：单行配置，按级别响应时限 + 升级上限 + 升级间隔；
- notification_delivery：新增 kind（alert/escalation/handover）与 extra（升级/交接载荷快照）。

既有数据的回填口径：
- 升级前没有 SLA 配置，唯一可依据的是本版本默认值（critical 30 分钟 / warning 120 分钟），
  因此按级别回填 `sla_minutes` 与 `sla_deadline_at = created_at + sla_minutes`；
- `assignee` 保持 NULL（历史告警从未指派责任人，不臆造）；
- `escalation_count` 回填 0，不补发历史超时升级；
- 历史已确认但 `acked_at` 为 NULL 的行保持 NULL，读取时归为 `unknown`（不计达标）。

Revision ID: 0008_alert_sla_handover
Revises: 0007_notification_rules_retry
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_alert_sla_handover"
down_revision: Union[str, None] = "0007_notification_rules_retry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_CRITICAL_MINUTES = 30
DEFAULT_WARNING_MINUTES = 120

ALERT_COLUMNS = (
    ("assignee", sa.String(64), True),
    ("sla_minutes", sa.Integer(), True),
    ("sla_deadline_at", sa.DateTime(timezone=True), True),
    ("escalation_count", sa.Integer(), False),
    ("last_escalated_at", sa.DateTime(timezone=True), True),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _backfill_deadlines() -> None:
    """按级别回填历史告警的 SLA 快照（升级前无配置，只能用本版本默认值）。"""
    bind = op.get_bind()
    if "device_alert" not in _tables():
        return
    bind.execute(
        sa.text(
            "UPDATE device_alert SET sla_minutes = CASE "
            f"WHEN level = 'critical' THEN {DEFAULT_CRITICAL_MINUTES} "
            f"ELSE {DEFAULT_WARNING_MINUTES} END "
            "WHERE sla_minutes IS NULL"
        )
    )
    if bind.dialect.name == "sqlite":
        # SQLite 没有 make_interval，用 datetime 修饰符构造同样的 UTC 偏移。
        bind.execute(
            sa.text(
                "UPDATE device_alert "
                "SET sla_deadline_at = datetime(created_at, '+' || sla_minutes || ' minutes') "
                "WHERE sla_deadline_at IS NULL"
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE device_alert "
                "SET sla_deadline_at = created_at + make_interval(mins => sla_minutes) "
                "WHERE sla_deadline_at IS NULL"
            )
        )


def upgrade() -> None:
    tables = _tables()

    if "sla_config" not in tables:
        op.create_table(
            "sla_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "critical_minutes", sa.Integer(), nullable=False,
                server_default=str(DEFAULT_CRITICAL_MINUTES),
            ),
            sa.Column(
                "warning_minutes", sa.Integer(), nullable=False,
                server_default=str(DEFAULT_WARNING_MINUTES),
            ),
            sa.Column("max_escalations", sa.Integer(), nullable=False, server_default="2"),
            sa.Column(
                "escalation_interval_minutes", sa.Integer(), nullable=False, server_default="30"
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
            sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        )
        op.execute(
            sa.text(
                "INSERT INTO sla_config (id, updated_by) VALUES (1, 'system') "
                "ON CONFLICT (id) DO NOTHING"
            )
            if op.get_bind().dialect.name == "postgresql"
            else sa.text("INSERT OR IGNORE INTO sla_config (id, updated_by) VALUES (1, 'system')")
        )

    if "device_alert" in tables:
        existing = _columns("device_alert")
        for name, column_type, nullable in ALERT_COLUMNS:
            if name in existing:
                continue
            kwargs: dict = {"nullable": nullable}
            if name == "escalation_count":
                kwargs["server_default"] = "0"
            op.add_column("device_alert", sa.Column(name, column_type, **kwargs))
        _backfill_deadlines()
        indexes = {
            index["name"] for index in sa.inspect(op.get_bind()).get_indexes("device_alert")
        }
        if "idx_alert_status_sla_deadline" not in indexes:
            op.create_index(
                "idx_alert_status_sla_deadline", "device_alert", ["status", "sla_deadline_at"]
            )

    if "notification_delivery" in tables:
        existing = _columns("notification_delivery")
        if "kind" not in existing:
            op.add_column(
                "notification_delivery",
                sa.Column("kind", sa.String(16), nullable=False, server_default="alert"),
            )
        if "extra" not in existing:
            # SQLite 自动退化为 JSON 文本，与模型层的 JSONPayload 变体一致
            op.add_column(
                "notification_delivery",
                sa.Column("extra", sa.JSON().with_variant(JSONB, "postgresql"), nullable=True),
            )


def downgrade() -> None:
    tables = _tables()

    if "notification_delivery" in tables:
        existing = _columns("notification_delivery")
        with op.batch_alter_table("notification_delivery") as batch:
            if "extra" in existing:
                batch.drop_column("extra")
            if "kind" in existing:
                batch.drop_column("kind")

    if "device_alert" in tables:
        indexes = {
            index["name"] for index in sa.inspect(op.get_bind()).get_indexes("device_alert")
        }
        if "idx_alert_status_sla_deadline" in indexes:
            op.drop_index("idx_alert_status_sla_deadline", table_name="device_alert")
        existing = _columns("device_alert")
        with op.batch_alter_table("device_alert") as batch:
            for name, _type, _nullable in reversed(ALERT_COLUMNS):
                if name in existing:
                    batch.drop_column(name)

    if "sla_config" in tables:
        op.drop_table("sla_config")
