/* Connections: Odoo, the device platform, and the terminals it publishes. */

import { api, auth } from '../api.js';
import {
  $, banner, busy, empty, esc, field, fmtAgo, guard, loading, pill, readForm, toast,
} from '../ui.js';

export async function render(mount) {
  mount.innerHTML = loading();
  const [odooList, sources, devices, providers] = await Promise.all([
    api.get('/odoo-connections'),
    api.get('/sources'),
    api.get('/devices').catch(() => []),
    api.get('/providers').catch(() => []),
  ]);

  const odoo = odooList[0] || null;
  const source = sources[0] || null;
  const readonly = !auth.canWrite;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card" style="margin-bottom:14px">
      <h2>Odoo <span class="hint">where attendance is written</span></h2>
      ${odoo ? statusRow(odoo) : ''}
      <form id="odooForm" ${readonly ? 'inert' : ''}>
        ${field({
          name: 'url', label: 'Server URL', required: true,
          value: odoo?.url || '', placeholder: 'https://acme.odoo.com',
          help: 'Just the address — no /odoo or /web on the end. Odoo 17+ shows those in the browser bar, and they are the most common cause of a failed connection.',
          strongHelp: true,
        })}
        ${field({
          name: 'db_name', label: 'Database', required: true, value: odoo?.db_name || '',
          help: 'On Odoo Online this is usually the subdomain.',
        })}
        ${field({ name: 'username', label: 'Login', required: true, value: odoo?.username || '' })}
        ${field({
          name: 'api_key', label: odoo ? 'API key' : 'API key', type: 'password',
          required: !odoo,
          placeholder: odoo ? 'unchanged' : '',
          help: odoo
            ? 'Stored encrypted and never shown again. Leave blank to keep the current one.'
            : 'Odoo → Preferences → Account Security → New API Key.',
        })}
        <div class="row">
          <button class="primary" id="saveOdoo">${odoo ? 'Save changes' : 'Connect Odoo'}</button>
          ${odoo ? '<button type="button" id="testOdoo">Test connection</button>' : ''}
        </div>
      </form>
    </div>

    <div class="card" style="margin-bottom:14px">
      <h2>Device platform <span class="hint">where punches come from</span></h2>
      ${source ? statusRow(source) : ''}
      <form id="sourceForm" ${readonly ? 'inert' : ''}>
        ${providers.length > 1 && !source ? field({
          name: 'provider', label: 'Platform', required: true,
          options: providers.map((p) => ({ value: p.slug, label: p.label })),
        }) : ''}
        ${field({
          name: 'base_url', label: 'Server URL', required: true,
          value: source?.base_url || '', placeholder: 'https://biotime.example.com:8081',
        })}
        ${field({ name: 'username', label: 'Username', required: true, value: source?.username || '' })}
        ${field({
          name: 'password', label: 'Password', type: 'password', required: !source,
          placeholder: source ? 'unchanged' : '',
          help: source ? 'Leave blank to keep the current one.' : '',
        })}
        ${field({
          name: 'server_timezone', label: 'Server timezone', required: true,
          value: source?.server_timezone || 'UTC',
          help: 'The zone the BioTime machine itself runs in — not yours and not Odoo’s. Punch times arrive with no offset, so a wrong value shifts every attendance record by hours without any error.',
          strongHelp: true,
        })}
        ${field({
          name: 'auth_type', label: 'Auth style', value: source?.auth_type || 'token',
          options: ['token', 'jwt'],
          help: 'BioTime 8.5+ usually needs jwt; older builds use token.',
        })}
        <div class="row">
          <button class="primary" id="saveSource">${source ? 'Save changes' : 'Connect platform'}</button>
          ${source ? '<button type="button" id="testSource">Test connection</button>' : ''}
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Terminals <span class="hint">${devices.length} imported</span></h2>
      ${devices.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Device</th><th>Serial</th><th>IP</th><th class="num">Punches</th><th>Last seen</th><th>Pairing</th><th></th></tr></thead>
            <tbody>
              ${devices.map((d) => `
                <tr data-device="${esc(d.id)}">
                  <td>${esc(d.alias || '—')}</td>
                  <td class="mono">${esc(d.serial_number)}</td>
                  <td class="mono">${esc(d.ip_address || '—')}</td>
                  <td class="num">${esc(d.punch_count)}</td>
                  <td>${esc(fmtAgo(d.last_seen_at))}</td>
                  <td>${esc(d.pairing_override || 'account default')}</td>
                  <td style="text-align:right">
                    ${auth.canWrite ? `<button class="sm" data-toggle="${esc(d.id)}" data-on="${d.is_enabled}">
                      ${d.is_enabled ? 'Disable' : 'Enable'}</button>` : pill(d.is_enabled ? 'active' : 'skipped')}
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No terminals yet', 'Import them from the platform once it is connected.')}
      ${source && auth.canWrite ? `
        <div class="row" style="margin-top:14px">
          <button id="discover">Import terminals</button>
        </div>` : ''}
    </div>`;

  if (readonly) return;

  // --- Odoo ---------------------------------------------------------------
  $('#odooForm', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    if (odoo && !values.api_key) delete values.api_key;
    busy($('#saveOdoo', mount), () =>
      guard(async () => {
        if (odoo) await api.patch(`/odoo-connections/${odoo.id}`, values);
        else await api.post('/odoo-connections', values);
        await render(mount);
      }, 'Odoo connection saved')
    );
  });

  $('#testOdoo', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        const result = await api.post(`/odoo-connections/${odoo.id}/test`);
        toast(result.message, result.ok ? 'ok' : 'bad');
        await render(mount);
      })
    )
  );

  // --- device platform ----------------------------------------------------
  $('#sourceForm', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    if (source && !values.password) delete values.password;
    busy($('#saveSource', mount), () =>
      guard(async () => {
        if (source) await api.patch(`/sources/${source.id}`, values);
        else await api.post('/sources', values);
        await render(mount);
      }, 'Device platform saved')
    );
  });

  $('#testSource', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        const result = await api.post(`/sources/${source.id}/test`);
        toast(result.message, result.ok ? 'ok' : 'bad');
        await render(mount);
      })
    )
  );

  $('#discover', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        const found = await api.post(`/sources/${source.id}/discover-devices`);
        await render(mount);
        return found;
      }, 'Terminals imported')
    )
  );

  mount.querySelectorAll('[data-toggle]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/devices/${button.dataset.toggle}`, {
            is_enabled: button.dataset.on !== 'true',
          });
          await render(mount);
        })
      )
    );
  });
}

function statusRow(connection) {
  return `
    <div class="row" style="margin-bottom:14px">
      ${pill(connection.status)}
      <span style="color:var(--muted);font-size:12.5px">
        checked ${esc(fmtAgo(connection.last_checked_at))}
      </span>
    </div>
    ${connection.status_message
      ? banner('Last error', connection.status_message, 'bad') : ''}`;
}
