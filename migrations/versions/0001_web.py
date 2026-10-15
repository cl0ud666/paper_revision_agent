"""Initial invite-only web application schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_web"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("web_users", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False, unique=True), sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False), sa.Column("is_admin", sa.Boolean, nullable=False),
        sa.Column("daily_run_limit", sa.Integer, nullable=False), sa.Column("run_token_limit", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("web_sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("web_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_web_sessions_user_id", "web_sessions", ["user_id"])
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
    op.create_table("web_runs", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("web_users.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer, nullable=False), sa.Column("document_name", sa.String(255), nullable=False),
        sa.Column("source_key", sa.Text, nullable=False), sa.Column("result_key", sa.Text), sa.Column("report_key", sa.Text),
        sa.Column("request", sa.Text, nullable=False), sa.Column("rules", sa.JSON, nullable=False),
        sa.Column("token_limit", sa.Integer, nullable=False), sa.Column("used_tokens", sa.Integer, nullable=False),
        sa.Column("reserved_tokens", sa.Integer, nullable=False), sa.Column("processing_seconds", sa.Integer, nullable=False), sa.Column("pending_id", sa.String(36)),
        sa.Column("pending_request", sa.JSON, nullable=False), sa.Column("state_summary", sa.JSON, nullable=False),
        sa.Column("error", sa.Text, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    for column in ("user_id", "status", "created_at"):
        op.create_index(f"ix_web_runs_{column}", "web_runs", [column])
    op.create_table("web_jobs", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("web_runs.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False), sa.Column("action", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False), sa.Column("publish_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("execution_token", sa.String(36)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "version", name="uq_web_job_run_version"))
    for column in ("run_id", "status", "publish_after"):
        op.create_index(f"ix_web_jobs_{column}", "web_jobs", [column])
    op.create_table("web_run_events", sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("web_runs.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False), sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_web_run_events_run_id", "web_run_events", ["run_id"])


def downgrade():
    for name in ("web_run_events", "web_jobs", "web_runs", "web_sessions", "web_users"):
        op.drop_table(name)
