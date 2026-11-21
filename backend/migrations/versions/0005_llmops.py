"""llmops: llm_calls metering, prompt_versions

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
import app.database.base  # noqa: F401

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('llm_calls',
        sa.Column('org_id', sa.String(length=32), nullable=True), sa.Column('user_id', sa.String(length=32), nullable=True),
        sa.Column('task', sa.String(length=40), nullable=False), sa.Column('model', sa.String(length=120), nullable=False),
        sa.Column('provider_model', sa.String(length=160), nullable=False), sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('fallback_from', sa.String(length=120), nullable=True), sa.Column('prompt_version', sa.String(length=20), nullable=False),
        sa.Column('prompt_sha', sa.String(length=16), nullable=False), sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False), sa.Column('cost_usd', sa.Float(), nullable=False),
        sa.Column('latency_ms', sa.Float(), nullable=False), sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False), sa.PrimaryKeyConstraint('id'))
    for c in ('org_id', 'task', 'model', 'created_at'):
        op.create_index(op.f(f'ix_llm_calls_{c}'), 'llm_calls', [c])
    op.create_table('prompt_versions',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('task', sa.String(length=40), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('text', sa.Text(), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=False), sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=32), nullable=False), sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False), sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('org_id', 'task', 'version'))
    op.create_index(op.f('ix_prompt_versions_org_id'), 'prompt_versions', ['org_id'])
    op.create_index(op.f('ix_prompt_versions_task'), 'prompt_versions', ['task'])


def downgrade() -> None:
    op.drop_table('prompt_versions')
    op.drop_table('llm_calls')
