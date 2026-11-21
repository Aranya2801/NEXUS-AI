"""chunk kind (text | table | figure) for multimodal retrieval

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('chunks', sa.Column('kind', sa.String(length=10), server_default='text', nullable=False))
    op.create_index(op.f('ix_chunks_kind'), 'chunks', ['kind'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chunks_kind'), table_name='chunks')
    op.drop_column('chunks', 'kind')
