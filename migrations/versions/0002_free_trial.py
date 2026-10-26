"""Three lifetime free tasks; existing internal trial runs remain exempt."""
from alembic import op
import sqlalchemy as sa

revision = "0002_free_trial"
down_revision = "0001_web"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("web_runs", sa.Column("free_quota", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("web_runs", "free_quota")
