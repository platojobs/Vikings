"""维护窗口（v1.39.0）。

三张表：
- `maintenance_window`：范围（条件树）/ 时间 / 周期 / 状态 / 创建人；
- `maintenance_suppression`：窗口内被抑制的「本会触发告警」上报记录；
- `maintenance_summary`：每次窗口发生结束后的汇总（含 critical 压制条数）。

Revision ID: 0014_maintenance
Revises: 0013_rule_conditions
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014_maintenance"
down_revision: Union[str, None] = "0013_rule_conditions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONPayload = sa.JSON().with_variant(JSONB, "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "maintenance_window" not in tables:
        op.create_table(
            "maintenance_window",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
            sa.Column("window_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("scope", JSONPayload, nullable=True),
            sa.Column("mode", sa.String(16), nullable=False, server_default="once"),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("recurrence", JSONPayload, nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="scheduled"),
            sa.Column("created_by", sa.String(64), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("idx_maintenance_status", "maintenance_window", ["status"])
        op.create_index("idx_maintenance_once_end", "maintenance_window", ["mode", "ends_at"])

    if "maintenance_suppression" not in tables:
        op.create_table(
            "maintenance_suppression",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
            sa.Column("window_id", sa.String(64), nullable=False),
            sa.Column("occurrence_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("device_id", sa.String(64), nullable=False),
            sa.Column("metric", sa.String(32), nullable=False),
            sa.Column("level", sa.String(16), nullable=False),
            sa.Column("value", sa.Numeric(10, 2), nullable=False),
            sa.Column("threshold", sa.Numeric(10, 2), nullable=False),
            sa.Column("rule_name", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index(
            "idx_maintenance_supp_window",
            "maintenance_suppression",
            ["window_id", "occurrence_start", "created_at"],
        )
        op.create_index(
            "idx_maintenance_supp_device", "maintenance_suppression", ["device_id", "created_at"]
        )
        op.create_index(
            "idx_maintenance_supp_metric", "maintenance_suppression", ["metric", "created_at"]
        )

    if "maintenance_summary" not in tables:
        op.create_table(
            "maintenance_summary",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
            sa.Column("window_id", sa.String(64), nullable=False),
            sa.Column("occurrence_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("occurrence_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("suppressed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("device_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("critical_suppressed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("devices", JSONPayload, nullable=False),
            sa.Column("metrics", JSONPayload, nullable=False),
            sa.Column("levels", JSONPayload, nullable=False),
            sa.Column("ended_early", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint(
                "window_id", "occurrence_start", name="uq_maintenance_summary_occurrence"
            ),
        )
        op.create_index("idx_maintenance_summary_created", "maintenance_summary", ["created_at"])


def downgrade() -> None:
    tables = _tables()
    if "maintenance_summary" in tables:
        op.drop_table("maintenance_summary")
    if "maintenance_suppression" in tables:
        op.drop_table("maintenance_suppression")
    if "maintenance_window" in tables:
        op.drop_table("maintenance_window")
