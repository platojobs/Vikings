"""通知配置与投递记录表（v1.29.0）。

- notification_config：单行 id=1，Web 修改后下一次告警即按新配置投递，无需重启；
- notification_delivery：成功/最终失败/被静默抑制的留痕，索引支持按设备+指标查最近一次。

Revision ID: 0006_notifications
Revises: 0005_device_cred_revoked
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_notifications"
down_revision: Union[str, None] = "0005_device_cred_revoked"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # 可重入：测试库先 create_all 过时会话已存在
    if not _has_table("notification_config"):
        op.create_table(
            "notification_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("webhook_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("webhook_url", sa.Text(), nullable=True),
            sa.Column("webhook_secret", sa.String(128), nullable=True),
            sa.Column("webhook_timeout_seconds", sa.Numeric(6, 2), nullable=False, server_default="10"),
            sa.Column("webhook_max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("smtp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("smtp_host", sa.String(255), nullable=True),
            sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
            sa.Column("smtp_username", sa.String(255), nullable=True),
            sa.Column("smtp_password", sa.String(255), nullable=True),
            sa.Column("smtp_from", sa.String(255), nullable=True),
            sa.Column("smtp_to", sa.Text(), nullable=True),
            sa.Column("smtp_starttls", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("smtp_timeout_seconds", sa.Numeric(6, 2), nullable=False, server_default="10"),
            sa.Column("smtp_max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("levels", sa.String(32), nullable=False, server_default="critical"),
            sa.Column("groups", sa.Text(), nullable=False, server_default=""),
            sa.Column("device_allow", sa.Text(), nullable=False, server_default=""),
            sa.Column("device_deny", sa.Text(), nullable=False, server_default=""),
            sa.Column("mute_minutes", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("retry_backoff_seconds", sa.Numeric(6, 2), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        )

    if not _has_table("notification_delivery"):
        op.create_table(
            "notification_delivery",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("alert_id", sa.String(64), nullable=False),
            sa.Column("device_id", sa.String(64), nullable=False),
            sa.Column("metric", sa.String(32), nullable=False, server_default=""),
            sa.Column("level", sa.String(16), nullable=False, server_default=""),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("idx_notify_delivery_created", "notification_delivery", ["created_at"])
        op.create_index(
            "idx_notify_device_metric_created",
            "notification_delivery",
            ["device_id", "metric", "created_at"],
        )


def downgrade() -> None:
    if _has_table("notification_delivery"):
        op.drop_index("idx_notify_device_metric_created", table_name="notification_delivery")
        op.drop_index("idx_notify_delivery_created", table_name="notification_delivery")
        op.drop_table("notification_delivery")
    if _has_table("notification_config"):
        op.drop_table("notification_config")
