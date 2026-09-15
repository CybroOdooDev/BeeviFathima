# -*- coding: utf-8 -*-
import pytz

from odoo import api, fields, models
import requests
from datetime import date, timedelta, datetime
import logging

_logger = logging.getLogger(__name__)


class LogTransaction(models.Model):
    _name = 'log.transaction'
    _description = 'Attendance Logged Records'
    _sql_constraints = [
        ('external_id_unique',
        'unique(external_id)',
        'External ID must be unique',),
    ]

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


    def fetch_employee_attendance_device_logs(self, emp):
        now = fields.Datetime.now()
        if emp.zk_last_sync:
            start_date = emp.zk_last_sync
            _logger.info("PREVIOUS SYNC FOUND FOR %s: %s", emp.name, emp.zk_last_sync)
        else:
            start_date = now - timedelta(days=1)
            _logger.info("FIRST SYNC FOR EMPLOYEE %s on %s", emp.name, start_date)
        end_date = now

        headers = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        url = f"http://biotimedxb.com:8007/iclock/api/transactions/?emp_code={emp.emp_code}&start_time=2026-09-09&end_time=2026-09-14"
        # "http://biotimedxb.com:8007/iclock/api/transactions/?emp={emp.external_id}&start_time={start_date}&end_time={end_date}"
        while url:
            _logger.info("URL: %s", url)
            try:
                _logger.info("GET RESPONSE")
                response = requests.get(url, headers=headers)
                response.raise_for_status()
                _logger.info("RESPONSE STATUS: %s", response.status_code)
                data = response.json()
                emp.write({
                    'zk_last_sync': fields.Datetime.now()
                })
                # print('RESPONSE', data)
                # _logger.info("Latest Successful fetch on %s", fields.Datetime.now())

            except requests.RequestException as e:
                _logger.exception("REQUEST FAILED: %s", e)
                return False
            records = data.get('data')
            if not records:
                _logger.info("NO RECORDS")
                return False
            try:
                for rec in records:
                    # print('RECORDS', rec)
                    _logger.info("ATTENDANCE LOG: %s", rec)
                    if rec.get('punch_state') not in ['0', '1']:
                        continue
                    if rec.get('verify_type') not in [0, 1, 2]:
                        continue
                    log_time = datetime.strptime(rec.get('punch_time'), "%Y-%m-%d %H:%M:%S")

                    user_tz = pytz.timezone('UTC')

                    local_dt = user_tz.localize(log_time)
                    utc_dt = local_dt.astimezone(pytz.UTC).replace(tzinfo=None)

                    device_id = self.env['log.terminal'].search([('name', '=', rec.get('terminal_sn'))], limit=1)
                    transaction = self.search([('external_id', '=', rec.get('id'))])
                    if not transaction:
                        t = self.create({
                            'employee_id': emp.id,
                            'log_type': rec.get('punch_state'),
                            'id_type': str(rec.get('verify_type')),
                            'log_time': utc_dt,
                            'device_id': device_id.id,
                            'external_id': rec.get('id'),
                        })
                        _logger.info("ATTENDANCE LOG CREATED: %s", t)
                if data.get('next'):
                    print('pagination', data.get("next"))
                    url = data.get('next')
                    continue
                else:
                    break
            except Exception as error:
                _logger.exception("ERROR while procession BioTime attendance log for employee %s: %s", emp.name, error)
                return False

    def process_log_check_in(self, transaction):
        _logger.info("PROCESS CHECK INS..")
        no_check_out = self.env['hr.attendance'].search([
            ('employee_id', '=', transaction.employee_id.id),
            ('check_out', '=', False),
        ], order='check_in desc', limit=1)
        print(transaction.log_time, transaction.log_type,no_check_out.check_in)
        if no_check_out:
            _logger.info('OPEN LOG FOUND: %s', no_check_out)
            no_check_out.check_out = transaction.log_time if transaction.log_time > no_check_out.check_in else False
            no_check_out.out_mode = 'manual'
        self.env['hr.attendance'].create({
            'employee_id': transaction.employee_id.id,
            'check_in': transaction.log_time,
            'in_mode': 'technical',
        })
        transaction.is_attendance_logged = True

    def process_log_check_out(self, transaction):
        _logger.info("PROCESS CHECK OUTS..")
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
        _logger.info("PROCESS ATTENDANCE LOGS...")
        transactions = self.search([
            ('is_attendance_logged', '=', False),
        ], order='log_time')
        for transaction in transactions:
            if transaction.log_type == '0':
                self.process_log_check_in(transaction)
            elif transaction.log_type == '1':
                self.process_log_check_out(transaction)

    @api.model
    def get_attendance_data(self):
        # print('NOW  ', fields.Datetime.now())
        _logger.info("CURRENT DATE TIME: %s", fields.Datetime.now())
        # get last sync date start date = last sync date
        employees = self.env['hr.employee'].search([('is_zk_data', '!=', False)])
        _logger.info("ZK EMPLOYEE IDS: %s", employees)
        for employee in employees:
            print('GET ATTENDANCE ', employee.name)
            _logger.info("GET Attendance logs for %s", employee.name)
            self.fetch_employee_attendance_device_logs(employee)
        self.process_attendance_logs()
