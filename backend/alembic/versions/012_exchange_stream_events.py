"""add exchange stream tables (WS fill / orders / account)

`001` 的 create_all 只在库处于 001 时执行过一次；已迁移的库不会再跑它，所以此后
新增的表不会自动出现。**只改 app/db/models.py 是不够的** —— 必须有一条
create_all(checkfirst=True) 的迁移把新表补出来，照 003 / 007 的写法。

create_all(checkfirst=True) 是幂等的，只会补建缺失的表。**列变更**仍然要用
002 那种显式写法，create_all 不会给已存在的表加列。
"""

from alembic import op

from app.db.models import Base

revision = "012_exchange_stream_events"
down_revision = "011_near_target_exit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 只补建，不删表：无法判断哪些表是本迁移建的，删错会丢数据。
    pass
