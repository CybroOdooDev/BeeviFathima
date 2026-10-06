/* The three read-heavy screens: attendance, employees, activity. */

import { api, auth } from '../api.js';
import {
  $, busy, empty, esc, field, fmtAgo, fmtHours, fmtLocal, fmtUtc, guard, loading, pill,
  readForm, stat, todayISO,
} from '../ui.js';

const TRASH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';

/* --- Attendance ----------------------------------------------------------- */

/** Quick ranges: the question is almost always "today", "yesterday" or "this
 * week", and picking two dates for it was two date pickers too many. */
function rangePresets() {
  const today = todayISO();
  const d = new Date();
  const monthStart = new Date(d.getFullYear(), d.getMonth(), 1);
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
  return [
    { label: 'Today', from: today, to: today },
    { label: 'Yesterday', from: todayISO(-1), to: todayISO(-1) },
    { label: 'Last 7 days', from: todayISO(-6), to: today },
    { label: 'This month', from: iso(monthStart), to: today },
    { label: 'Last 30 days', from: todayISO(-29), to: today },
  ];
}

export async function renderAttendance(mount, route) {
  const from = route.query.from || todayISO(-6);
  const to = route.query.to || todayISO();
  const emp = route.query.emp || '';
  const q = route.query.q || '';
  const presets = rangePresets();
  const linkFor = (overrides) => {
    const params = new URLSearchParams(Object.entries({ from, to, emp, q, ...overrides }).filter(([, v]) => v));
    return `#/attendance?${params}`;
  };

  mount.innerHTML = `
    <div class="card filter-bar" style="margin-bottom:14px">
      <div class="chips" role="group" aria-label="Date range">
        ${presets.map((p) => `
          <a class="chip ${p.from === from && p.to === to ? 'on' : ''}" href="${esc(linkFor({ from: p.from, to: p.to }))}">
            ${esc(p.label)}</a>`).join('')}
      </div>
      <form id="filters" class="filter-row">
        <label class="inline-field"><span>From</span><input type="date" name="from" value="${esc(from)}"></label>
        <label class="inline-field"><span>To</span><input type="date" name="to" value="${esc(to)}"></label>
        <label class="inline-field grow"><span>Find</span>
          <input type="search" name="q" value="${esc(q || emp)}" placeholder="Name or badge" autocomplete="off"></label>
        <button class="primary sm" type="submit">Show</button>
      </form>
    </div>
    <div id="rows">${loading()}</div>`;

  $('#filters', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    window.location.hash = linkFor({ from: values.from, to: values.to, q: values.q, emp: '' });
  });

  const params = new URLSearchParams({ date_from: from, date_to: to, limit: '500' });
  if (emp) params.set('emp_code', emp);
  const all = await api.get(`/attendance?${params}`);

  const draw = (needle) => {
    const n = needle.trim().toLowerCase();
    const rows = n
      ? all.filter((r) => (r.employee_name || '').toLowerCase().includes(n) || String(r.emp_code).toLowerCase().includes(n))
      : all;
    const hours = rows.reduce((sum, r) => sum + (r.worked_hours || 0), 0);
    const people = new Set(rows.map((r) => r.emp_code)).size;
    const open = rows.filter((r) => !r.check_out_local).length;
    $('#rows', mount).innerHTML = rows.length ? `
      <div class="grid cols-3" style="margin-bottom:14px">
        ${stat({ label: 'People', value: people })}
        ${stat({ label: 'Hours', value: hours.toFixed(1), note: 'punch to punch' })}
        ${stat({ label: 'Still open', value: open, tone: open ? 'warn' : '', note: 'no check-out yet' })}
      </div>
      <div class="card">
        <h2>Shifts <span class="hint">${rows.length}${n ? ` matching “${esc(needle.trim())}”` : ''}${
          all.length >= 500 ? ' · showing the latest 500 — narrow the dates for more' : ''}</span></h2>
        <div class="scroll">
          <table>
            <thead><tr>
              <th>Employee</th><th>Date</th><th>In</th><th>Out</th>
              <th class="num" title="Punch to punch. Odoo's own Worked Hours can read lower: from 17 onward it subtracts the break in the employee's working schedule.">Elapsed</th>
              <th>Device</th><th>Flags</th><th class="num">Odoo</th>
            </tr></thead>
            <tbody>
              ${rows.map((r) => `
                <tr>
                  <td><div>${esc(r.employee_name || '—')}</div><div class="hint mono">${esc(r.emp_code)}</div></td>
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
      </div>` : empty(
        n ? `No one matching “${needle.trim()}”` : 'No attendance in this range',
        n ? 'Check the spelling, or clear the search.' : 'Pick a wider range above, or run a sync.'
      );
  };
  draw(q || '');

  // Search narrows what is already loaded, as you type — no Show press needed.
  const box = mount.querySelector('input[name=q]');
  box.addEventListener('input', () => draw(box.value));
}

function flags(record) {
  const out = [];
  if (record.is_auto_closed) out.push('<span class="pill warn">auto-closed</span>');
  if (record.is_orphan_out) out.push('<span class="pill bad">orphan out</span>');
  return out.join(' ') || '<span class="pill ok">clean</span>';
}

/* --- Employees ------------------------------------------------------------ */

/* The badges still waiting for an Odoo employee. Lives on Settings → Odoo
 * connection (matching is configuration, and needs Odoo to search), reached
 * from the Employees page's "Show unmapped employees". */
export function unmappedCard(attention) {
  if (!attention.length) {
    return `
      <div class="card" id="unmapped" style="margin-top:18px">
        <h2>Unmapped employees <span class="hint">none</span></h2>
        ${empty('Every badge is matched', 'New badges show up here after a sync, until they are matched.')}
      </div>`;
  }
  return `
    <div class="card" id="unmapped" style="margin-top:18px">
      <h2>Unmapped employees <span class="hint">${attention.length} badge${attention.length === 1 ? '' : 's'} holding attendance</span></h2>
      <p style="color:var(--muted);margin:0 0 12px;font-size:13px">
        These badges have punched but no Odoo employee carries them, so their
        attendance is held. Pick the employee each one belongs to — or, for
        good, set the badge as that employee's <strong>Badge ID</strong> in Odoo
        and matching becomes automatic.
      </p>
      <div class="scroll" style="overflow:visible">
        <table>
          <thead><tr><th>Badge</th><th>Name on the device</th><th>Status</th><th>Match to Odoo employee</th></tr></thead>
          <tbody>
            ${attention.map((m) => `
              <tr>
                <td class="mono">${esc(m.emp_code)}</td>
                <td>${esc(m.source_name || '—')}${m.match_note ? `<div class="hint">${esc(m.match_note)}</div>` : ''}</td>
                <td>${pill(m.status === 'unmapped' ? 'unmapped' : m.status, m.status === 'unmapped' ? 'not matched' : m.status)}</td>
                <td>
                  ${auth.canWrite ? `
                    <div class="match-row">
                      <div class="picker" data-picker="${esc(m.id)}">
                        <input type="search" placeholder="Search Odoo employees…" autocomplete="off"
                               aria-label="Odoo employee for badge ${esc(m.emp_code)}" value="${esc(m.source_name && m.source_name !== 'Unknown badge' ? m.source_name : '')}">
                        <ul class="picker-list hidden" role="listbox"></ul>
                      </div>
                      <button class="sm primary" data-map="${esc(m.id)}" disabled>Match</button>
                      <button class="sm link" data-ignore="${esc(m.id)}" title="Stop holding attendance for this badge — its punches are skipped.">Ignore</button>
                    </div>` : '<span class="hint">Your role cannot match badges.</span>'}
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

export function wireUnmapped(mount, rerender) {
  mount.querySelectorAll('[data-picker]').forEach((box) => wirePicker(box, mount, rerender));
  mount.querySelectorAll('[data-ignore]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/mappings/${button.dataset.ignore}`, { status: 'ignored' });
          await rerender();
        }, 'Badge ignored')
      )
    );
  });
}

export const needsMatch = (m) => ['unmapped', 'ambiguous'].includes(m.status);

export async function renderEmployees(mount) {
  mount.innerHTML = loading();
  const mappings = await api.get('/mappings?limit=500');
  const waiting = mappings.filter(needsMatch).length;
  const resolved = mappings.filter((m) => !needsMatch(m));

  mount.innerHTML = `
    <div class="card">
      <div class="card-head" style="position:static">
        <h2>Employees <span class="hint">${resolved.length} matched to Odoo</span></h2>
        <div class="actions">
          ${resolved.length > 8 ? '<input type="search" id="matchedFilter" class="compact-search" placeholder="Filter by name or badge">' : ''}
          ${waiting ? `<a class="btn sm warn-link" href="#/settings/odoo?show=unmapped">
            Show unmapped employees (${waiting})</a>` : ''}
        </div>
      </div>
      ${resolved.length ? `
        <div class="scroll">
          <table id="matchedTable">
            <thead><tr><th>Employee</th><th>Badge</th><th>Matched by</th><th>Status</th><th>Open shift</th><th>Last punch</th></tr></thead>
            <tbody>
              ${resolved.map((m) => `
                <tr data-search="${esc(`${m.odoo_employee_name || ''} ${m.emp_code}`.toLowerCase())}">
                  <td><div>${esc(m.odoo_employee_name || '—')}</div>${m.odoo_employee_id ? `<div class="hint mono">Odoo #${esc(m.odoo_employee_id)}</div>` : ''}</td>
                  <td class="mono">${esc(m.emp_code)}</td>
                  <td>${esc(m.match_method || '—')}</td>
                  <td>${pill(m.status)}</td>
                  <td>${m.open_attendance_id
                    ? `<span class="pill warn">open #${esc(m.open_attendance_id)}</span>`
                    : '<span class="pill mute">none</span>'}</td>
                  <td>${esc(m.last_punch_at ? fmtAgo(m.last_punch_at) : '—')}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('Nothing matched yet', waiting
          ? 'Badges have punched but none is matched yet — use “Show unmapped employees” above.'
          : 'Badges appear here after the first sync.')}
    </div>`;

  $('#matchedFilter', mount)?.addEventListener('input', (event) => {
    const n = event.target.value.trim().toLowerCase();
    mount.querySelectorAll('#matchedTable tbody tr').forEach((tr) => {
      tr.classList.toggle('hidden', Boolean(n) && !tr.dataset.search.includes(n));
    });
  });
}

/** Search-as-you-type over the connected Odoo's employees.
 *
 * Picking one enables Match. If Odoo cannot be searched (not connected, or
 * refusing), typing a numeric Odoo id still works — the old way, kept as the
 * fallback rather than the only way. */
function wirePicker(box, mount, rerender) {
  const id = box.dataset.picker;
  const input = box.querySelector('input');
  const list = box.querySelector('.picker-list');
  const matchButton = mount.querySelector(`[data-map="${id}"]`);
  let picked = null;
  let timer = null;
  let seq = 0;

  const setPicked = (emp) => {
    picked = emp;
    matchButton.disabled = !picked;
    box.classList.toggle('chosen', Boolean(picked));
  };

  const show = (html) => {
    list.innerHTML = html;
    list.classList.toggle('hidden', !html);
  };

  const search = async () => {
    const q = input.value.trim();
    const mine = ++seq;
    let rows;
    try {
      rows = await api.get(`/odoo-employees?q=${encodeURIComponent(q)}&limit=12`);
    } catch (error) {
      if (mine !== seq) return;
      if (/^\d+$/.test(q)) {
        setPicked({ id: Number(q), name: null });
        show(`<li class="picker-note">Odoo can’t be searched right now (${esc(error.message)}). Match will use Odoo id ${esc(q)}.</li>`);
      } else {
        show(`<li class="picker-note">Odoo can’t be searched right now: ${esc(error.message)}. You can type the employee’s Odoo id instead.</li>`);
      }
      return;
    }
    if (mine !== seq) return;
    show(rows.length ? rows.map((r) => `
      <li role="option" data-emp="${esc(r.id)}" data-name="${esc(r.name)}" class="${r.matched_badge ? 'taken' : ''}">
        <span>${esc(r.name)}${r.department ? ` <span class="hint">· ${esc(r.department)}</span>` : ''}</span>
        <span class="hint">${r.matched_badge ? `has badge ${esc(r.matched_badge)}` : `#${esc(r.id)}`}</span>
      </li>`).join('') : `<li class="picker-note">No Odoo employee matches “${esc(q)}”.</li>`);
  };

  input.addEventListener('input', () => {
    setPicked(null);
    clearTimeout(timer);
    timer = setTimeout(search, 220);
  });
  input.addEventListener('focus', () => { if (!picked) search(); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') show('');
    if (event.key === 'Enter') {
      event.preventDefault();
      const first = list.querySelector('li[data-emp]:not(.taken)');
      if (first) first.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      else if (picked) matchButton.click();
    }
  });
  input.addEventListener('blur', () => setTimeout(() => show(''), 150));
  list.addEventListener('mousedown', (event) => {
    const li = event.target.closest('li[data-emp]');
    if (!li) return;
    event.preventDefault();
    if (li.classList.contains('taken')) return;
    setPicked({ id: Number(li.dataset.emp), name: li.dataset.name });
    input.value = li.dataset.name;
    show('');
  });

  matchButton.addEventListener('click', () => {
    if (!picked) return;
    busy(matchButton, () =>
      guard(async () => {
        await api.patch(`/mappings/${id}`, {
          odoo_employee_id: picked.id,
          ...(picked.name ? { odoo_employee_name: picked.name } : {}),
        });
        await rerender();
      }, `Matched${picked.name ? ` to ${picked.name}` : ''} — held punches sync on the next run`)
    );
  });
}

/* --- Activity ------------------------------------------------------------- */

const STATE_LABEL = {
  pending: 'Pending', synced: 'Synced', unmapped: 'Unmatched badge', error: 'Error',
  skipped: 'Skipped', deleted: 'Deleted', held: 'Held (device limit)',
};

function wireRunLogs(mount) {
  mount.querySelectorAll('[data-log]').forEach((button) => {
    button.addEventListener('click', () => {
      $(`#log-${button.dataset.log}`, mount).classList.toggle('hidden');
    });
  });
}
export async function renderActivity(mount, route) {
  const stateFilter = route.query.state || '';
  const badge = route.query.emp_code || '';
  const terminal = route.query.terminal_sn || '';
  const runId = route.query.run_id || '';
  const from = route.query.date_from || '';
  const to = route.query.date_to || '';
  // Two views of one history: the punches themselves (what most visits are
  // for) and the sync runs that brought them in.
  const view = route.query.view === 'runs' ? 'runs' : 'punches';
  const limit = Math.min(Number(route.query.limit) || 100, 500);
  mount.innerHTML = loading();

  // Everything lives in the query string, so a filtered ledger is a URL you
  // can paste into a ticket.
  const params = new URLSearchParams({ limit: String(limit) });
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

  const states = ['', 'pending', 'synced', 'unmapped', 'error', 'held', 'skipped', 'deleted'];

  /** Keep the state tab while changing a filter, and vice versa. */
  const linkFor = (overrides) => {
    const q = new URLSearchParams();
    const merged = { state: stateFilter, emp_code: badge, terminal_sn: terminal,
                     run_id: runId, date_from: from, date_to: to, view: view === 'runs' ? 'runs' : '',
                     ...overrides };
    Object.entries(merged).forEach(([k, v]) => { if (v) q.set(k, v); });
    const s = q.toString();
    return `#/activity${s ? `?${s}` : ''}`;
  };

  const filtered = Boolean(badge || terminal || runId || from || to);
  const shownRun = runId ? runs.find((r) => r.id === runId) : null;

  const viewTabs = `
    <div class="tabs">
      <a href="#/activity" class="${view === 'punches' ? 'active' : ''}">Punches</a>
      <a href="#/activity?view=runs" class="${view === 'runs' ? 'active' : ''}">Sync runs</a>
    </div>`;

  const runsCard = `
    <div class="card" style="margin-bottom:14px">
      <h2>Sync runs <span class="hint">the last ${runs.length}</span></h2>
      ${runs.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Started</th><th>Result</th><th class="num">Fetched</th><th class="num">New</th><th class="num">Created</th><th class="num">Closed</th><th class="num">Errors</th><th>Trigger</th><th></th></tr></thead>
            <tbody>
              ${runs.map((r) => `
                <tr>
                  <td><div>${esc(fmtAgo(r.started_at))}</div><div class="hint mono">${esc(fmtUtc(r.started_at))} UTC</div></td>
                  <td>${pill(r.status)}</td>
                  <td class="num">${esc(r.punches_fetched)}</td>
                  <td class="num">${esc(r.punches_new)}</td>
                  <td class="num">${esc(r.attendances_created)}</td>
                  <td class="num">${esc(r.attendances_closed)}</td>
                  <td class="num">${esc(r.error_count)}</td>
                  <td>${esc(r.triggered_by)}</td>
                  <td class="actions-cell"><div class="row-actions">
                    ${r.punches_new
                      ? `<a class="btn sm" href="${linkFor({
                          run_id: r.id, state: '', emp_code: '', view: '',
                          terminal_sn: '', date_from: '', date_to: '' })}"
                         >${esc(r.punches_new)} punch${r.punches_new === 1 ? '' : 'es'}</a>`
                      : '<span class="hint">no new punches</span>'}
                    <button class="sm" data-log="${esc(r.id)}">Log</button>
                  </div></td>
                </tr>
                <tr class="hidden" id="log-${esc(r.id)}">
                  <td colspan="9">
                    ${r.error_message ? `<p class="err" style="margin-top:0">${esc(r.error_message)}</p>` : ''}
                    <pre class="log">${esc((r.log || []).join('\n') || 'No log lines recorded.')}</pre>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : empty('No runs yet', 'Press Sync now at the top to run one.')}
    </div>`;

  if (view === 'runs') {
    mount.innerHTML = viewTabs + runsCard;
    wireRunLogs(mount);
    return;
  }

  mount.innerHTML = `
    ${viewTabs}
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
      <div class="chips" role="group" aria-label="State">
        ${states.map((s) => `
          <a href="${linkFor({ state: s, limit: '' })}" class="chip ${s === stateFilter ? 'on' : ''}">
            ${esc(s ? STATE_LABEL[s] : 'All')}</a>`).join('')}
      </div>

      <form id="punchFilters" class="filter-row" style="margin:14px 0 6px">
        <label class="inline-field"><span>Device</span>
          <select name="terminal_sn">
            <option value="">any device</option>
            ${devices.map((d) => `<option value="${esc(d.serial_number)}"${
              d.serial_number === terminal ? ' selected' : ''
            }>${esc(d.alias || d.serial_number)}</option>`).join('')}
          </select></label>
        <label class="inline-field"><span>Badge</span>
          <input type="text" name="emp_code" value="${esc(badge)}" placeholder="any" style="width:120px"></label>
        <label class="inline-field"><span>From</span>
          <input type="date" name="date_from" value="${esc(from)}"></label>
        <label class="inline-field"><span>To</span>
          <input type="date" name="date_to" value="${esc(to)}"></label>
        <div class="row-actions">
          <button class="primary sm" id="applyPunches">Apply</button>
          ${filtered ? `<a class="btn sm" href="${linkFor({
            emp_code: '', terminal_sn: '', date_from: '', date_to: '' })}">Clear</a>` : ''}
        </div>
      </form>
      ${punches.length ? `
        <div class="scroll">
          <table>
            <thead><tr><th>Punch time</th><th>Badge</th><th>Dir</th><th>Device</th><th>State</th><th class="num">Odoo</th><th>Detail</th><th></th></tr></thead>
            <tbody>
              ${punches.map((p) => `
                <tr>
                  <td><div class="mono">${esc(fmtUtc(p.punch_time_local || p.punch_time_utc))}</div>
                      <div class="hint mono">${esc(fmtUtc(p.punch_time_utc))} UTC</div></td>
                  <td class="mono">${esc(p.emp_code)}</td>
                  <td>${esc(p.direction)}</td>
                  <td class="mono">${esc(p.terminal_sn || '—')}</td>
                  <td>${pill(p.process_state)}</td>
                  <td class="num mono">${esc(p.odoo_attendance_id ?? '—')}</td>
                  <td style="color:var(--muted);font-size:12.5px">${esc(p.error_message || '—')}</td>
                  <td class="actions-cell"><div class="row-actions">
                    ${auth.canWrite && !p.odoo_attendance_id && ['error', 'skipped', 'unmapped'].includes(p.process_state)
                      ? `<button class="sm" data-retry="${esc(p.id)}">Retry</button>
                         <button type="button" class="icon-btn" data-delete="${esc(p.id)}" aria-label="Delete punch"
                                 title="Delete — removes it from the queue. It won't be pushed, and won't come back on the next sync.">${TRASH_ICON}</button>` : ''}
                    ${auth.canWrite && p.process_state === 'deleted'
                      ? `<button class="sm" data-retry="${esc(p.id)}"
                                 title="Put it back in the queue for the next sync">Restore</button>` : ''}
                  </div></td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${punches.length >= limit && limit < 500 ? `
          <div class="row" style="justify-content:center;margin-top:12px">
            <a class="btn" href="${linkFor({ limit: String(Math.min(limit + 100, 500)) })}">Show more</a>
          </div>` : punches.length >= 500 ? '<div class="hint" style="text-align:center;margin-top:10px">Showing the latest 500 — narrow the filters to see older ones.</div>' : ''}
        ` : empty(
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

  wireRunLogs(mount);

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
    const icon = button.innerHTML;
    button.addEventListener('click', () => {
      if (!armed) {
        // Armed: the muted icon turns into a plain "Confirm delete" button for
        // a few seconds, then goes back.
        button.textContent = 'Confirm delete';
        button.classList.remove('icon-btn');
        button.classList.add('sm', 'danger');
        armed = setTimeout(() => {
          armed = null;
          button.innerHTML = icon;
          button.classList.remove('sm', 'danger');
          button.classList.add('icon-btn');
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
