"""security: org_policies, audit_logs.sig

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
import app.database.base  # noqa: F401

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('org_policies',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('policy', sa.JSON(), nullable=False),
        sa.Column('updated_by', sa.String(length=32), nullable=False), sa.Column('updated_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('org_id'))
    op.add_column('audit_logs', sa.Column('sig', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('audit_logs', 'sig')
    op.drop_table('org_policies')
