# Part of BioBridge Attendance Devices. See LICENSE file for full copyright
# and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    #: BioBridge's own external reference for the punch(es) behind this
    #: record. Its presence on this model is also how BioBridge's own probe
    #: detects that *some* companion module is installed — see
    #: OdooClient.ping() in the middleware.
    biotime_ref = fields.Char(
        string="BioBridge Reference",
        copy=False,
        help="BioBridge's own reference for the punch(es) behind this "
        "record. Set automatically — not meant to be edited by hand.",
    )
    device_id = fields.Many2one(
        "biobridge.device",
        string="Device",
        copy=False,
        index=True,
        domain="[('company_id', '=', employee_company_id)]",
        help="Which biometric terminal recorded the check-in for this "
        "record, if BioBridge could tell. Its location, if you have set "
        "one, is exactly where this punch actually happened.",
    )
    device_location = fields.Char(
        related="device_id.location",
        string="Punch Location",
        store=True,
        help="The device's own location field, copied here so Attendance "
        "reports can group or filter by it without following the link.",
    )
    pairing_method_id = fields.Many2one(
        "biobridge.pairing.method",
        string="Pairing Method",
        copy=False,
        index=True,
        help="Which pairing method BioBridge used to build this record from "
        "the raw punches. If the account's method was changed part-way "
        "through, this tells the records before and after apart.",
    )

    #: Drives the Device field's domain: only terminals of the employee's own
    #: company can be picked.
    employee_company_id = fields.Many2one(
        "res.company", related="employee_id.company_id", string="Employee Company"
    )

    @api.constrains("device_id", "employee_id")
    def _check_device_company(self):
        """The terminal and the employee must belong to the same company."""
        for rec in self:
            device, emp = rec.device_id, rec.employee_id
            if device and emp.company_id and device.company_id != emp.company_id:
                raise ValidationError(
                    _(
                        "%(employee)s belongs to %(emp_company)s, but device %(device)s "
                        "belongs to %(dev_company)s.",
                        employee=emp.name,
                        emp_company=emp.company_id.display_name,
                        device=device.display_name,
                        dev_company=device.company_id.display_name,
                    )
                )
