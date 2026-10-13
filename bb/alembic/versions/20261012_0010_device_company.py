"""terminal belongs to an Odoo company

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('device') as batch:
        batch.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('company_name', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('device') as batch:
        batch.drop_column('company_name')
        batch.drop_column('company_id')
