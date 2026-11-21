"""autonomous agent runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
import app.database.base  # noqa: F401

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('autonomous_runs',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('goal', sa.Text(), nullable=False), sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False), sa.Column('result', sa.JSON(), nullable=True), sa.Column('error', sa.Text(), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_autonomous_runs_org_id'), 'autonomous_runs', ['org_id'])
    op.create_index(op.f('ix_autonomous_runs_user_id'), 'autonomous_runs', ['user_id'])
    op.create_index(op.f('ix_autonomous_runs_status'), 'autonomous_runs', ['status'])


def downgrade() -> None:
    op.drop_table('autonomous_runs')
