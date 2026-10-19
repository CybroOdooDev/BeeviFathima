"""per-company switches on the Odoo connection

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06 09:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default: null already means "every company is on".
    op.add_column('odoo_connection', sa.Column('disabled_company_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('odoo_connection', 'disabled_company_ids')
