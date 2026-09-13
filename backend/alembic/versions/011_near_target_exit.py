"""add near-target timestamp for bounded profitable holds

Once a position reaches the near-target threshold, the timestamp is latched so
the position manager can release it if the full target does not arrive within
the configured timeout.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "011_near_target_exit"
down_revision = "010_widen_impact_horizon"
branch_labels = None
depends_on = None

TABLE = "positions"
COLUMN = "near_target_at"


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    columns = {column["name"] for column in inspect(bind).get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    columns = {column["name"] for column in inspect(bind).get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
