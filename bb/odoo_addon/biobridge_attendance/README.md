# BioBridge Attendance Devices

Optional companion module for a customer's Odoo. **BioBridge itself needs
nothing installed on Odoo** — it talks to stock `hr.attendance` over the
standard external API. Install this only for a customer who wants to see
*which terminal* recorded each punch and *where* that terminal is.

## What it adds

- `biobridge.device` — one record per physical terminal, per company, keyed
  on serial number. Menu: **Biometric Devices**.
- `hr.attendance.device_id` — which terminal recorded the check-in.
- `hr.attendance.device_location` — that terminal's location, denormalised
  onto the attendance record so reports can group/filter by it directly.
- `hr.attendance.biotime_ref` — BioBridge's own reference for the punch(es)
  behind the record (this already existed conceptually — BioBridge's own
  connection probe checks for it — this module is what actually defines it).
- A best-effort patch onto Odoo's stock Attendance list/form/search adding
  the two device fields, **plus** an addon-owned "Attendance by Device"
  screen that works regardless of whether that patch's view names matched
  the target install (see the comment in `views/hr_attendance_views.xml`).

## Install

1. Confirm the Odoo version this is going to. The model and fields work on
   any version BioBridge itself supports (14–19). The *view XML* was written
   against 17+'s syntax (`invisible="<expr>"`, not the older `attrs={...}`
   dialect) — on an older target, either back-port that syntax or drop the
   patch in `hr_attendance_views.xml` and keep just the addon-owned screen.
2. Copy this directory into the Odoo instance's addons path.
3. Update the apps list and install **BioBridge Attendance Devices**.
4. Nothing else to configure — BioBridge notices this module on its next
   connection test (it looks for the `device_id` field this module adds to
   `hr.attendance`) and starts registering each device automatically (via
   `biobridge.device`'s `_biobridge_upsert`) the first time it pushes a
   punch from a terminal it hasn't reported for this tenant before. Set a
   device's `location` once it appears, if you want it on reports; a name
   change or a location is never overwritten by BioBridge afterwards.
   Re-run "Test connection" in BioBridge's own settings after installing if
   you don't want to wait for the next scheduled sync.

## Access

Two access levels, deliberately not three: **read** (`base.group_user` —
every internal user, so anyone can see which terminal a punch came from) and
**full** (`hr_attendance.group_hr_attendance_manager` — the Attendance
Administrator). The middle "Officer" tier that `hr_attendance` itself has is
left out on purpose: its own name and exact place in the group hierarchy has
changed across Odoo releases (14 through 19 alone), while `group_hr_attendance_manager`
("Administrator") has stayed both the same name and the same role in every
version checked. Anyone who can manage attendance at all can manage devices.

## Multi-company

`biobridge.device.company_id` defaults to whoever's logged-in company
creates it — in practice, the company of the Odoo user BioBridge
authenticates as for that connection. One BioBridge tenant maps to one Odoo
connection, so in the common case this is automatic and needs no attention;
a customer running several companies through one Odoo database with one
shared BioBridge connection should be aware every device it registers lands
in that connection's company.

`security/biobridge_device_security.xml` adds the record rule that actually
makes that isolation visible in Odoo's own UI: **Biometric Devices** shows
only the devices belonging to whichever company (or companies) are currently
active in the session's company switcher — the same `company_ids` every
other multi-company model in Odoo filters on. Without it, `company_id` being
set on each device would do nothing by itself; ir.model.access.csv's grants
control *whether* a user can read biobridge.device at all, not *which rows*
of it they see, and only an ir.rule does the latter. This is independent of
(and on top of) BioBridge's own `OdooConnection.company_id` scoping — that
controls what BioBridge itself reads and writes over XML-RPC (see
`app/integrations/odoo.py`); this rule controls what a person clicking
around inside Odoo sees.
