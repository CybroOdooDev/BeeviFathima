/* Terminals: every device the account's biometric connections bring in, shown
 * per connection, read-only, failed connections included. Connections with
 * several terminals fold. Managing a connection (sync, edit, test) lives in
 * Settings. */

import { api } from '../api.js';
import { empty, esc, fmtAgo, loading, pill } from '../ui.js';

function deviceState(d, source) {
  if (d.over_plan_limit) {
    return `<span data-tip="Beyond your plan's device limit: its punches are kept but not sent to Odoo until you upgrade.">${pill('pending', 'over plan limit')}</span>`;
  }
  if (d.missing_since) return pill('missing', 'missing');
  // Disabled, or behind a connection that is failing: not collecting punches.
  return d.is_enabled && (!source || source.status === 'connected') ? pill('active', 'active') : pill('skipped', 'inactive');
}

/* Which connection cards are folded shut is kept per browser, not per
 * account: try/catch because storage can be unavailable. */
let FOLDED = new Set();   // "connection:<source id>"
try { FOLDED = new Set(JSON.parse(localStorage.getItem('bb.terminals.folded') || '[]')); } catch { /* none */ }

const companyOf = (device, source) => device?.company_name || source.default_company_name || null;
const companyPill = (name) => (name ? ` <span class="pill mute">${esc(name)}</span>` : '');
// Header line: the address as saved. The IP column drops the internal scheme
// tag (zk://, adms://), which only tells the backend the protocol.
const addressOf = (source) => (source.provider === 'zk_adms'
  ? `serial ${String(source.base_url || '').replace(/^adms:\/\//i, '')}`
  : source.base_url);
const ipOf = (source) => String(source.base_url || '').replace(/^(zk|adms):\/\//i, '');

function terminalRow(source, device) {
  if (!device) {
    // A connection with no terminal yet (a device not reached, say), so a
    // failed connection still shows.
    return `<tr>
      <td>${esc(source.name)}${companyPill(source.default_company_name)}</td><td class="mono">—</td>
      <td class="mono">${esc(ipOf(source) || '—')}</td><td class="num">—</td><td>—</td>
      <td>${pill('skipped', 'inactive')}</td></tr>`;
  }
  return `<tr>
    <td>${esc(device.alias || device.serial_number)}${companyPill(companyOf(device, source))}</td>
    <td class="mono">${esc(device.serial_number)}</td>
    <td class="mono">${esc(device.ip_address || '—')}</td>
    <td class="num">${esc(device.punch_count)}</td>
    <td>${esc(device.last_seen_at ? fmtAgo(device.last_seen_at) : '—')}</td>
    <td>${deviceState(device, source)}</td></tr>`;
}

/** One card per connection; folds when it has more than one terminal. */
function connectionCard(source, devices) {
  const foldId = `connection:${source.id}`;
  const collapsible = devices.length > 1;
  const folded = collapsible && FOLDED.has(foldId);
  const rows = devices.length ? devices.map((d) => terminalRow(source, d)).join('') : terminalRow(source, null);
  return `
    <div class="card" style="margin-bottom:14px">
      <div class="item-head"${collapsible ? ` data-fold="${esc(foldId)}" role="button" tabindex="0"
          aria-expanded="${folded ? 'false' : 'true'}" style="cursor:pointer"` : ''}>
        <div style="min-width:0">
          <div class="row" style="gap:8px;align-items:center">
            ${collapsible ? `<span class="chev" aria-hidden="true" style="display:inline-block;width:14px">${folded ? '▸' : '▾'}</span>` : ''}
            <strong>${esc(source.name)}</strong>
            ${pill(source.status)}
            ${collapsible ? `<span class="hint">${devices.length} terminals</span>` : ''}
          </div>
          <div class="hint mono" style="margin-top:2px${collapsible ? ';margin-left:22px' : ''}">${esc(addressOf(source))}</div>
        </div>
      </div>
      <div class="scroll terminal-list" style="margin-top:12px"${folded ? ' hidden' : ''}>
        <table>
          <thead><tr><th>Device</th><th>Serial</th><th>IP</th><th class="num">Punches</th><th>Last Seen</th><th>State</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

export async function renderTerminals(mount) {
  mount.innerHTML = loading();
  const [sources, devices] = await Promise.all([
    api.get('/sources'),
    api.get('/devices').catch(() => []),
  ]);
  const devicesBySource = {};
  devices.forEach((d) => { (devicesBySource[d.source_id] ||= []).push(d); });

  if (!sources.length) {
    mount.innerHTML = `
      <div class="card">
        ${empty('No Biometric Connections Yet', 'Add a BioTime server or a standalone device, then its terminals show up here.')}
        <div class="row" style="justify-content:center"><a class="btn primary-link" href="#/settings/biometric?add=1">Add Connection</a></div>
      </div>`;
    return;
  }

  const ordered = [...sources].sort((x, y) => x.name.localeCompare(y.name));

  mount.innerHTML = `
    <div class="page-bar">
      <span class="hint" id="tCount"></span>
      <div class="row" style="gap:8px;align-items:center">
        <a class="btn sm" href="#/settings/biometric">Manage Connections</a>
      </div>
    </div>
    <div id="tList"></div>`;

  const paint = () => {
    const terminals = devices.filter((d) => devicesBySource[d.source_id]).length;
    const conns = ordered.length;
    mount.querySelector('#tCount').textContent =
      `${terminals} terminal${terminals === 1 ? '' : 's'} across ${conns} connection${conns === 1 ? '' : 's'}`;
    const list = mount.querySelector('#tList');
    list.innerHTML = ordered.map((source) => connectionCard(source, devicesBySource[source.id] || [])).join('');

    list.querySelectorAll('[data-fold]').forEach((head) => {
      const toggle = () => {
        const id = head.dataset.fold;
        const folding = head.getAttribute('aria-expanded') === 'true';
        head.setAttribute('aria-expanded', folding ? 'false' : 'true');
        const chev = head.querySelector('.chev');
        if (chev) chev.textContent = folding ? '▸' : '▾';
        const body = head.closest('.card')?.querySelector('.terminal-list');
        if (body) body.hidden = folding;
        if (folding) FOLDED.add(id); else FOLDED.delete(id);
        try { localStorage.setItem('bb.terminals.folded', JSON.stringify([...FOLDED])); } catch { /* fine */ }
      };
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });
    });
  };

  paint();
}
