/* Staff console → Leads: everyone who used the website's Contact / Book a demo
 * form. The request is already in the database — this is where it gets worked:
 * a status, and a note for whoever picks it up next. */

import { api, auth } from '../api.js';
import { banner, empty, esc, fmtAgo, guard, loading, pill } from '../ui.js';

const STATUSES = [
  ['new', 'New'], ['contacted', 'Contacted'], ['demo_booked', 'Demo booked'], ['won', 'Won'], ['closed', 'Closed'],
];
const TONE = { new: 'pending', contacted: 'trialing', demo_booked: 'trialing', won: 'success', closed: 'skipped' };

function setup(r) {
  const parts = [
    r.odoo_version && `Odoo ${r.odoo_version}`, r.odoo_hosting, r.biometric_system, r.device_setup,
    r.employees && `${r.employees} employees`,
  ].filter(Boolean);
  return parts.length ? parts.map(esc).join(' · ') : '—';
}

function card(r) {
  const when = r.preferred_date
    ? `${esc(r.preferred_date)}${r.preferred_window ? ` · ${esc(r.preferred_window.toLowerCase())}` : ''}`
      + `${r.timezone ? ` <span class="hint">(${esc(r.timezone)})</span>` : ''}`
    : '';
  return `
    <div class="card lead" data-id="${esc(r.id)}" style="margin-bottom:12px">
      <div class="row" style="justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
        <div>
          <strong>${esc(r.name)}</strong> <span class="hint">· ${esc(r.company)}</span>
          <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}
            · <span title="${esc(r.created_at)}">${esc(fmtAgo(r.created_at))}</span></div>
        </div>
        <div>${pill(TONE[r.status] || 'mute', STATUSES.find(([v]) => v === r.status)?.[1] || r.status)}
          <span class="pill mute">${esc(r.topic)}</span></div>
      </div>
      ${when ? `<div style="margin-top:8px"><span class="hint">Wants a demo:</span> <strong>${when}</strong></div>` : ''}
      <div style="margin-top:8px"><span class="hint">Setup:</span> ${setup(r)}</div>
      ${r.message ? `<p style="white-space:pre-wrap;margin:8px 0 0">${esc(r.message)}</p>` : ''}
      <div class="row" style="margin-top:12px;gap:8px;align-items:flex-start;flex-wrap:wrap">
        <select data-status aria-label="Status">${STATUSES.map(([v, l]) =>
          `<option value="${v}" ${v === r.status ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
        <textarea data-notes rows="2" placeholder="Notes — who called, what was agreed…"
          style="flex:1;min-width:220px">${esc(r.notes || '')}</textarea>
        <button class="sm" data-save>Save</button>
      </div>
      ${r.handled_by ? `<div class="hint" style="margin-top:6px">Last updated by ${esc(r.handled_by)}</div>` : ''}
    </div>`;
}

export async function render(mount, filter = 'open') {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not available', 'This section is for platform staff.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  const rows = await api.get(`/admin/contact-requests${filter === 'open' ? '?status=open' : ''}`);
  mount.innerHTML = `
    <div class="row" style="margin-bottom:14px;gap:8px">
      <button class="sm ${filter === 'open' ? 'primary' : ''}" data-filter="open">Open</button>
      <button class="sm ${filter === 'all' ? 'primary' : ''}" data-filter="all">All</button>
    </div>
    ${rows.length ? rows.map(card).join('')
      : `<div class="card">${empty('No leads here', filter === 'open'
        ? 'Everything has been dealt with. New website requests appear here.'
        : 'Requests from the website’s Contact / Book a demo form appear here.')}</div>`}`;

  mount.querySelectorAll('[data-filter]').forEach((b) =>
    b.addEventListener('click', () => render(mount, b.dataset.filter)));
  mount.querySelectorAll('.lead').forEach((el) => {
    el.querySelector('[data-save]').addEventListener('click', () => guard(async () => {
      await api.patch(`/admin/contact-requests/${el.dataset.id}`, {
        status: el.querySelector('[data-status]').value,
        notes: el.querySelector('[data-notes]').value,
      });
      await render(mount, filter);
    }, 'Saved'));
  });
}
