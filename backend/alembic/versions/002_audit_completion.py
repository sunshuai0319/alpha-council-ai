"""persist identity, controls, macro observations, and document retries"""

import sqlalchemy as sa

from alembic import op

revision = "002_audit_completion"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clerk_webhook_events",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "wallet_challenges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False, unique=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_challenges_user_id", "wallet_challenges", ["user_id"])
    op.create_table(
        "control_states",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "macro_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("series_id", sa.String(length=64), nullable=False),
        sa.Column("observation_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("series_id", "observation_date", name="uq_macro_observation"),
    )
    op.create_index("ix_macro_observations_series_id", "macro_observations", ["series_id"])
    op.create_index("ix_macro_observations_observation_date", "macro_observations", ["observation_date"])
    op.add_column(
        "source_documents",
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("source_documents", "processing_attempts")
    op.drop_table("macro_observations")
    op.drop_table("control_states")
    op.drop_index("ix_wallet_challenges_user_id", table_name="wallet_challenges")
    op.drop_table("wallet_challenges")
    op.drop_table("clerk_webhook_events")
