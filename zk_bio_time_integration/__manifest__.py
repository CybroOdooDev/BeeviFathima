# -*- coding: utf-8 -*-
{
    'name': "ZKTeco Biotime Integration",
    'version': "19.0.1.0.0",
    'license': "LGPL-3",
    'installable': True,
    'depends': [
        "base",
        "hr",
        "mail"
    ],
    'data': [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/log_terminal_views.xml",
        "views/log_transaction_views.xml",
    ]
}