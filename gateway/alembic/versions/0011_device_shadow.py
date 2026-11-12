"""设备影子表（v1.36.0）。

新建 `device_shadow` 表：一行一设备，记录期望状态（desired）、上报状态（reported）、
版本号与双侧时间戳。同步状态由服务层根据 desired/reported 与 device.status 派生，
不落库，避免双写不一致。

Revision ID: 0011_device_shadow
Revises: 0010_device_tags
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_device_shadow"
down_revision: Union[str, None] = "0010_device_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "device_shadow" in _tables():
        return

    json_variant = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "device_shadow",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("desired", json_variant, nullable=False, server_default="{}"),
        sa.Column("reported", json_variant, nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("desired_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("desired_updated_by", sa.String(64), nullable=True),
        sa.Column("reported_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    if "device_shadow" in _tables():
        op.drop_table("device_shadow")
