# -*- coding: utf-8 -*-
from odoo import api, fields, models
import requests

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    external_id = fields.Integer(
        string='External ID',
        index=True,
    )
    is_zk_data = fields.Boolean(
        string='Is ZK Data',
    )

    @api.model
    def sync_zk_employees(self):
        print('SYNC ZK EMPLOYEES..')
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        response = requests.get("http://biotimedxb.com:8007//personnel/api/employees/", headers=headers)
        data = response.json()
        # "http://biotimedxb.com:8007/iclock/api/transactions/"
        response2 = requests.get("http://biotimedxb.com:8007/iclock/api/transactions/?emp=2222", headers=headers)
        data2 = response2.json()
        # print(data2)
        employees = self.search([('is_zk_data', '!=', False)]).mapped('external_id')
        print(employees)
        for rec in data2['data']:
            print(rec)
            # if rec['emp'] in employees:
            #     print(rec)
        # for rec in data['data']:
        #     print('EMPLOYEE: ',rec)
        #     department_id = self.env['hr.department'].search([('external_id','=',rec['department']['id'])])
        #     employee = self.search([('external_id','=',rec['id'])])
            # print('DEPT',department_id, department_id.name)
            # if not employee:
            #     self.create({
            #         'name': rec['full_name'],
            #         'department_id': department_id.id,
            #         'is_zk_data': True,
            #         'external_id': rec['id'],
            #     })
            # print(employee)