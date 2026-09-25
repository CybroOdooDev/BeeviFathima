/* The three read-heavy screens: attendance, employees, activity. */

import { api, auth } from '../api.js';
import {
  $, busy, empty, esc, field, fmtHours, fmtLocal, fmtUtc, guard, loading, pill,
  readForm, todayISO,
} from '../ui.js';

/* --- Attendance ----------------------------------------------------------- */
export async function renderAttendance(mount, route) {
  const from = route.query.from || todayISO(-7);
  const to = route.query.to || todayISO();
  const emp = route.query.emp || '';

  mount.innerHTML = `
    <form class="card" id="filters" style="margin-bottom:14px">
      <div class="grid cols-4" style="align-items:end">
        ${field({ name: 'from', label: 'From', type: 'date', value: from })}
        ${field({ name: 'to', label: 'To', type: 'date', value: to })}
        ${field({ name: 'emp', label: 'Badge', value: emp, placeholder: 'any' })}
        <div class="field"><button class="primary" style="width:100%">Apply</button></div>
      </div>
    </form>
    <div id="rows">${loading()}</div>`;

  $('#filters', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    const query = new URLSearchParams(
      Object.entries(values).filter(([, v]) => v)
    ).toString();
    window.location.hash = `#/attendance${query ? `?${query}` : ''}`;
  });

  const params = new URLSearchParams({ date_from: from, date_to: to, limit: '200' });
  if (emp) params.set('emp_code', emp);
  const rows = await api.get(`/attendance?${params}`);

  $('#rows', mount).innerHTML = rows.length ? `
    <div class="card">
      <h2>Attendance <span class="hint">${rows.length} interval${rows.length === 1 ? '' : 's'}, times in your timezone, elapsed is punch to punch</span></h2>
      <div class="scroll">
        <table>
          <thead><tr>
            <th>Badge</th><th>Employee</th><th>Date</th><th>In</th><th>Out</th>
            <th class="num" title="Punch to punch. Odoo's own Worked Hours can read lower: from 17 onward it subtracts the break in the employee's working schedule.">Elapsed</th>
            <th>Device</th><th>Flags</th><th class="num">Odoo</th>
          </tr></thead>
          <tbody>
            ${rows.map((r) => `
              <tr>
                <td class="mono">${esc(r.emp_code)}</td>
                <td>${esc(r.employee_name || '—')}</td>
                <td>${esc(r.shift_date || '—')}</td>
                <td>${esc(fmtLocal(r.check_in_local)?.slice(11) || '—')}</td>
                <td>${r.check_out_local ? esc(fmtLocal(r.check_out_local).slice(11)) : '<span class="pill warn">open</span>'}</td>
                <td class="num">${esc(fmtHours(r.worked_hours))}</td>
                <td class="mono">${esc(r.device_serial || '—')}</td>
                <td>${flags(r)}</td>
                <td class="num mono">${esc(r.odoo_attendance_id ?? '—')}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>` : empty('No attendance in this range', 'Widen the dates, or run a sync.');
}

function flags(record) {
  const out = [];
  if (record.is_late) out.push(`<span class="pill warn">late ${record.late_minutes}m</span>`);
  if (record.is_auto_closed) out.push('<span class="pill warn">auto-closed</span>');
  if (record.is_orphan_out) out.push('<span class="pill bad">orphan out</span>');
  return out.join(' ') || '<span class="pill ok">clean</span>';
}

/* --- Employees ------------------------------------------------------------ */
export async function renderEmployees(mount) {
  mount.innerHTML = loading();
  const mappings = await api.get('/mappings?limit=500');

  const attention = mappings.filter((m) => ['unmapped', 'ambiguous'].includes(m.status));
  const resolved = mappings.filter((m) => !['unmapped', 'ambiguous'].includes(m.status));

  mount.innerHTML = `
    ${attention.length ? `
      <div class="card" style="margin-bottom:14px">
        <h2>Needs attention <span class="hint">${attention.length} badge${attention.length === 1 ? '' : 's'} holding attendance</span></h2>
        <p style="color:var(--muted);margin:0 0 12px;font-size:13px">
          These badges have punched but no Odoo employee carries them. The clean fix
          is to set the badge as the employee's <strong>Badge ID</strong> in Odoo, then
          re-run a sync — matching is automatic from then on. Or match one here.
        </p>
        <div class="scroll">
          <table>
            <thead><tr><th>Badge</th><th>Name seen</th><th>Status</th><th>Why</th><th></th></tr></thead>
            <tbody>
              ${attention.map((m) => `
                <tr>
                  <td class="mono">${esc(m.emp_code)}</td>
                  <td>${esc(m.source_name || '—')}</td>
                  <td>${pill(m.status)}</td>
                  <td style="color:var(--muted);font-size:12.5px">${esc(m.match_note || '—')}</td>
                  <td style="text-align:right">
                    ${auth.canWrite ? `
                      <div class="row" style="justify-content:flex-end;flex-wrap:nowrap">
                        <input style="width:110px" placeholder="Odoo id" data-id="${esc(m.id)}">
                        <button class="sm" data-map="${esc(m.id)}">Match</button>
                        <button class="sm" data-ignore="${esc(m.id)}">Ignore</button>
                      </div>` : ''}
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}

    <div class="card">
      <h2>Matched badges <span class="hint">${resolved.length}</span></h2>
      ${resolved.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Badge</th><th>Odoo employee</th><th class="num">Odoo id</th><th>Matched by</th><th>Status</th><th>Open shift</th><th>Last punch</th></tr></thead>
            <tbody>
              ${resolved.map((m) => `
                <tr>
                  <td class="mono">${esc(m.emp_code)}</td>
                  <td>${esc(m.odoo_employee_name || '—')}</td>
                  <td class="num mono">${esc(m.odoo_employee_id ?? '—')}</td>
                  <td>${esc(m.match_method || '—')}</td>
                  <td>${pill(m.status)}</td>
                  <td>${m.open_attendance_id
                    ? `<span class="pill warn">#${esc(m.open_attendance_id)}</span>`
                    : '<span class="pill mute">none</span>'}</td>
                  <td>${esc(fmtUtc(m.last_punch_at))}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('Nothing matched yet', 'Badges appear here after the first sync.')}
    </div>`;

  mount.querySelectorAll('[data-map]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.map;
      const input = mount.querySelector(`[data-id="${id}"]`);
      const value = Number(input.value);
      if (!value) {
        input.focus();
        return;
      }
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${id}`, { odoo_employee_id: value });
          await renderEmployees(mount);
        }, 'Badge matched — its held punches will sync on the next run')
      );
    });
  });

  mount.querySelectorAll('[data-ignore]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${button.dataset.ignore}`, { status: 'ignored' });
          await renderEmployees(mount);
        }, 'Badge ignored')
      )
    );
  });
}

/* --- Activity ------------------------------------------------------------- */
export async function renderActivity(mount, route) {
  const stateFilter = route.query.state || '';
  const badge = route.query.emp_code || '';
  const terminal = route.query.terminal_sn || '';
  const runId = route.query.run_id || '';
  const from = route.query.date_from || '';
  const to = route.query.date_to || '';
  mount.innerHTML = loading();

  // Everything except the state tabs lives in the query string, so a filtered
  // ledger is a URL you can paste into a ticket.
  const params = new URLSearchParams({ limit: '200' });
  if (stateFilter) params.set('state', stateFilter);
  if (badge) params.set('emp_code', badge);
  if (terminal) params.set('terminal_sn', terminal);
  if (runId) params.set('run_id', runId);
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);

  const [runs, punches, devices] = await Promise.all([
    api.get('/sync/runs?limit=20'),
    api.get(`/punches?${params}`),
    api.get('/devices').catch(() => []),
  ]);

  const states = ['', 'pending', 'synced', 'unmapped', 'error', 'skipped', 'deleted'];

  /** Keep the state tab while changing a filter, and vice versa. */
  const linkFor = (overrides) => {
    const q = new URLSearchParams();
    const merged = { state: stateFilter, emp_code: badge, terminal_sn: terminal,
                     run_id: runId, date_from: from, date_to: to, ...overrides };
    Object.entries(merged).forEach(([k, v]) => { if (v) q.set(k, v); });
    const s = q.toString();
    return `#/activity${s ? `?${s}` : ''}`;
  };

  const filtered = Boolean(badge || terminal || runId || from || to);
  const shownRun = runId ? runs.find((r) => r.id === runId) : null;

  mount.innerHTML = `
    <div class="card" style="margin-bottom:14px">
      <h2>Sync runs</h2>
      ${runs.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Started</th><th>Result</th><th class="num">Fetched</th><th class="num">New</th><th class="num">Created</th><th class="num">Closed</th><th class="num">Errors</th><th>Trigger</th><th></th></tr></thead>
            <tbody>
              ${runs.map((r) => `
                <tr>
                  <td>${esc(fmtUtc(r.started_at))}</td>
                  <td>${pill(r.status)}</td>
                  <td class="num">${esc(r.punches_fetched)}</td>
                  <td class="num">${esc(r.punches_new)}</td>
                  <td class="num">${esc(r.attendances_created)}</td>
                  <td class="num">${esc(r.attendances_closed)}</td>
                  <td class="num">${esc(r.error_count)}</td>
                  <td>${esc(r.triggered_by)}</td>
                  <td style="text-align:right;white-space:nowrap">
                    ${r.punches_new
                      ? `<a class="link sm" href="${linkFor({
                          run_id: r.id, state: '', emp_code: '',
                          terminal_sn: '', date_from: '', date_to: '' })}"
                         >${esc(r.punches_new)} punch${r.punches_new === 1 ? '' : 'es'}</a>`
                      : '<span class="hint">no new punches</span>'}
                    <button class="link sm" data-log="${esc(r.id)}">Log</button>
                  </td>
                </tr>
                <tr class="hidden" id="log-${esc(r.id)}">
                  <td colspan="9">
                    ${r.error_message ? `<p class="err" style="margin-top:0">${esc(r.error_message)}</p>` : ''}
                    <pre class="log">${esc((r.log || []).join('\n') || 'No log lines recorded.')}</pre>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No runs yet')}
    </div>

    <div class="card">
      <h2>Punch ledger <span class="hint">${filtered
        ? `${punches.length} punch${punches.length === 1 ? '' : 'es'} matching`
        : 'every punch ever pulled'}</span></h2>
      ${shownRun ? `
        <div class="banner" style="margin:0 0 12px">
          <strong>The sync that started ${esc(fmtUtc(shownRun.started_at))}</strong>
          Read ${esc(shownRun.punches_fetched)} punch${shownRun.punches_fetched === 1 ? '' : 'es'}
          from the device platform and added ${esc(shownRun.punches_new)} to the ledger${
            shownRun.punches_fetched > shownRun.punches_new
              ? `. The other ${
                  esc(shownRun.punches_fetched - shownRun.punches_new)
                } ${shownRun.punches_fetched - shownRun.punches_new === 1
                  ? 'was' : 'were'} already here — every run deliberately
                 re-reads a window of known punches, because devices upload
                 late and their clocks drift. Those stay listed under the run
                 that first saw them.`
              : '.'}
          <a href="${linkFor({ run_id: '' })}">Show the whole ledger</a>
        </div>` : ''}
      <div class="tabs">
        ${states.map((s) => `
          <a href="${linkFor({ state: s })}" class="${s === stateFilter ? 'active' : ''}">
            ${esc(s || 'all')}</a>`).join('')}
      </div>

      <form id="punchFilters" class="row" style="gap:10px;flex-wrap:wrap;margin:12px 0 4px;align-items:flex-end">
        <div class="field" style="margin:0;min-width:150px">
          <label for="terminal_sn">Device</label>
          <select name="terminal_sn" id="terminal_sn">
            <option value="">any device</option>
            ${devices.map((d) => `<option value="${esc(d.serial_number)}"${
              d.serial_number === terminal ? ' selected' : ''
            }>${esc(d.alias || d.serial_number)}</option>`).join('')}
          </select>
        </div>
        <div class="field" style="margin:0;width:130px">
          <label for="emp_code">Badge</label>
          <input type="text" name="emp_code" id="emp_code" value="${esc(badge)}" placeholder="any">
        </div>
        <div class="field" style="margin:0;width:165px">
          <label for="date_from">From</label>
          <input type="date" name="date_from" id="date_from" value="${esc(from)}">
        </div>
        <div class="field" style="margin:0;width:165px">
          <label for="date_to">To</label>
          <input type="date" name="date_to" id="date_to" value="${esc(to)}">
        </div>
        <button class="primary" id="applyPunches">Apply</button>
        ${filtered ? `<a class="btn" href="${linkFor({
          emp_code: '', terminal_sn: '', date_from: '', date_to: '' })}">Clear</a>` : ''}
      </form>
      ${punches.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Punch time (UTC)</th><th>Local</th><th>Badge</th><th>Dir</th><th>Device</th><th>State</th><th class="num">Odoo</th><th>Detail</th><th></th></tr></thead>
            <tbody>
              ${punches.map((p) => `
                <tr>
                  <td class="mono">${esc(fmtUtc(p.punch_time_utc))}</td>
                  <td class="mono">${esc(fmtUtc(p.punch_time_local))}</td>
                  <td class="mono">${esc(p.emp_code)}</td>
                  <td>${esc(p.direction)}</td>
                  <td class="mono">${esc(p.terminal_sn || '—')}</td>
                  <td>${pill(p.process_state)}</td>
                  <td class="num mono">${esc(p.odoo_attendance_id ?? '—')}</td>
                  <td style="color:var(--muted);font-size:12.5px">${esc(p.error_message || '—')}</td>
                  <td style="text-align:right">
                    ${auth.canWrite && ['error', 'skipped', 'unmapped'].includes(p.process_state)
                      ? `<button class="sm" data-retry="${esc(p.id)}">Retry</button>
                         <button class="sm" data-delete="${esc(p.id)}"
                                 title="Remove from the queue. It won't be pushed, and won't come back on the next sync.">Delete</button>` : ''}
                    ${auth.canWrite && p.process_state === 'deleted'
                      ? `<button class="sm" data-retry="${esc(p.id)}"
                                 title="Put it back in the queue for the next sync">Restore</button>` : ''}
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty(
          'No punches',
          filtered
            ? 'Nothing matches these filters. Widen the dates, or clear them.'
            : stateFilter
              ? `Nothing in the ${stateFilter} state.`
              : 'Run a sync to pull some.'
        )}
    </div>`;

  $('#punchFilters', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    window.location.hash = linkFor(values);
  });

  mount.querySelectorAll('[data-log]').forEach((button) => {
    button.addEventListener('click', () => {
      $(`#log-${button.dataset.log}`, mount).classList.toggle('hidden');
    });
  });

  mount.querySelectorAll('[data-retry]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.post(`/punches/${button.dataset.retry}/retry`);
          await renderActivity(mount, route);
        }, 'Queued for the next sync')
      )
    );
  });

  // Two clicks, no confirm() dialog (nothing in this app uses one): the first
  // arms the button for a few seconds, the second deletes.
  mount.querySelectorAll('[data-delete]').forEach((button) => {
    let armed = null;
    button.addEventListener('click', () => {
      if (!armed) {
        button.textContent = 'Confirm delete';
        button.classList.add('danger');
        armed = setTimeout(() => {
          armed = null;
          button.textContent = 'Delete';
          button.classList.remove('danger');
        }, 4000);
        return;
      }
      clearTimeout(armed);
      armed = null;
      busy(button, () =>
        guard(async () => {
          await api.del(`/punches/${button.dataset.delete}`);
          await renderActivity(mount, route);
        }, 'Punch deleted')
      );
    });
  });
}
