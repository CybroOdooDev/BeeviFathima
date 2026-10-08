"""employee mapping remembers the Odoo company

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.add_column(sa.Column('odoo_company_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('odoo_company_name', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.drop_column('odoo_company_name')
        batch.drop_column('odoo_company_id')
