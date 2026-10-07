"""demo becomes a sub-stage of contacted

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('contact_request', sa.Column('demo_status', sa.String(length=12), nullable=True))
    # Leads that sat in the old "demo" stage are contacted leads with a demo
    # booked. Their history keeps the original "-> Demo" entry as it was.
    op.execute("UPDATE contact_request SET demo_status = 'scheduled', status = 'contacted' WHERE status = 'demo'")


def downgrade() -> None:
    op.execute("UPDATE contact_request SET status = 'demo' WHERE status = 'contacted' AND demo_status IS NOT NULL")
    op.drop_column('contact_request', 'demo_status')
