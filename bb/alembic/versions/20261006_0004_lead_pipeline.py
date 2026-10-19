"""lead pipeline: stages, history

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('contact_request', sa.Column('stage_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contact_request', sa.Column('lost_reason', sa.String(length=160), nullable=True))
    # The old five-value status becomes the pipeline: a booked demo is the
    # "demo" stage, and a closed lead is a lost one.
    op.execute("UPDATE contact_request SET status = 'demo' WHERE status = 'demo_booked'")
    op.execute("UPDATE contact_request SET status = 'lost' WHERE status = 'closed'")
    op.execute("UPDATE contact_request SET stage_changed_at = updated_at WHERE stage_changed_at IS NULL")

    op.create_table(
        'contact_event',
        sa.Column('contact_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('from_stage', sa.String(length=20), nullable=True),
        sa.Column('to_stage', sa.String(length=20), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('actor', sa.String(length=254), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['contact_id'], ['contact_request.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_contact_event_lead_created', 'contact_event', ['contact_id', 'created_at'])
    # Every existing lead gets an opening entry so its timeline is never empty.
    op.execute(
        "INSERT INTO contact_event (id, contact_id, kind, from_stage, to_stage, actor, created_at, updated_at) "
        "SELECT substr(replace(id, '-', '') || 'e', 1, 32), id, 'stage', NULL, 'new', 'website', created_at, created_at "
        "FROM contact_request"
    )


def downgrade() -> None:
    op.drop_index('ix_contact_event_lead_created', table_name='contact_event')
    op.drop_table('contact_event')
    op.execute("UPDATE contact_request SET status = 'closed' WHERE status = 'lost'")
    op.execute("UPDATE contact_request SET status = 'demo_booked' WHERE status IN ('demo','qualified')")
    op.drop_column('contact_request', 'lost_reason')
    op.drop_column('contact_request', 'stage_changed_at')
