# Part of BioBridge Attendance Devices. See LICENSE file for full copyright
# and licensing details.
from odoo import fields, models


class BiobridgePairingMethod(models.Model):
    """How BioBridge turned raw punches into an attendance record.

    One row per method (State Based, Alternating, First In / Last Out).
    BioBridge stamps each attendance record with the method that wrote it, so
    when an account changes its pairing method part-way through, the records
    from before and after the change can be told apart.
    """

    _name = "biobridge.pairing.method"
    _description = "BioBridge Pairing Method"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        index=True,
        help="BioBridge's own identifier for the method — never edit it by hand.",
    )

    _sql_constraints = [
        ("code_unique", "unique(code)", "A pairing method with this code already exists."),
    ]
