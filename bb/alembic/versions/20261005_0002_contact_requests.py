"""contact requests

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 10:50:54.984528
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('contact_request',
    sa.Column('topic', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('company', sa.String(length=160), nullable=False),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('employees', sa.String(length=40), nullable=True),
    sa.Column('odoo_version', sa.String(length=40), nullable=True),
    sa.Column('odoo_hosting', sa.String(length=40), nullable=True),
    sa.Column('biometric_system', sa.String(length=120), nullable=True),
    sa.Column('device_setup', sa.String(length=60), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('preferred_date', sa.String(length=10), nullable=True),
    sa.Column('preferred_window', sa.String(length=20), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='new', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('handled_by', sa.String(length=254), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('contact_request', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contact_request_email'), ['email'], unique=False)
        batch_op.create_index('ix_contact_status_created', ['status', 'created_at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('contact_request', schema=None) as batch_op:
        batch_op.drop_index('ix_contact_status_created')
        batch_op.drop_index(batch_op.f('ix_contact_request_email'))

    op.drop_table('contact_request')
