"""add users.locale

界面语言偏好：worker 按用户语言让 LLM 生成中文/英文的分析文本。列变更要显式
迁移（create_all 不会给已存在的表加列），已有行用 server_default 兜底。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "005_user_locale"
down_revision = "004_pending_immediate"
branch_labels = None
depends_on = None

TABLE = "users"
COLUMN = "locale"


def _has_column() -> bool:
    bind = op.get_bind()
    return COLUMN in {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.String(length=16), nullable=False, server_default="zh-CN"),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)
