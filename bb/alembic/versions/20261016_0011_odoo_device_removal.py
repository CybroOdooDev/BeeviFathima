"""queue of Odoo device records to delete after a connection is removed

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The app may have created it already (create_all on startup); then only
    # the revision needs recording.
    if sa.inspect(op.get_bind()).has_table('odoo_device_removal'):
        return
    op.create_table('odoo_device_removal',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('serial_number', sa.String(length=64), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('company_id', sa.Integer(), nullable=True),
    sa.Column('source_name', sa.String(length=120), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('odoo_device_removal', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_odoo_device_removal_tenant_id'), ['tenant_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('odoo_device_removal', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_odoo_device_removal_tenant_id'))
    op.drop_table('odoo_device_removal')
