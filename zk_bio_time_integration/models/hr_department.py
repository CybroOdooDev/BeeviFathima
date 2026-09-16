# -*- coding: utf-8 -*-
import requests
from odoo import api, fields, models


class HrDepartment(models.Model):
    _inherit = 'hr.department'

    external_id = fields.Integer(
        string='External ID',
        index=True,
    )

    @api.model
    def sync_zk_departments(self):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        response = requests.get("http://biotimedxb.com:8007/personnel/api/departments/", headers=headers)
        data = response.json()
        for rec in data['data']:
            dept = self.search([('external_id', '=', rec['id'])], limit=1)
            if not dept:
                self.create({
                    'name': rec['dept_name'],
                    'external_id': rec['id'],
                })
            else:
                self.write({
                    'name': rec['dept_name'],
                    'external_id': rec['id'],
                })
            print('ZK DATA::',rec)

