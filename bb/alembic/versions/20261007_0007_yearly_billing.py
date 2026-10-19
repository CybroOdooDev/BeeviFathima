"""yearly billing: plan yearly price + Stripe price, account billing interval

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07 16:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscription_plan') as batch:
        batch.add_column(sa.Column('yearly_price_cents', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('stripe_yearly_price_id', sa.String(length=80), nullable=True))
        batch.create_index('ix_subscription_plan_stripe_yearly_price_id', ['stripe_yearly_price_id'])
    with op.batch_alter_table('tenant') as batch:
        batch.add_column(sa.Column('billing_interval', sa.String(length=5), nullable=False, server_default='month'))
    with op.batch_alter_table('pending_signup') as batch:
        batch.add_column(sa.Column('billing_interval', sa.String(length=5), nullable=False, server_default='month'))


def downgrade() -> None:
    with op.batch_alter_table('pending_signup') as batch:
        batch.drop_column('billing_interval')
    with op.batch_alter_table('tenant') as batch:
        batch.drop_column('billing_interval')
    with op.batch_alter_table('subscription_plan') as batch:
        batch.drop_index('ix_subscription_plan_stripe_yearly_price_id')
        batch.drop_column('stripe_yearly_price_id')
        batch.drop_column('yearly_price_cents')
