"""设备影子版本历史（v1.37.0）。

Revision ID: 0012_shadow_history
Revises: 0011_device_shadow
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012_shadow_history"
down_revision: Union[str, None] = "0011_device_shadow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "device_shadow_revision" in _tables():
        return

    json_variant = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "device_shadow_revision",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("desired", json_variant, nullable=False, server_default="{}"),
        sa.Column("changed_keys", json_variant, nullable=False, server_default="[]"),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("device_id", "version", name="uq_shadow_revision_device_version"),
    )

    # 只能为升级时的当前状态建立基线；此前没有快照，禁止臆造更早版本。
    shadow = sa.table(
        "device_shadow",
        sa.column("device_id", sa.String(64)),
        sa.column("version", sa.Integer()),
        sa.column("desired", json_variant),
        sa.column("desired_updated_at", sa.DateTime(timezone=True)),
        sa.column("desired_updated_by", sa.String(64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    revision_table = sa.table(
        "device_shadow_revision",
        sa.column("device_id", sa.String(64)),
        sa.column("version", sa.Integer()),
        sa.column("desired", json_variant),
        sa.column("changed_keys", json_variant),
        sa.column("action", sa.String(16)),
        sa.column("source_version", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("created_by", sa.String(64)),
    )
    op.execute(
        sa.insert(revision_table).from_select(
            [
                "device_id",
                "version",
                "desired",
                "changed_keys",
                "action",
                "source_version",
                "created_at",
                "created_by",
            ],
            sa.select(
                shadow.c.device_id,
                shadow.c.version,
                shadow.c.desired,
                sa.literal_column("'[]'"),
                sa.literal_column("'baseline'"),
                sa.literal(None),
                sa.func.coalesce(shadow.c.desired_updated_at, shadow.c.created_at, sa.func.now()),
                shadow.c.desired_updated_by,
            ),
        )
    )


def downgrade() -> None:
    if "device_shadow_revision" in _tables():
        op.drop_table("device_shadow_revision")
