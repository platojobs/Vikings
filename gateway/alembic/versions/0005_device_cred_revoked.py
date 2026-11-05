"""device.mqtt_credential_revoked_at：记录设备凭证吊销时间。

- NULL：凭证从未签发或仍有效；
- 非 NULL：该凭证已被吊销，设备必须重新签发才能接入 Broker；
- 仅新增一列，不改动既有数据，存量设备默认 NULL（按「未吊销」解释）。

Revision ID: 0005_device_cred_revoked
Revises: 0004_refresh_tokens
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_device_cred_revoked"
down_revision: Union[str, None] = "0004_refresh_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "device"
COLUMN = "mqtt_credential_revoked_at"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    # 可重入：测试库先 create_all 过时列已存在
    if _has_column():
        print(f"[0005] 列 {TABLE}.{COLUMN} 已存在，跳过")
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)
