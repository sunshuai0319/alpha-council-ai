"""add control_states.pending_immediate

「恢复周期」触发立即 tick 的标记：scheduler 发现 RUNNING + pending_immediate
就马上跑一轮，不必等满 decision_interval_seconds。列变更要显式迁移
（create_all 不会给已存在的表加列），对已有行用 server_default 兜底。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "004_pending_immediate"
down_revision = "003_sync_missing_tables"
branch_labels = None
depends_on = None

TABLE = "control_states"
COLUMN = "pending_immediate"


def _has_column() -> bool:
    bind = op.get_bind()
    return COLUMN in {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)
