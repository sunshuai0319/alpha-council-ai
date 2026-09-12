"""widen document summary impact horizon labels

Ark can return a composite impact horizon such as
``short-term bearish; medium-term bullish``.  The original 32-character
column was too narrow for that valid value.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "010_widen_impact_horizon"
down_revision = "009_position_management"
branch_labels = None
depends_on = None

TABLE = "document_summaries"
COLUMN = "impact_horizon"
TARGET_LENGTH = 128


def _current_length() -> int | None:
    bind = op.get_bind()
    for column in inspect(bind).get_columns(TABLE):
        if column["name"] == COLUMN:
            return getattr(column["type"], "length", None)
    return None


def _alter_length(length: int) -> None:
    bind = op.get_bind()
    kwargs = {
        "existing_type": sa.String(length=length),
        "type_": sa.String(length=TARGET_LENGTH),
        "existing_nullable": False,
    }
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.alter_column(COLUMN, **kwargs)
    else:
        op.alter_column(TABLE, COLUMN, **kwargs)


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table(TABLE):
        return
    current_length = _current_length()
    # TEXT or an already wider VARCHAR does not need an alteration.
    if current_length is None or current_length >= TARGET_LENGTH:
        return
    _alter_length(current_length)


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table(TABLE):
        return
    current_length = _current_length()
    if current_length is None or current_length <= 32:
        return
    bind = op.get_bind()
    kwargs = {
        "existing_type": sa.String(length=current_length),
        "type_": sa.String(length=32),
        "existing_nullable": False,
    }
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.alter_column(COLUMN, **kwargs)
    else:
        op.alter_column(TABLE, COLUMN, **kwargs)
