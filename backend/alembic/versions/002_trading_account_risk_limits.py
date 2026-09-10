"""add trading_accounts.risk_limits

001 只负责按当前模型补建缺失的表，不会给已存在的表加列，所以列变更要在这里
显式承载。写法对 PostgreSQL 与 SQLite 都成立，且可重复执行。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "002_trading_account_risk_limits"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None

TABLE = "trading_accounts"
COLUMN = "risk_limits"


def _has_column() -> bool:
    bind = op.get_bind()
    return COLUMN in {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    # 空库由 001 直接建出带该列的表，这里要能跳过。
    if not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)
