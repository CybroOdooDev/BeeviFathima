/* Staff console → Closed accounts: every deleted account, who closed it and
 * why. The account's data is gone; this is what's left on purpose. */

import { api, auth } from '../api.js';
import { banner, empty, esc, fmtAgo, loading, pill } from '../ui.js';

export async function render(mount) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not Available', 'This section is for platform staff.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  const rows = await api.get('/admin/closures');
  const byCustomer = rows.filter((r) => r.closed_by === 'customer');
  const counts = {};
  byCustomer.forEach((r) => { counts[r.reason_label] = (counts[r.reason_label] || 0) + 1; });
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  mount.innerHTML = `
    ${top.length ? `<div class="card" style="margin-bottom:16px">
      <div class="card-head"><h2>Why Customers Leave <span class="hint">${byCustomer.length} self-service closure${byCustomer.length === 1 ? '' : 's'}</span></h2></div>
      <div class="row" style="gap:8px">${top.map(([label, n]) => `<span class="pill mute">${esc(label)} · ${n}</span>`).join('')}</div>
    </div>` : ''}
    <div class="card">
      ${rows.length ? `<div class="scroll"><table>
        <thead><tr><th>Closed</th><th>Account</th><th>Owner</th><th>Plan</th><th>By</th><th>Reason</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td title="${esc(r.closed_at || '')}">${esc(fmtAgo(r.closed_at))}</td>
          <td><strong>${esc(r.tenant_name)}</strong><div class="hint mono">${esc(r.tenant_slug || '')}</div></td>
          <td>${esc(r.owner_email || '—')}</td>
          <td>${esc(r.plan_name || '—')}${r.stripe_subscription_cancelled ? '<div class="hint">Stripe subscription cancelled</div>' : ''}</td>
          <td>${r.closed_by === 'customer' ? pill('pending', 'customer') : pill('skipped', 'staff')}
            <div class="hint">${esc(r.closed_by_email || '')}</div></td>
          <td><strong>${esc(r.reason_label || '—')}</strong>${r.reason_text ? `<div class="hint" style="white-space:pre-wrap;max-width:420px">${esc(r.reason_text)}</div>` : ''}</td>
        </tr>`).join('')}</tbody></table></div>`
        : empty('No Closed Accounts', 'Accounts deleted by their owner or by staff show up here with the reason.')}
    </div>`;
}
