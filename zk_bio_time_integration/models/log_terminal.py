# -*- coding: utf-8 -*-
from odoo import api, fields, models
import requests

class LogTerminal(models.Model):
    _name = 'log.terminal'
    _description = 'Attendance Logging Terminals'

    name = fields.Char(
        string="Serial Number",
        copy=False,
    )

    alias = fields.Char(
        string="Alias",
    )

    ip_address = fields.Char(
        string="IP Address",
    )

    external_id = fields.Integer(
        string="External ID",
    )

    @api.model
    def sync_zk_log_terminals(self):
        header = {
            "Content-Type": "application/json",
            "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"
        }
        response = requests.get("http://biotimedxb.com:8007/iclock/api/terminals/", headers=header)
        data = response.json()
        # print('TERMINALS::',data)
        for rec in data['data']:
            print(rec)
            terminal = self.search([
                '|',
                ('external_id', '=', rec['id']),
                ('ip_address','=', rec['ip_address']),
            ])
            if not terminal:
                self.create({
                    'external_id': rec['id'],
                    'name': rec['sn'],
                    'ip_address': rec['ip_address'],
                    'alias': rec['alias'],
                })