"""employee mapping remembers department and manager

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.add_column(sa.Column('odoo_department_name', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('odoo_manager_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('odoo_manager_name', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.drop_column('odoo_manager_name')
        batch.drop_column('odoo_manager_id')
        batch.drop_column('odoo_department_name')
