"""demo requests already past New default to a pending demo

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 14:00:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New code gives a demo lead a pending demo when it is first contacted;
    # this does the same for leads that were already contacted or qualified.
    op.execute(
        "UPDATE contact_request SET demo_status = 'pending' "
        "WHERE demo_status IS NULL AND status IN ('contacted', 'qualified') "
        "AND (topic = 'Demo' OR preferred_date IS NOT NULL)"
    )


def downgrade() -> None:
    pass
