"""通知规则与持久化重试队列（v1.31.0）。

- notification_rule：多规则路由，每条规则有独立接收人、级别门槛与分组范围；
- notification_delivery：增加 pending 队列所需字段（next_attempt_at / finished_at /
  duration_ms）与路由快照（rule_id / rule_name / group_name）；
- 从 v1.29.0 的单行配置回填一条「默认规则」，升级后行为与原配置等价。

Revision ID: 0007_notification_rules_retry
Revises: 0006_notifications
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_notification_rules_retry"
down_revision: Union[str, None] = "0006_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_DELIVERY_COLUMNS = (
    ("rule_id", sa.Integer(), True),
    ("rule_name", sa.String(64), True),
    ("group_name", sa.String(64), True),
    ("duration_ms", sa.Integer(), False),
    ("next_attempt_at", sa.DateTime(timezone=True), True),
    ("finished_at", sa.DateTime(timezone=True), True),
    # v1.29.0 的 delivery 表没有 updated_at；重试队列需要它记录最后一次尝试时间。
    # 模型上 nullable=False + server_default=now()，迁移用 batch 模式补列，
    # 以便 SQLite（不允许在 ADD COLUMN 里使用 CURRENT_TIMESTAMP 默认值）也能回填。
    ("updated_at", sa.DateTime(timezone=True), False),
)
NEW_DELIVERY_INDEXES = {
    "idx_notify_delivery_status_next": ("status", "next_attempt_at"),
    "idx_notify_delivery_channel_created": ("channel", "created_at"),
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _seed_default_rule() -> None:
    """把 v1.29.0 的单行配置转成一条规则，保证升级前后行为一致。

    只有配置里真的填了接收人时才回填；否则保持「无规则」，升级后所有告警
    记为 suppressed（不投递），与升级前「通道未启用 = 不发」一致。
    """
    bind = op.get_bind()
    if "notification_config" not in _tables():
        return
    config = bind.execute(
        sa.text(
            "SELECT id, webhook_enabled, webhook_url, webhook_secret, smtp_enabled, smtp_to, "
            "levels, groups, device_allow, device_deny FROM notification_config"
        )
    ).mappings().first()
    if config is None:
        return
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM notification_rule")).scalar() or 0
    if int(existing) > 0:
        return

    webhook_ready = bool(config["webhook_enabled"] and config["webhook_url"])
    smtp_ready = bool(config["smtp_enabled"] and config["smtp_to"])
    if not webhook_ready and not smtp_ready:
        return

    bind.execute(
        sa.text(
            "INSERT INTO notification_rule "
            "(name, enabled, priority, levels, groups, device_allow, device_deny, "
            " webhook_enabled, webhook_url, webhook_secret, smtp_enabled, smtp_to, "
            " created_at, updated_at, updated_by) "
            "VALUES (:name, true, 100, :levels, :groups, :allow, :deny, "
            " :webhook_enabled, :webhook_url, :webhook_secret, :smtp_enabled, :smtp_to, "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'system')"
        ),
        {
            "name": "默认规则（迁移自 v1.29.0 配置）",
            "levels": config["levels"] or "critical",
            "groups": config["groups"] or "",
            "allow": config["device_allow"] or "",
            "deny": config["device_deny"] or "",
            "webhook_enabled": bool(config["webhook_enabled"]),
            "webhook_url": config["webhook_url"],
            "webhook_secret": config["webhook_secret"],
            "smtp_enabled": bool(config["smtp_enabled"]),
            "smtp_to": config["smtp_to"],
        },
    )


def upgrade() -> None:
    tables = _tables()

    if "notification_rule" not in tables:
        op.create_table(
            "notification_rule",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("levels", sa.String(32), nullable=False, server_default="critical"),
            sa.Column("groups", sa.Text(), nullable=False, server_default=""),
            sa.Column("device_allow", sa.Text(), nullable=False, server_default=""),
            sa.Column("device_deny", sa.Text(), nullable=False, server_default=""),
            sa.Column("webhook_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("webhook_url", sa.Text(), nullable=True),
            sa.Column("webhook_secret", sa.String(128), nullable=True),
            sa.Column("smtp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("smtp_to", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        )
        op.create_index("idx_notify_rule_priority", "notification_rule", ["priority", "id"])

    if "notification_delivery" in tables:
        existing = _columns("notification_delivery")
        for name, column_type, nullable in NEW_DELIVERY_COLUMNS:
            if name in existing:
                continue
            if name == "updated_at":
                # SQLite 的 ADD COLUMN 不接受 CURRENT_TIMESTAMP 默认值，
                # batch 模式在 SQLite 下重建表、在 PostgreSQL 下仍是普通 ALTER。
                with op.batch_alter_table("notification_delivery") as batch:
                    batch.add_column(
                        sa.Column(
                            "updated_at",
                            column_type,
                            nullable=False,
                            server_default=sa.func.now(),
                        )
                    )
                continue
            kwargs = {"nullable": nullable}
            if name == "duration_ms":
                kwargs["server_default"] = "0"
            op.add_column("notification_delivery", sa.Column(name, column_type, **kwargs))

        existing_indexes = _indexes("notification_delivery")
        for index_name, columns in NEW_DELIVERY_INDEXES.items():
            if index_name not in existing_indexes:
                op.create_index(index_name, "notification_delivery", list(columns))

    _seed_default_rule()


def downgrade() -> None:
    tables = _tables()

    if "notification_delivery" in tables:
        existing_indexes = _indexes("notification_delivery")
        for index_name in NEW_DELIVERY_INDEXES:
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="notification_delivery")
        existing = _columns("notification_delivery")
        for name, _type, _nullable in reversed(NEW_DELIVERY_COLUMNS):
            if name in existing:
                op.drop_column("notification_delivery", name)

    if "notification_rule" in tables:
        if "idx_notify_rule_priority" in _indexes("notification_rule"):
            op.drop_index("idx_notify_rule_priority", table_name="notification_rule")
        op.drop_table("notification_rule")
