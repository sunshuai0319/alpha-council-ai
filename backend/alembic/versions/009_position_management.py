"""add positions columns for software position management

交易所的触发单改不了也撤不掉，所以「保本 / 移动止损」只能是本地记账 + 按需下
reduceOnly 市价单 —— 那就得有地方记当前有效止损、开仓时刻、持仓期内的最有利价。

给已有表加列必须写显式增量迁移（001 的 create_all 不会动已存在的表）。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "009_position_management"
down_revision = "008_signal_tracking"
branch_labels = None
depends_on = None

TABLE = "positions"
COLUMNS = (
    ("effective_stop", sa.Numeric(30, 12)),
    ("opened_at", sa.DateTime(timezone=True)),
    ("peak_price", sa.Numeric(30, 12)),
)


def _present() -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    # 空库由 001 直接建出带这些列的表，这里要能跳过。
    present = _present()
    for name, type_ in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    present = _present()
    for name, _ in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
