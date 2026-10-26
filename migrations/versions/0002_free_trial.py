"""Three lifetime free tasks; existing internal trial runs remain exempt."""

import sqlalchemy as sa
from alembic import op

revision = "0002_free_trial"
down_revision = "0001_web"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "web_runs", sa.Column("free_quota", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    op.drop_column("web_runs", "free_quota")
