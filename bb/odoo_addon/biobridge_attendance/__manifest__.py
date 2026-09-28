{
    "name": "BioBridge Attendance Devices",
    "summary": "Track which biometric terminal recorded each attendance punch.",
    "description": """
BioBridge Attendance Devices
=============================

BioBridge (the biometric-device middleware from Cybrosys) works against a
stock Odoo out of the box, over the standard external API — no module
required. Installing this one is optional, and only worth it if you want to
see *which physical terminal* recorded each attendance record.

What it adds
------------
* A **Biometric Device** model (Employees > Devices) listing every terminal
  BioBridge has connected — one record per terminal, per company, keyed by
  the terminal's own serial number so it never duplicates.
* A **Device** field on Attendance, showing exactly which terminal a
  check-in or check-out came from, alongside the location/area you gave that
  terminal (front gate, warehouse entrance, and so on).

BioBridge keeps this list in sync automatically once installed — it
registers itself against the terminal's serial number the first time a punch
from that device is pushed. There is nothing to configure here by hand,
though you are free to rename a device or set its location for a clearer
Attendance report.
""",
    # Deliberately no Odoo-series prefix (no "17.0.x") — nothing here uses a
    # version-specific API, so it installs unchanged on any Odoo release
    # BioBridge itself supports (14 through 19). Bump the last segment only.
    "version": "1.0.0",
    # View syntax targets Odoo 17+ specifically (the `invisible="<expr>"`
    # attribute form, not the pre-17 `attrs="{...}"` dialect) — the XML-RPC
    # side of BioBridge itself is version-agnostic back to 14, but this
    # module's views were written once and not back-ported/verified against
    # 14-16's older view syntax. Adjust the view XML first if targeting one
    # of those.
    "category": "Human Resources/Attendances",
    "author": "Cybrosys Techno Solutions",
    "website": "https://www.cybrosys.com",
    "license": "LGPL-3",
    "depends": ["hr_attendance"],
    "data": [
        "security/ir.model.access.csv",
        "security/biobridge_device_security.xml",
        "views/biobridge_device_views.xml",
        "views/hr_attendance_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
