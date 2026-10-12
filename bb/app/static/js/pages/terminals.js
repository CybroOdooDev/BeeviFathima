/* Terminals: every device the account's biometric connections bring in.
 *
 * Two tabs, one per kind, because the two kinds are configured differently and so
 * carry their controls in different places:
 * Standalone devices are each their own connection, so each row has its own
 * Sync now. Terminals behind a platform server (BioTime) are all read through
 * that server in one go, so the Sync now sits on the platform, once. */

import { api, auth } from '../api.js';
import { empty, esc, fmtAgo, loading, pill, toast } from '../ui.js';
import { syncSource } from './settings.js';

function deviceState(d) {
  if (d.over_plan_limit) {
    return `<span data-tip="Beyond your plan's device limit: its punches are kept but not sent to Odoo until you upgrade.">${pill('pending', 'over plan limit')}</span>`;
  }
  if (d.missing_since) return pill('missing', 'missing');
  return d.is_enabled ? pill('active', 'active') : pill('skipped', 'inactive');
}

/* Company pickers. Shown only when the Odoo has more than one company on,
 * because with one there is nowhere wrong for a terminal's punches to go. */
let COMPANIES = [];

const companyOptions = (selected, blank) => `
  <option value="">${esc(blank)}</option>
  ${COMPANIES.map((c) => `<option value="${esc(c.id)}"${Number(selected) === c.id ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}`;

function deviceCompanyCell(d, source) {
  const inherited = source.default_company_name;
  const held = d.held_punch_count
    ? ` <span data-tip="${esc(d.held_punch_count)} punch(es) are waiting for review until this terminal has the right company.">${pill('pending', `${d.held_punch_count} held`)}</span>`
    : '';
  if (!auth.canWrite) return `${esc(d.company_name || inherited || '—')}${held}`;
  const blank = inherited ? `Connection default (${inherited})` : 'Not set';
  return `<select class="sm" data-device-company="${esc(d.id)}" aria-label="Company for ${esc(d.alias || d.serial_number)}">
      ${companyOptions(d.company_id, blank)}</select>${held}`;
}

async function saveDeviceCompany(select) {
  const id = select.value ? Number(select.value) : null;
  const name = id ? COMPANIES.find((c) => c.id === id)?.name || null : null;
  await api.patch(`/devices/${select.dataset.deviceCompany}`, { company_id: id, company_name: name });
}

async function saveSourceCompany(select) {
  const id = select.value ? Number(select.value) : null;
  const name = id ? COMPANIES.find((c) => c.id === id)?.name || null : null;
  await api.put(`/sources/${select.dataset.sourceCompany}/default-company`, { company_id: id, company_name: name });
}

const syncButton = (source, label = 'Sync Now') => (auth.canWrite && source.is_active !== false
  ? `<button class="sm primary" data-sync-source="${esc(source.id)}"
             title="Pull ${esc(source.name)}'s punches now and push them to Odoo">${esc(label)}</button>`
  : '');

function standaloneCard(sources, devicesBySource) {
  if (!sources.length) return '';
  return `
    <div class="card" style="margin-bottom:18px">
      <div class="scroll">
        <table class="tight-table">
          <thead><tr><th>Device</th><th>Address</th><th>Serial</th>
            <th class="num">Punches</th><th>Last Seen</th>${COMPANIES.length > 1 ? '<th>Company</th>' : ''}<th>State</th><th>Connection</th><th></th></tr></thead>
          <tbody>
            ${sources.map((s) => {
              const d = (devicesBySource[s.id] || [])[0];
              // One line per cell, laid out like the platform tables below: the
              // device state gets its own column and "checked … ago" moves into
              // the connection pill's hover label, so badges no longer sit
              // stacked under text at a different height from the rest of the row.
              return `
                <tr>
                  <td><strong>${esc(d?.alias || s.name)}</strong></td>
                  <td class="mono">${esc(s.base_url.replace(/^zk:\/\//i, ''))}</td>
                  <td class="mono">${esc(d?.serial_number || '—')}</td>
                  <td class="num">${esc(d?.punch_count ?? '—')}</td>
                  <td>${esc(d?.last_seen_at ? fmtAgo(d.last_seen_at) : '—')}</td>
                  ${COMPANIES.length > 1 ? `<td>${d ? deviceCompanyCell(d, s) : '—'}</td>` : ''}
                  <td>${d ? deviceState(d)
                      : '<span class="hint" data-tip="Recorded the first time Test connection reaches the device (Settings → Biometric connections)">Not Recognised Yet</span>'}</td>
                  <td><span data-tip="Checked ${esc(fmtAgo(s.last_checked_at))}">${pill(s.status)}</span></td>
                  <td class="actions-cell"><div class="row-actions">${syncButton(s)}</div></td>
                </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

function platformCard(source, devices) {
  return `
    <div class="card" style="margin-bottom:18px">
      <div class="card-head" style="position:static">
        <h2>${esc(source.name)}</h2>
        <div class="actions">
          ${COMPANIES.length > 1 && auth.canWrite ? `
            <label class="hint" data-tip="Every terminal of this connection belongs to this company unless it sets its own.">
              Default Company:
              <select class="sm" data-source-company="${esc(source.id)}">${companyOptions(source.default_company_id, 'Not set')}</select>
            </label>` : ''}
          <span data-tip="Checked ${esc(fmtAgo(source.last_checked_at))}">${pill(source.status)}</span>
          ${syncButton(source)}
        </div>
      </div>
      ${devices.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Terminal</th><th>Serial</th><th>IP</th><th>Area</th>
              <th class="num">Punches</th><th>Last Seen</th>${COMPANIES.length > 1 ? '<th>Company</th>' : ''}<th>State</th></tr></thead>
            <tbody>
              ${devices.map((d) => `
                <tr>
                  <td><strong>${esc(d.alias || d.serial_number)}</strong></td>
                  <td class="mono">${esc(d.serial_number)}</td>
                  <td class="mono">${esc(d.ip_address || '—')}</td>
                  <td>${esc(d.area || '—')}</td>
                  <td class="num">${esc(d.punch_count)}</td>
                  <td>${esc(d.last_seen_at ? fmtAgo(d.last_seen_at) : '—')}</td>
                  ${COMPANIES.length > 1 ? `<td>${deviceCompanyCell(d, source)}</td>` : ''}
                  <td>${deviceState(d)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No Terminals Imported Yet',
          'Use “Import terminals” on this connection in Settings → Biometric connections.')}
    </div>`;
}

/* Companies that are switched on for the account's Odoo connection; [] when
 * there is no Odoo yet or it cannot be reached (the pickers then stay hidden). */
async function loadCompanies() {
  try {
    const conns = await api.get('/odoo-connections');
    const conn = conns.find((c) => c.is_active !== false) || conns[0];
    if (!conn) return [];
    const all = await api.get(`/odoo-connections/${conn.id}/companies`);
    const off = new Set(conn.disabled_company_ids || []);
    return all.filter((c) => !off.has(c.id) && (conn.company_id == null || c.id === conn.company_id));
  } catch {
    return [];
  }
}

export async function renderTerminals(mount, route) {
  mount.innerHTML = loading();
  const [sources, devices] = await Promise.all([
    api.get('/sources'),
    api.get('/devices').catch(() => []),
  ]);
  COMPANIES = await loadCompanies();
  const devicesBySource = {};
  devices.forEach((d) => { (devicesBySource[d.source_id] ||= []).push(d); });
  const standalone = sources.filter((s) => s.connection_kind === 'device');
  const platforms = sources.filter((s) => s.connection_kind !== 'device');
  // One kind at a time, as tabs: ?view= picks, otherwise whichever kind this
  // account actually has (standalone first when it has both).
  const asked = route?.query?.view;
  const view = asked === 'platforms' || asked === 'standalone' ? asked
    : standalone.length || !platforms.length ? 'standalone' : 'platforms';
  const count = standalone.length + platforms.reduce((n, s) => n + (devicesBySource[s.id] || []).length, 0);

  mount.innerHTML = sources.length ? `
    <div class="page-bar">
      <span class="hint">${count} terminal${count === 1 ? '' : 's'} across ${sources.length} connection${sources.length === 1 ? '' : 's'}</span>
      <a class="btn sm" href="#/settings/biometric">Manage Connections</a>
    </div>
    <nav class="tabs" aria-label="Connection type">
      <a href="#/terminals?view=standalone" class="${view === 'standalone' ? 'active' : ''}"
         ${view === 'standalone' ? 'aria-current="page"' : ''}>Standalone Devices <span class="tab-count">${standalone.length}</span></a>
      <a href="#/terminals?view=platforms" class="${view === 'platforms' ? 'active' : ''}"
         ${view === 'platforms' ? 'aria-current="page"' : ''}>Platform Servers <span class="tab-count">${platforms.length}</span></a>
    </nav>
    ${view === 'standalone'
      ? (standalone.length ? standaloneCard(standalone, devicesBySource)
        : `<div class="card">${empty('No Standalone Devices', 'Add one in Settings → Biometric connections.')}</div>`)
      : (platforms.length ? platforms.map((s) => platformCard(s, devicesBySource[s.id] || [])).join('')
        : `<div class="card">${empty('No Platform Servers', 'Add one in Settings → Biometric connections.')}</div>`)}
  ` : `
    <div class="card">
      ${empty('No Biometric Connections Yet', 'Add a BioTime server or a standalone device, then its terminals show up here.')}
      ${auth.canWrite ? '<div class="row" style="justify-content:center"><a class="btn primary-link" href="#/settings/biometric?add=1">Add Connection</a></div>' : ''}
    </div>`;

  const onPick = (selector, save) => mount.querySelectorAll(selector).forEach((select) => {
    select.addEventListener('change', async () => {
      select.disabled = true;
      try {
        await save(select);
        await renderTerminals(mount, route);
      } catch (err) {
        select.disabled = false;
        toast(err.message || 'Could not save the company', 'bad');
      }
    });
  });
  onPick('[data-device-company]', saveDeviceCompany);
  onPick('[data-source-company]', saveSourceCompany);

  mount.querySelectorAll('[data-sync-source]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      const label = button.textContent;
      button.textContent = 'Syncing…';
      await syncSource(button.dataset.syncSource);
      button.disabled = false;
      button.textContent = label;
      await renderTerminals(mount, route);
    });
  });
}
