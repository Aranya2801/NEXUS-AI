"""connectors: sources, item mapping, runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
import app.database.base  # noqa: F401

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('connectors',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('collection_id', sa.String(length=32), nullable=False),
        sa.Column('created_by', sa.String(length=32), nullable=False), sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False), sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('secret_enc', sa.Text(), nullable=False), sa.Column('acl_mode', sa.String(length=10), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False), sa.Column('interval_minutes', sa.Integer(), nullable=True),
        sa.Column('cursor', sa.Text(), nullable=True), sa.Column('last_sync_at', app.database.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id']), sa.ForeignKeyConstraint(['org_id'], ['organizations.id']),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('org_id', 'name'))
    op.create_index(op.f('ix_connectors_collection_id'), 'connectors', ['collection_id'])
    op.create_index(op.f('ix_connectors_org_id'), 'connectors', ['org_id'])
    op.create_table('connector_items',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('connector_id', sa.String(length=32), nullable=False),
        sa.Column('external_id', sa.String(length=300), nullable=False), sa.Column('document_id', sa.String(length=32), nullable=True),
        sa.Column('name', sa.String(length=300), nullable=False), sa.Column('url', sa.String(length=1000), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=False), sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True), sa.Column('external_acl', sa.JSON(), nullable=True),
        sa.Column('synced_at', app.database.base.UTCDateTime(timezone=True), nullable=True), sa.Column('id', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['connector_id'], ['connectors.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connector_id', 'external_id'))
    op.create_index(op.f('ix_connector_items_connector_id'), 'connector_items', ['connector_id'])
    op.create_index(op.f('ix_connector_items_org_id'), 'connector_items', ['org_id'])
    op.create_table('connector_runs',
        sa.Column('org_id', sa.String(length=32), nullable=False), sa.Column('connector_id', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False), sa.Column('started_at', app.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('finished_at', app.database.base.UTCDateTime(timezone=True), nullable=True), sa.Column('stats', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True), sa.Column('id', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['connector_id'], ['connectors.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_connector_runs_connector_id'), 'connector_runs', ['connector_id'])
    op.create_index(op.f('ix_connector_runs_org_id'), 'connector_runs', ['org_id'])


def downgrade() -> None:
    for t in ('connector_runs', 'connector_items', 'connectors'):
        op.drop_table(t)
