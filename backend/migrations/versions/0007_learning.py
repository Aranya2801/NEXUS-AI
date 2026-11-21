"""continuous learning: feedback reasons/consent, eval_cases, eval_runs, experiments, agent_runs.variants

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
import app.database.base  # noqa: F401

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('feedback', sa.Column('reasons', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('feedback', sa.Column('allow_review', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('agent_runs', sa.Column('variants', sa.JSON(), nullable=False, server_default='{}'))
    op.create_table('eval_cases',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('query', sa.Text(), nullable=False),
        sa.Column('expected_document_ids', sa.JSON(), nullable=False), sa.Column('source_run_id', sa.String(length=32), nullable=True),
        sa.Column('note', sa.String(length=300), nullable=False), sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=32), nullable=False), sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False), sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_eval_cases_org_id'), 'eval_cases', ['org_id'])
    op.create_table('eval_runs',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False), sa.Column('metrics', sa.JSON(), nullable=False), sa.Column('per_case', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.String(length=32), nullable=False), sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False), sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_eval_runs_org_id'), 'eval_runs', ['org_id'])
    op.create_table('experiments',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('task', sa.String(length=40), nullable=False), sa.Column('variants', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False), sa.Column('winner', sa.String(length=100), nullable=True),
        sa.Column('created_by', sa.String(length=32), nullable=False), sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('ended_at', app.database.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False), sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('org_id', 'name'))
    op.create_index(op.f('ix_experiments_org_id'), 'experiments', ['org_id'])
    op.create_index(op.f('ix_experiments_task'), 'experiments', ['task'])
    op.create_index(op.f('ix_experiments_status'), 'experiments', ['status'])


def downgrade() -> None:
    op.drop_table('experiments')
    op.drop_table('eval_runs')
    op.drop_table('eval_cases')
    op.drop_column('agent_runs', 'variants')
    op.drop_column('feedback', 'allow_review')
    op.drop_column('feedback', 'reasons')
