/* Terminals: every device the account's biometric connections bring in.
 *
 * Standalone devices are each their own connection, so each row has its own
 * Sync now. Terminals behind a platform server (BioTime) are all read through
 * that server in one go, so the Sync now sits on the platform, once. */

import { api, auth } from '../api.js';
import { empty, esc, fmtAgo, loading, pill } from '../ui.js';
import { syncSource } from './settings.js';

function deviceState(d) {
  if (d.missing_since) return pill('missing', 'missing');
  return d.is_enabled ? pill('active', 'enabled') : pill('skipped', 'disabled');
}

const syncButton = (source, label = 'Sync now') => (auth.canWrite && source.is_active !== false
  ? `<button class="sm primary" data-sync-source="${esc(source.id)}"
             title="Pull ${esc(source.name)}'s punches now and push them to Odoo">${esc(label)}</button>`
  : '');

function standaloneCard(sources, devicesBySource) {
  if (!sources.length) return '';
  return `
    <div class="card" style="margin-bottom:18px">
      <h2>Standalone devices <span class="hint">${sources.length} connected directly</span></h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Device</th><th>Address</th><th>Serial</th><th>Connection</th>
            <th class="num">Punches</th><th>Last seen</th><th></th></tr></thead>
          <tbody>
            ${sources.map((s) => {
              const d = (devicesBySource[s.id] || [])[0];
              return `
                <tr>
                  <td><div><strong>${esc(d?.alias || s.name)}</strong></div>
                      ${d ? `<div class="hint">${deviceState(d)}</div>`
                          : '<div class="hint">not imported yet — Import terminals in Settings</div>'}</td>
                  <td class="mono">${esc(s.base_url.replace(/^zk:\/\//i, ''))}</td>
                  <td class="mono">${esc(d?.serial_number || '—')}</td>
                  <td>${pill(s.status)}<div class="hint">checked ${esc(fmtAgo(s.last_checked_at))}</div></td>
                  <td class="num">${esc(d?.punch_count ?? '—')}</td>
                  <td>${esc(d?.last_seen_at ? fmtAgo(d.last_seen_at) : '—')}</td>
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
        <h2>${esc(source.name)} <span class="hint">platform server · ${esc(source.base_url)}</span></h2>
        <div class="actions">
          ${pill(source.status)}
          <span class="hint">checked ${esc(fmtAgo(source.last_checked_at))}</span>
          ${syncButton(source)}
        </div>
      </div>
      ${devices.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Terminal</th><th>Serial</th><th>IP</th><th>Area</th>
              <th class="num">Punches</th><th>Last seen</th><th>State</th></tr></thead>
            <tbody>
              ${devices.map((d) => `
                <tr>
                  <td><strong>${esc(d.alias || d.serial_number)}</strong></td>
                  <td class="mono">${esc(d.serial_number)}</td>
                  <td class="mono">${esc(d.ip_address || '—')}</td>
                  <td>${esc(d.area || '—')}</td>
                  <td class="num">${esc(d.punch_count)}</td>
                  <td>${esc(d.last_seen_at ? fmtAgo(d.last_seen_at) : '—')}</td>
                  <td>${deviceState(d)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No terminals imported yet',
          'Use “Import terminals” on this connection in Settings → Biometric connections.')}
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
  const standalone = sources.filter((s) => s.connection_kind === 'device');
  const platforms = sources.filter((s) => s.connection_kind !== 'device');
  const count = standalone.length + platforms.reduce((n, s) => n + (devicesBySource[s.id] || []).length, 0);

  mount.innerHTML = sources.length ? `
    <div class="page-bar">
      <span class="hint">${count} terminal${count === 1 ? '' : 's'} across ${sources.length} connection${sources.length === 1 ? '' : 's'}</span>
      <a class="btn sm" href="#/settings/biometric">Manage connections</a>
    </div>
    ${standaloneCard(standalone, devicesBySource)}
    ${platforms.map((s) => platformCard(s, devicesBySource[s.id] || [])).join('')}
  ` : `
    <div class="card">
      ${empty('No biometric connections yet', 'Add a BioTime server or a standalone device, then its terminals show up here.')}
      ${auth.canWrite ? '<div class="row" style="justify-content:center"><a class="btn primary-link" href="#/settings/biometric?add=1">Add connection</a></div>' : ''}
    </div>`;

  mount.querySelectorAll('[data-sync-source]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      const label = button.textContent;
      button.textContent = 'Syncing…';
      await syncSource(button.dataset.syncSource);
      button.disabled = false;
      button.textContent = label;
      await renderTerminals(mount);
    });
  });
}
