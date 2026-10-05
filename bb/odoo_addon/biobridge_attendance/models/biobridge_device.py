# Part of BioBridge Attendance Devices. See LICENSE file for full copyright
# and licensing details.
from odoo import api, fields, models


class BiobridgeDevice(models.Model):
    """One physical biometric terminal the company owns.

    BioBridge (the middleware, outside Odoo) upserts these itself, keyed on
    ``serial_number`` — the same identity it already tracks its own devices
    by — the first time a punch from that terminal is pushed. Nothing here
    needs to be created by hand, though renaming one or giving it a location
    is exactly what shows up on the Attendance report afterwards.
    """

    _name = "biobridge.device"
    _description = "Biometric Attendance Device"
    _order = "name"

    name = fields.Char(
        required=True,
        help="A friendly name for this terminal — defaults to its serial "
        "number until you give it one, e.g. the site or door it sits at.",
    )
    serial_number = fields.Char(
        required=True,
        index=True,
        help="The terminal's own hardware serial number. This is the "
        "identity BioBridge matches on — never edit it by hand.",
    )
    location = fields.Char(
        help="Where this terminal is — 'Main Entrance', 'Warehouse Gate 2', "
        "and so on. Shown on every attendance record it produces.",
    )
    terminal_model = fields.Char(string="Model")
    ip_address = fields.Char(string="IP Address")
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    active = fields.Boolean(default=True)
    attendance_count = fields.Integer(compute="_compute_attendance_count")

    _sql_constraints = [
        (
            "serial_number_company_uniq",
            "unique(serial_number, company_id)",
            "A device with this serial number already exists for this company.",
        ),
    ]

    def _compute_attendance_count(self):
        counts = dict(
            self.env["hr.attendance"]
            .with_context(active_test=False)
            ._read_group(
                [("device_id", "in", self.ids)], ["device_id"], ["__count"]
            )
        )
        # The grouping key comes back as the device recordset, not a bare id.
        by_id = {
            (k.id if hasattr(k, "id") else k): v for k, v in counts.items()
        }
        for device in self:
            device.attendance_count = by_id.get(device.id, 0)

    def action_view_attendances(self):
        """The stat button's target. Built here rather than as a static
        action referencing hr_attendance's own action id, which has not
        stayed the same name across the Odoo versions this add-on supports.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.display_name,
            "res_model": "hr.attendance",
            "view_mode": "tree,form",
            "domain": [("device_id", "=", self.id)],
            "context": {"search_default_group_by_employee": 1},
        }

    def name_get(self):
        return [
            (device.id, f"{device.name} ({device.serial_number})")
            for device in self
        ]

    @api.model
    def biobridge_upsert(self, serial_number, vals=None):
        """Find-or-create by serial number, scoped to the current company.

        The one entry point BioBridge calls over Odoo's external API (JSON-2
        on Odoo 19+, XML-RPC before). It has to be public: Odoo refuses any
        method whose name starts with an underscore over RPC.
        """
        return self._biobridge_upsert(serial_number, vals)

    @api.model
    def _biobridge_upsert(self, serial_number, vals=None):
        """Find-or-create by serial number, scoped to the current company.

        The implementation behind ``biobridge_upsert`` — kept as a single
        model method rather than leaving the search/create race to the
        caller, since two near-simultaneous pushes for a brand new terminal
        would otherwise create it twice.
        """
        vals = dict(vals or {})
        vals["serial_number"] = serial_number
        device = self.search(
            [
                ("serial_number", "=", serial_number),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        if device:
            # Never blank out a name or location the customer already set —
            # only fill in fields BioBridge actually has fresh data for.
            device.write({k: v for k, v in vals.items() if v})
            return device.id
        vals.setdefault("name", serial_number)
        return self.create(vals).id
