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

    @api.model
    def get_attendance_data(self):
        header = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        print('NOW  ', date.today() - timedelta(days=1))
        today = date.today()
        employees = self.env['hr.employee'].search([('is_zk_data', '!=', False)])
        for employee in employees:
            response = requests.get(
                f"http://biotimedxb.com:8007/iclock/api/transactions/?emp={employee.external_id}&start_time={today - timedelta(days=1)}&end_time={today}",
                headers=header)
            records = response.json()
            logs = []
            print('RECORDS:', records)
            if 'data' in records:
                for rec in records['data']:
                    print(rec)
                    employee_id = employee.id
                    if rec['punch_state'] in ['0', '1']:
                        log_type = rec['punch_state']
                    log_time = datetime.strptime(rec['punch_time'], "%Y-%m-%d %H:%M:%S")

                    user_tz = pytz.timezone(self.env.user.tz or 'UTC')

                    local_dt = user_tz.localize(log_time)
                    utc_dt = local_dt.astimezone(pytz.UTC).replace(tzinfo=None)

                    if rec['verify_type'] in [0, 1, 2]:
                        id_type = str(rec['verify_type'])
                    device_id = self.env['log.terminal'].search([('name', '=', rec['terminal_sn'])])
                    # print(f"Employee ID: {employee_id}\nLog type: {log_type}\nLog time: {log_time}\nDevice ID: {device_id}\nID type: {id_type}")
                    transaction = self.search([('external_id', '=', rec['id'])])
                    print('LOG:::', transaction, rec['id'])
                    if not transaction:
                        logs += [self.create({
                            'employee_id': employee_id,
                            'log_type': log_type,
                            'log_time': utc_dt,
                            'device_id': device_id.id,
                            'id_type': id_type,
                            'external_id': rec['id'],
                        })]

                for log in logs:
                    if log.log_type == '0':
                        no_check_out = self.env['hr.attendance'].search([
                            ('employee_id', '=', log.employee_id.id),
                            ('check_out', '=', False),
                        ], order='check_in desc', limit=1)
                        if no_check_out:
                            check_out_log = self.search([
                                ('employee_id', '=', log.employee_id.id),
                                ('log_type', '=', '1'),
                                ('log_time', '>', log.log_time),
                                ('is_attendance_logged', '=', False),
                            ], order='log_time', limit=1)
                            no_check_out.check_out = log.log_time if not check_out_log else check_out_log.log_time
                            check_out_log.is_attendance_logged = True
                            no_check_out.out_mode = 'technical' if check_out_log else 'manual'
                        self.env['hr.attendance'].create({
                            'employee_id': log.employee_id.id,
                            'check_in': log.log_time,
                            'in_mode': 'technical'
                        })
                        log.is_attendance_logged = True
            else:
                print('NO EXTERNAL API RECORDS')
                logs = self.search([
                    ('employee_id', '=', employee.id),
                    ('is_attendance_logged', '=', False),
                ])
                print(logs)
                for log in logs:
                    if log.log_type == '0':
                        no_check_out = self.env['hr.attendance'].search([
                            ('employee_id', '=', log.employee_id.id),
                            ('check_out', '=', False),
                        ], order='check_in desc', limit=1)
                        if no_check_out:
                            check_out_log = self.search([
                                ('employee_id', '=', log.employee_id.id),
                                ('log_type', '=', '1'),
                                ('log_time', '>', log.log_time),
                                ('is_attendance_logged', '=', False),
                            ], order='log_time', limit=1)
                            no_check_out.check_out = log.log_time if not check_out_log else check_out_log.log_time
                            check_out_log.is_attendance_logged = True
                            no_check_out.out_mode = 'technical' if check_out_log else 'manual'
                        self.env['hr.attendance'].create({
                            'employee_id': log.employee_id.id,
                            'check_in': log.log_time,
                            'in_mode': 'technical'
                        })
                        log.is_attendance_logged = True
                    elif log.log_type == '1':
                        no_check_out = self.env['hr.attendance'].search([
                            ('employee_id', '=', log.employee_id.id),
                            ('check_out', '=', False),
                            ('check_in','<', log.log_time),
                        ], order='check_in', limit=1)
                        print(no_check_out)
                        if no_check_out:
                            no_check_out.check_out = log.log_time
                            log.is_attendance_logged = True
            # print('recs created..',log)

        # for rec in self.search([]):
        #     print(rec.log_time)
        #     print(rec.employee_id)
        #     if rec.log_type == '0':
        #         no_check_out_attendances = self.env['hr.attendance'].search([
        #             ('employee_id', '=', rec.employee_id.id),
        #             ('check_out', '=', False),
        #         ], order='check_in desc', limit=1)
        #         if no_check_out_attendances:
        #             print(123123123777, no_check_out_attendances)
        #             no_check_out_attendances.check_out = rec.log_time
        #
        #         test = self.env['hr.attendance'].create({
        #             'employee_id': rec.employee_id.id,
        #             'check_in': rec.log_time,
        #             'in_mode': 'technical',
        #
        #         })
        #         print(22222, test)
