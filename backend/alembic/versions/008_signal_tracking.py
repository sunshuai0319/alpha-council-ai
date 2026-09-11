"""add trading_decisions signal_score and veto_type

给已有表加列必须写显式增量迁移（001 的 create_all 不会动已存在的表）。
signal_score / veto_type 用于前向验证（spec 4.2）：统计否决率、看哪些信号
分项真与未来收益相关。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "008_signal_tracking"
down_revision = "007_sync_missing_tables"
branch_labels = None
depends_on = None

TABLE = "trading_decisions"
COLUMNS = (
    ("signal_score", sa.Float(), None),
    ("veto_type", sa.String(32), None),
)


def _present() -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    # 空库由 001 直接建出带这些列的表，这里要能跳过。
    present = _present()
    for name, type_, default in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True, server_default=default))


def downgrade() -> None:
    present = _present()
    for name, _, _ in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
