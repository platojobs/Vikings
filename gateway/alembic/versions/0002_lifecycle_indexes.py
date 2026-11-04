"""告警/审计查询索引（P1-2）。

依据真实查询形态（gateway/app/services/alert_service.py、audit_service.py）：

- 告警中心：`WHERE status=? ORDER BY created_at DESC`（status 可省略，level 也可单选）
- 移动端角标：`WHERE status='open' AND device_id IN (...) GROUP BY device_id`
- 审计列表：`WHERE operator=? / action=? ORDER BY created_at DESC`

v1.18.0 只有 `created_at`、`(device_id, metric, created_at)`、`(action, created_at)`，
status/level/operator 过滤只能靠顺序扫描。本迁移补齐四个覆盖索引。

Revision ID: 0002_lifecycle_indexes
Revises: 0001_baseline
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_lifecycle_indexes"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_INDEXES = (
    ("idx_alert_status_created", "device_alert", ["status", "created_at"]),
    ("idx_alert_level_created", "device_alert", ["level", "created_at"]),
    ("idx_alert_status_device", "device_alert", ["status", "device_id"]),
    ("idx_audit_operator_created", "audit_log", ["operator", "created_at"]),
)


def _existing_indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    # 可重入：索引已存在（例如手工加过）时跳过，避免重复执行报错
    for name, table, columns in NEW_INDEXES:
        if name in _existing_indexes(table):
            print(f"[0002] 索引 {name} 已存在，跳过")
            continue
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(NEW_INDEXES):
        if name in _existing_indexes(table):
            op.drop_index(name, table_name=table)
