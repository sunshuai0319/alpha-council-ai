"""sync tables added to the models since 003 (market_microstructures)

`001` 的 create_all 只在库处于 001 时执行过一次；已迁移的库不会再跑它，所以此后
新增的表不会自动出现。**只改 app/db/models.py 是不够的** —— 实测：加完模型跑
`alembic upgrade head` 到 006，`market_microstructures` 依然不存在，只有新库因为
001 用的是当前模型才碰巧建出来。

create_all(checkfirst=True) 是幂等的，只会补建缺失的表。**列变更**仍然要用 `002`
那种显式写法，create_all 不会给已存在的表加列。
"""

from alembic import op
from app.db.models import Base

revision = "007_sync_missing_tables"
down_revision = "006_market_snapshot_range_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 只补建，不删表：无法判断哪些表是本迁移建的，删错会丢数据。
    pass
