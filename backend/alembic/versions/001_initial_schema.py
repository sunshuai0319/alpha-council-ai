"""create initial alpha council schema"""

from alembic import op
from app.db.models import Base

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建出所有缺失的表（按当前模型定义）。

    `checkfirst=True` 是必需的：`False` 会对已存在的表发 CREATE TABLE 并抛
    DuplicateTable，导致已经有数据的库根本无法执行迁移。它只补建缺失的表，
    **不会**给已存在的表加新列 —— 表结构变更由后续迁移显式承载。
    """

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
