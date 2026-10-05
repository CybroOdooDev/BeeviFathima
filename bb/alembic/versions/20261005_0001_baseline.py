"""baseline

Revision ID: 0001
Revises: 
Create Date: 2026-10-05 10:19:01.042908
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Frozen snapshot of the schema when Alembic was adopted.
    op.create_table('account_closure',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('tenant_name', sa.String(length=200), nullable=False),
    sa.Column('tenant_slug', sa.String(length=120), nullable=True),
    sa.Column('owner_email', sa.String(length=254), nullable=True),
    sa.Column('plan_name', sa.String(length=80), nullable=True),
    sa.Column('status_before', sa.String(length=20), nullable=True),
    sa.Column('closed_by', sa.String(length=20), nullable=False),
    sa.Column('closed_by_email', sa.String(length=254), nullable=True),
    sa.Column('reason_code', sa.String(length=40), nullable=True),
    sa.Column('reason_text', sa.Text(), nullable=True),
    sa.Column('stripe_subscription_cancelled', sa.Boolean(), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_account_closure_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('adms_command',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('serial_number', sa.String(length=64), nullable=False),
    sa.Column('command', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('return_code', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('adms_command', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_adms_command_serial_number'), ['serial_number'], unique=False)

    op.create_table('pending_signup',
    sa.Column('company_name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('plan_id', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pending_signup', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pending_signup_email'), ['email'], unique=False)

    op.create_table('platform_setting',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=254), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('scheduler_lease',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('owner', sa.String(length=120), nullable=True),
    sa.Column('mode', sa.String(length=20), nullable=True),
    sa.Column('last_tick_at', sa.DateTime(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('stripe_event',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('type', sa.String(length=80), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('subscription_plan',
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('monthly_price_cents', sa.Integer(), nullable=True),
    sa.Column('max_employees', sa.Integer(), nullable=True),
    sa.Column('min_sync_interval_minutes', sa.Integer(), nullable=True),
    sa.Column('max_devices', sa.Integer(), nullable=True),
    sa.Column('stripe_price_id', sa.String(length=80), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    with op.batch_alter_table('subscription_plan', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscription_plan_stripe_price_id'), ['stripe_price_id'], unique=False)

    op.create_table('tenant',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspension_reason', sa.String(length=200), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('sync_interval_minutes', sa.Integer(), nullable=False),
    sa.Column('sync_enabled', sa.Boolean(), nullable=False),
    sa.Column('alert_emails_enabled', sa.Boolean(), server_default=sa.text('(true)'), nullable=False),
    sa.Column('alert_state', sa.JSON(), nullable=True),
    sa.Column('pairing_mode', sa.String(length=20), nullable=False),
    sa.Column('day_boundary_hour', sa.Integer(), nullable=False),
    sa.Column('min_punch_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('max_shift_hours', sa.Integer(), nullable=False),
    sa.Column('orphan_out_policy', sa.String(length=20), nullable=False),
    sa.Column('auto_create_employees', sa.Boolean(), nullable=False),
    sa.Column('work_start_time', sa.String(length=5), nullable=False),
    sa.Column('late_grace_minutes', sa.Integer(), nullable=False),
    sa.Column('biometric_mode', sa.String(length=20), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.String(length=32), nullable=True),
    sa.Column('pending_plan_id', sa.String(length=32), nullable=True),
    sa.Column('subscription_renews_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stripe_customer_id', sa.String(length=64), nullable=True),
    sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True),
    sa.Column('limit_max_employees', sa.Integer(), nullable=True),
    sa.Column('limit_max_devices', sa.Integer(), nullable=True),
    sa.Column('limit_min_sync_interval_minutes', sa.Integer(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['pending_plan_id'], ['subscription_plan.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['plan_id'], ['subscription_plan.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_pending_plan_id'), ['pending_plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_slug'), ['slug'], unique=True)
        batch_op.create_index(batch_op.f('ix_tenant_stripe_customer_id'), ['stripe_customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_stripe_subscription_id'), ['stripe_subscription_id'], unique=False)

    op.create_table('app_user',
    sa.Column('tenant_id', sa.String(length=32), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=True),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_platform_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('email_verify_token_hash', sa.String(length=64), nullable=True),
    sa.Column('email_verify_token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('password_reset_token_hash', sa.String(length=64), nullable=True),
    sa.Column('password_reset_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('password_reset_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_login_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('credentials_pending', sa.Boolean(), server_default=sa.text('(false)'), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), server_default=sa.text('(false)'), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email', name='uq_user_email')
    )
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_app_user_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_app_user_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('attendance_record',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('emp_code', sa.String(length=64), nullable=False),
    sa.Column('employee_name', sa.String(length=200), nullable=True),
    sa.Column('department', sa.String(length=200), nullable=True),
    sa.Column('odoo_employee_id', sa.Integer(), nullable=True),
    sa.Column('odoo_attendance_id', sa.Integer(), nullable=True),
    sa.Column('check_in', sa.DateTime(), nullable=False),
    sa.Column('check_out', sa.DateTime(), nullable=True),
    sa.Column('worked_hours', sa.Float(), nullable=True),
    sa.Column('shift_date', sa.String(length=10), nullable=True),
    sa.Column('check_in_local', sa.DateTime(), nullable=True),
    sa.Column('check_out_local', sa.DateTime(), nullable=True),
    sa.Column('device_serial', sa.String(length=64), nullable=True),
    sa.Column('pairing_mode', sa.String(length=20), nullable=True),
    sa.Column('is_auto_closed', sa.Boolean(), nullable=False),
    sa.Column('is_orphan_out', sa.Boolean(), nullable=False),
    sa.Column('is_late', sa.Boolean(), nullable=False),
    sa.Column('late_minutes', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'odoo_attendance_id', name='uq_attendance_odoo_id')
    )
    with op.batch_alter_table('attendance_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_attendance_record_shift_date'), ['shift_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_attendance_record_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index('ix_attendance_tenant_checkin', ['tenant_id', 'check_in'], unique=False)

    op.create_table('audit_log',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('actor_user_id', sa.String(length=32), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=120), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('device_source',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('connection_kind', sa.String(length=20), server_default=sa.text("'platform'"), nullable=False),
    sa.Column('base_url', sa.String(length=500), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=False),
    sa.Column('password_enc', sa.Text(), nullable=True),
    sa.Column('auth_type', sa.String(length=10), nullable=False),
    sa.Column('token_enc', sa.Text(), nullable=True),
    sa.Column('verify_ssl', sa.Boolean(), nullable=False),
    sa.Column('server_timezone', sa.String(length=64), nullable=False),
    sa.Column('cursor_punch_time', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_message', sa.Text(), nullable=True),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('auto_provision_employees', sa.Boolean(), server_default=sa.text('(false)'), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_source_name')
    )
    with op.batch_alter_table('device_source', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_source_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('employee_mapping',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('emp_code', sa.String(length=64), nullable=False),
    sa.Column('source_name', sa.String(length=200), nullable=True),
    sa.Column('department', sa.String(length=200), nullable=True),
    sa.Column('odoo_employee_id', sa.Integer(), nullable=True),
    sa.Column('odoo_employee_name', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('match_method', sa.String(length=32), nullable=True),
    sa.Column('match_note', sa.Text(), nullable=True),
    sa.Column('open_attendance_id', sa.Integer(), nullable=True),
    sa.Column('open_check_in', sa.DateTime(), nullable=True),
    sa.Column('last_punch_at', sa.DateTime(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'emp_code', name='uq_mapping_emp_code')
    )
    with op.batch_alter_table('employee_mapping', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_employee_mapping_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index('ix_mapping_tenant_status', ['tenant_id', 'status'], unique=False)

    op.create_table('odoo_connection',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('db_name', sa.String(length=120), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=False),
    sa.Column('api_key_enc', sa.Text(), nullable=True),
    sa.Column('odoo_version', sa.String(length=32), nullable=True),
    sa.Column('uid_cache', sa.Integer(), nullable=True),
    sa.Column('has_companion_addon', sa.Boolean(), nullable=False),
    sa.Column('has_device_tracking', sa.Boolean(), server_default=sa.text('(false)'), nullable=False),
    sa.Column('device_tracking_mode', sa.String(length=20), nullable=True),
    sa.Column('company_id', sa.Integer(), nullable=True),
    sa.Column('company_name', sa.String(length=120), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_message', sa.Text(), nullable=True),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_odoo_conn_name')
    )
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_odoo_connection_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('punch_record',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('source_id', sa.String(length=32), nullable=False),
    sa.Column('device_id', sa.String(length=32), nullable=True),
    sa.Column('first_seen_run_id', sa.String(length=32), nullable=True),
    sa.Column('external_id', sa.String(length=190), nullable=False),
    sa.Column('emp_code', sa.String(length=64), nullable=False),
    sa.Column('punch_time_utc', sa.DateTime(), nullable=False),
    sa.Column('punch_time_local', sa.DateTime(), nullable=True),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('raw_state', sa.String(length=8), nullable=True),
    sa.Column('verify_type', sa.String(length=8), nullable=True),
    sa.Column('terminal_sn', sa.String(length=64), nullable=True),
    sa.Column('process_state', sa.String(length=16), nullable=False),
    sa.Column('odoo_attendance_id', sa.Integer(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('raw', sa.JSON(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'source_id', 'external_id', name='uq_punch_external')
    )
    with op.batch_alter_table('punch_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_punch_record_first_seen_run_id'), ['first_seen_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_punch_record_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_punch_record_terminal_sn'), ['terminal_sn'], unique=False)
        batch_op.create_index('ix_punch_tenant_emp_time', ['tenant_id', 'emp_code', 'punch_time_utc'], unique=False)
        batch_op.create_index('ix_punch_tenant_state', ['tenant_id', 'process_state'], unique=False)

    op.create_table('sync_run',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('triggered_by', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('cursor_from', sa.DateTime(), nullable=True),
    sa.Column('cursor_to', sa.DateTime(), nullable=True),
    sa.Column('punches_fetched', sa.Integer(), nullable=False),
    sa.Column('punches_new', sa.Integer(), nullable=False),
    sa.Column('punches_skipped', sa.Integer(), nullable=False),
    sa.Column('attendances_created', sa.Integer(), nullable=False),
    sa.Column('attendances_closed', sa.Integer(), nullable=False),
    sa.Column('employees_matched', sa.Integer(), nullable=False),
    sa.Column('employees_provisioned', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error_count', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('log', sa.JSON(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sync_run', schema=None) as batch_op:
        batch_op.create_index('ix_run_tenant_started', ['tenant_id', 'started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_sync_run_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('adms_device',
    sa.Column('serial_number', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=True),
    sa.Column('source_id', sa.String(length=32), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ip', sa.String(length=64), nullable=True),
    sa.Column('push_version', sa.String(length=32), nullable=True),
    sa.Column('firmware', sa.String(length=80), nullable=True),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('user_count', sa.Integer(), nullable=True),
    sa.Column('attlog_count', sa.Integer(), nullable=True),
    sa.Column('attlog_stamp', sa.String(length=32), nullable=True),
    sa.Column('operlog_stamp', sa.String(length=32), nullable=True),
    sa.Column('users', sa.JSON(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['device_source.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('adms_device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_adms_device_serial_number'), ['serial_number'], unique=True)
        batch_op.create_index(batch_op.f('ix_adms_device_source_id'), ['source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_adms_device_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('device',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('source_id', sa.String(length=32), nullable=False),
    sa.Column('serial_number', sa.String(length=64), nullable=False),
    sa.Column('alias', sa.String(length=120), nullable=True),
    sa.Column('area', sa.String(length=120), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('pairing_override', sa.String(length=20), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('punch_count', sa.Integer(), nullable=False),
    sa.Column('missing_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['device_source.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'source_id', 'serial_number', name='uq_device_serial')
    )
    with op.batch_alter_table('device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_source_id'), ['source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('user_session',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=True),
    sa.Column('scope', sa.String(length=16), server_default=sa.text("'tenant'"), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('device_label', sa.String(length=120), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=80), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.create_index('ix_session_user_revoked', ['user_id', 'revoked_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_session_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_session_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    # Frozen snapshot of the schema when Alembic was adopted.
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_session_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_session_tenant_id'))
        batch_op.drop_index('ix_session_user_revoked')

    op.drop_table('user_session')
    with op.batch_alter_table('device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_device_source_id'))

    op.drop_table('device')
    with op.batch_alter_table('adms_device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_adms_device_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_adms_device_source_id'))
        batch_op.drop_index(batch_op.f('ix_adms_device_serial_number'))

    op.drop_table('adms_device')
    with op.batch_alter_table('sync_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sync_run_tenant_id'))
        batch_op.drop_index('ix_run_tenant_started')

    op.drop_table('sync_run')
    with op.batch_alter_table('punch_record', schema=None) as batch_op:
        batch_op.drop_index('ix_punch_tenant_state')
        batch_op.drop_index('ix_punch_tenant_emp_time')
        batch_op.drop_index(batch_op.f('ix_punch_record_terminal_sn'))
        batch_op.drop_index(batch_op.f('ix_punch_record_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_punch_record_first_seen_run_id'))

    op.drop_table('punch_record')
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_odoo_connection_tenant_id'))

    op.drop_table('odoo_connection')
    with op.batch_alter_table('employee_mapping', schema=None) as batch_op:
        batch_op.drop_index('ix_mapping_tenant_status')
        batch_op.drop_index(batch_op.f('ix_employee_mapping_tenant_id'))

    op.drop_table('employee_mapping')
    with op.batch_alter_table('device_source', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_source_tenant_id'))

    op.drop_table('device_source')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_tenant_id'))

    op.drop_table('audit_log')
    with op.batch_alter_table('attendance_record', schema=None) as batch_op:
        batch_op.drop_index('ix_attendance_tenant_checkin')
        batch_op.drop_index(batch_op.f('ix_attendance_record_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_attendance_record_shift_date'))

    op.drop_table('attendance_record')
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_app_user_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_app_user_email'))

    op.drop_table('app_user')
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_stripe_subscription_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_stripe_customer_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_slug'))
        batch_op.drop_index(batch_op.f('ix_tenant_plan_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_pending_plan_id'))

    op.drop_table('tenant')
    with op.batch_alter_table('subscription_plan', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscription_plan_stripe_price_id'))

    op.drop_table('subscription_plan')
    op.drop_table('stripe_event')
    op.drop_table('scheduler_lease')
    op.drop_table('platform_setting')
    with op.batch_alter_table('pending_signup', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pending_signup_email'))

    op.drop_table('pending_signup')
    with op.batch_alter_table('adms_command', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_adms_command_serial_number'))

    op.drop_table('adms_command')
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_account_closure_tenant_id'))

    op.drop_table('account_closure')
