"""告警处置元数据（P2-2）。

给 `device_alert` 增加三列，回答「谁、何时、为什么确认」：

- ack_note：处置备注，可选，最长 500 字由 API 校验；
- acked_by：确认人（网关登录用户名）；
- acked_at：确认时间（UTC）。

三列均允许 NULL：
- 未确认的 open 行天然为空；
- v1.21.0 之前已确认的历史行无法得知真实操作人与时间，保持 NULL，
  不做「用 created_at 冒充 acked_at」的臆测回填；页面显示为「历史确认（无处置记录）」。
- 旧客户端不传备注时 note 为空，其余字段照常写入，保持向后兼容。

Revision ID: 0003_alert_ack_metadata
Revises: 0002_lifecycle_indexes
Create Date: 2026-11-05
"""

from typing import Dict, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_alert_ack_metadata"
down_revision: Union[str, None] = "0002_lifecycle_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "device_alert"

NEW_COLUMNS: Dict[str, sa.Column] = {
    "ack_note": sa.Column("ack_note", sa.Text(), nullable=True),
    "acked_by": sa.Column("acked_by", sa.String(length=64), nullable=True),
    "acked_at": sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
}


def _existing_columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    # 可重入：列已存在（例如测试库先 create_all 过）时跳过
    existing = _existing_columns(TABLE)
    for name, column in NEW_COLUMNS.items():
        if name in existing:
            print(f"[0003] 列 {TABLE}.{name} 已存在，跳过")
            continue
        op.add_column(TABLE, column)


def downgrade() -> None:
    existing = _existing_columns(TABLE)
    for name in reversed(list(NEW_COLUMNS)):
        if name in existing:
            op.drop_column(TABLE, name)
