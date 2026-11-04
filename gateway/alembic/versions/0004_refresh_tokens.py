"""刷新令牌表（P2-4 / v1.24.0）。

`refresh_token` 保存服务端可吊销的刷新令牌指纹：

- token_hash：明文令牌的 SHA-256（主键，明文不落库）；
- username：所属账号；
- issued_at / expires_at：签发与过期时间（UTC）；
- revoked_at / revoked_reason：吊销时间与原因（logout / rotated / password_change /
  admin_revoke / reuse_detected / expired），NULL 表示仍有效；
- replaced_by：轮换后的新令牌指纹，便于审计追链。

对既有库：只新增表，不改动任何既有表与数据，现有用户/会话记录不受影响
（存量部署升级后旧 access 仍按原逻辑可用，重新登录即可获得 refresh）。

Revision ID: 0004_refresh_tokens
Revises: 0003_alert_ack_metadata
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_refresh_tokens"
down_revision: Union[str, None] = "0003_alert_ack_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "refresh_token"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # 可重入：测试库可能先经 Base.metadata.create_all 建过表
    if TABLE in _table_names():
        print(f"[0004] 表 {TABLE} 已存在，跳过")
        return

    op.create_table(
        TABLE,
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(32), nullable=True),
        sa.Column("replaced_by", sa.String(64), nullable=True),
    )
    op.create_index("idx_refresh_token_username", TABLE, ["username"])
    op.create_index("idx_refresh_token_expires", TABLE, ["expires_at"])


def downgrade() -> None:
    if TABLE in _table_names():
        op.drop_table(TABLE)
