# -*- coding: utf-8 -*-
import pytz

from odoo import api, fields, models
import requests
from datetime import date, timedelta, datetime


class LogTransaction(models.Model):
    _name = 'log.transaction'
    _description = 'Attendance Logged Records'

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
    )

    department_id = fields.Many2one(
        'hr.department',
        related='employee_id.department_id',
    )

    log_type = fields.Selection(
        selection=[
            ('0', 'Check In'),
            ('1', 'Check Out'),
        ]
    )
    log_time = fields.Datetime(
        string='Log Time',
    )
    id_type = fields.Selection(
        selection=[
            ('0', 'Any'),
            ('1', "Fingerprint"),
            ('2', "Face ID"),
            # ('4', "RFID")
        ],
        string="Identification Type",
    )
    device_id = fields.Many2one(
        'log.terminal',
        string='Device',
    )
    external_id = fields.Integer(
        string='External ID',
    )

    is_attendance_logged = fields.Boolean(
        string='Is Attendance Logged',
        default=False,
    )

    fetch_success_date = fields.Datetime(
        string='Fetch Success Date',
    )

    def fetch_employee_attendance_device_logs(self, emp):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        start_date = self.fetch_success_date
        end_date = fields.Datetime.now()
        print('start_date', start_date)
        print('end_date', end_date)
        print('GET RESPONSE')
        url = f"http://biotimedxb.com:8007/iclock/api/transactions/?emp={emp.external_id}&start_time={start_date}&end_time={end_date}"
        # "http://biotimedxb.com:8007/iclock/api/transactions/?emp={emp.external_id}&start_time={start_date}&end_time={end_date}"
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            print('RESPONSE', data)
            self.fetch_success_date = end_date

        except requests.RequestException:
            return
        records = data.get('data')
        if not records:
            print('NO RECORDS')
            return
        for rec in records:
            print('RECORDS', rec)
            employee_id = emp.id
            if rec.get('punch_state') not in ['0', '1']:
                continue
            if rec.get('log_type') not in [0, 1, 2]:
                continue
            log_time = datetime.strptime(rec.get('punch_time'), "%Y-%m-%d %H:%M:%S")

            user_tz = pytz.timezone('UTC')

            local_dt = user_tz.localize(log_time)
            utc_dt = local_dt.astimezone(pytz.UTC).replace(tzinfo=None)

            device_id = self.env['log.terminal'].search([('name', '=', rec.get('terminal_sn'))], limit=1)
            transaction = self.search([('external_id', '=', rec.get('id'))])
            if not transaction:
                self.create({
                    'employee_id': emp.id,
                    'log_type': rec.get('punch_state'),
                    'id_type': str(rec.get('log_type')),
                    'log_time': utc_dt,
                    'device_id': device_id.id,
                    'external_id': rec.get('id'),
                })

    def process_log_check_in(self, transaction):
        no_check_out = self.env['hr.attendance'].search([
            ('employee_id', '=', transaction.employee_id.id),
            ('check_out', '=', False),
        ], order='check_in desc', limit=1)
        print('open log ', no_check_out)
        if no_check_out:
            no_check_out.check_out = transaction.log_time
            no_check_out.out_mode = 'manual'
        self.env['hr.attendance'].create({
            'employee_id': transaction.employee_id.id,
            'check_in': transaction.log_time,
            'in_mode': 'technical',
        })
        transaction.is_attendance_logged = True

    def process_log_check_out(self, transaction):
        no_check_out = self.env['hr.attendance'].search([
            ('employee_id', '=', transaction.employee_id.id),
            ('check_out', '=', False),
            ('check_in', '<', transaction.log_time)
        ], order='check_in', limit=1)

        if no_check_out:
            no_check_out.check_out = transaction.log_time
            no_check_out.out_mode = 'technical'
            transaction.is_attendance_logged = True

    def process_attendance_logs(self):
        transactions = self.search([
            ('is_attendance_logged', '=', False),
        ],order='log_time')
        for transaction in transactions:
            if transaction.log_type == '0':
                self.process_log_check_in(transaction)
            elif transaction.log_type == '1':
                self.process_log_check_out(transaction)

    @api.model
    def get_attendance_data(self):
        print('NOW  ', date.today() - timedelta(days=1))
        #get last sync date start date = last sync date
        today = date.today()
        tomorrow = today + timedelta(days=1)
        employees = self.env['hr.employee'].search([('is_zk_data', '!=', False)])
        for employee in employees:
            print('GET ATTENDANCE ', employee.name)
            self.fetch_employee_attendance_device_logs(employee)
        self.process_attendance_logs()
