"""sync tables added to the models since 001

`001` 的 create_all 只在库处于 001 时执行过一次；已迁移的库不会再跑它，所以
此后新增的表不会自动出现（`collector_errors` 就是这么漏掉的，worker 一跑就撞
`relation does not exist`）。

新增表时加一条这样的迁移即可 —— create_all(checkfirst=True) 是幂等的，只会补建
缺失的表。**列变更**仍然要用 `002` 那种显式写法，create_all 不会给已存在的表加列。
"""

from alembic import op
from app.db.models import Base

revision = "003_sync_missing_tables"
down_revision = "002_trading_account_risk_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 只补建，不删表：无法判断哪些表是本迁移建的，删错会丢数据。
    pass
