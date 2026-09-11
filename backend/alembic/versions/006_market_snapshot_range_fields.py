"""add market_snapshots 24h range and basis columns

001 只按当前模型补建缺失的表，不动已存在的表，所以给 market_snapshots 加列
必须在这里显式承载。写法对 PostgreSQL 与 SQLite 都成立，且可重复执行。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "006_market_snapshot_range_fields"
down_revision = "005_user_locale"
branch_labels = None
depends_on = None

TABLE = "market_snapshots"
COLUMNS = (
    ("open_24h", sa.Numeric(30, 12)),
    ("high_24h", sa.Numeric(30, 12)),
    ("low_24h", sa.Numeric(30, 12)),
    ("price_change_pct", sa.Float()),
    ("quote_volume_24h", sa.Float()),
    ("mark_price", sa.Numeric(30, 12)),
    ("index_price", sa.Numeric(30, 12)),
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
