/* The staff console: every customer's account settings and sync cadence.
 *
 * Scheduling and configuration, plus counts and error text when an account is
 * stuck. Not the punch ledger: support can see *that* a customer is failing and
 * *why*, without reading who badged in when. The diagnostics endpoint scrubs
 * employee names out of Odoo's own error text, since Odoo writes them into the
 * message.
 */

import { api, auth } from '../api.js';
import {
  $, $$, banner, busy, empty, esc, field, fmtAgo, fmtIn, guard, loading, pill,
  readForm, toast,
} from '../ui.js';

const STATUSES = [
  { value: 'trialing', label: 'trialing' },
  { value: 'active', label: 'active' },
  { value: 'past_due', label: 'past due' },
  { value: 'suspended', label: 'suspended — stops syncing' },
  { value: 'cancelled', label: 'cancelled — stops syncing' },
];

const PAIRING = [
  { value: 'alternating', label: 'Alternating — in, out, in, out' },
  { value: 'state_based', label: 'State based — trust the device keys' },
  { value: 'first_last', label: 'First / last of the day' },
];

const ORPHAN = [
  { value: 'flag', label: 'Flag — zero-length record for review' },
  { value: 'create', label: 'Create — open a shift at that time' },
  { value: 'ignore', label: 'Ignore — drop it' },
];

/** Options for a plan <select>, including the "no plan" choice.
 *
 * Retired plans stay in the list (see GET /admin/plans) — a tenant already
 * on one still has to render its current selection, and pulling a plan from
 * this list is not how a plan gets retired anyway.
 */
function planOptions(plans) {
  return [
    { value: '', label: 'No plan — nothing enforced' },
    ...plans.map((p) => ({
      value: p.id,
      label: `${p.name}${p.is_active ? '' : ' (retired)'}`,
    })),
  ];
}

/** ISO datetime -> the plain YYYY-MM-DD a <input type=date> needs. */
function toDateInput(value) {
  return value ? String(value).slice(0, 10) : '';
}

export async function render(mount, route) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner(
      'Not available',
      'This section is for platform staff. Your account does not have that access.',
      'warn'
    );
    return;
  }

  const query = route.query.q || '';
  const openId = route.query.open || '';
  mount.innerHTML = loading();

  const [health, tenants, plans] = await Promise.all([
    api.get('/admin/scheduler').catch(() => null),
    api.get(`/admin/tenants${query ? `?q=${encodeURIComponent(query)}` : ''}`),
    api.get('/admin/plans').catch(() => []),
  ]);
  const defaultPlanId = (plans.find((p) => p.is_default) || {}).id || '';

  const linkFor = (overrides) => {
    const q = new URLSearchParams();
    Object.entries({ q: query, open: openId, ...overrides })
      .forEach(([k, v]) => { if (v) q.set(k, v); });
    const s = q.toString();
    return `#/platform${s ? `?${s}` : ''}`;
  };

  // If the scheduler is down then *every* customer has stopped, whatever their
  // interval says, and editing one number will not start anything.
  const schedulerDown = health && !health.running;

  mount.innerHTML = `
    ${schedulerDown ? banner(
      'The scheduler is not running',
      'No customer is syncing automatically right now, whatever their interval '
      + 'says. Changing a setting here will not start anything until it is back.',
      'bad') : ''}

    <div class="card" style="margin-bottom:14px">
      <h2>Platform <span class="hint">${health
        ? `${esc(health.tenants_scheduled)} of ${esc(health.tenants_total)} account(s) scheduled`
        : ''}</span></h2>
      ${health ? `
        <table>
          <tbody>
            <tr><td>Scheduler</td><td style="text-align:right">
              ${pill(health.running ? 'connected' : 'failed',
                     health.running ? 'running' : 'not running')}</td></tr>
            <tr><td>Mode</td><td style="text-align:right">${esc(health.mode || '—')}</td></tr>
            <tr><td>Host</td><td style="text-align:right" class="mono">${esc(health.owner || '—')}</td></tr>
            <tr><td>Last tick</td><td style="text-align:right">${esc(fmtAgo(health.last_tick_at))}</td></tr>
          </tbody>
        </table>` : '<div class="hint">Could not read the scheduler state.</div>'}
    </div>

    <div class="card" style="margin-bottom:14px">
      <h2>New account <span class="hint">onboard a customer yourself</span></h2>
      <form id="newTenant">
        <div class="grid cols-2">
          ${field({ name: 'company_name', label: 'Company', required: true,
                    placeholder: 'Muscat Traders' })}
          ${field({ name: 'owner_email', label: 'Owner email', type: 'email',
                    required: true, placeholder: 'boss@muscat.com' })}
          ${field({ name: 'timezone', label: 'Timezone', required: true,
                    value: 'Asia/Dubai',
                    help: 'Used to render their attendance. Not the device zone.' })}
          ${field({ name: 'sync_interval_minutes', label: 'Sync every (minutes)',
                    type: 'number', required: true, value: 15 })}
          ${field({ name: 'plan_id', label: 'Plan', value: defaultPlanId,
                    options: planOptions(plans),
                    help: 'Sets the initial employee cap and sync-interval floor. '
                        + 'The renewal date starts as a standard trial from today — '
                        + 'change it from the account row afterward.' })}
        </div>
        <div class="row" style="margin-top:4px">
          <button class="primary" id="createTenant">Create account</button>
          <span class="hint">A password is generated and shown once — nothing
            stores it in the clear.</span>
        </div>
      </form>
      <div id="createdBox"></div>
    </div>

    <div class="card">
      <h2>Accounts <span class="hint">${esc(tenants.length)} total</span></h2>
      <form id="search" class="row" style="gap:10px;margin-bottom:12px">
        <input type="text" name="q" id="q" value="${esc(query)}"
               placeholder="Filter by name or slug" style="max-width:280px">
        <button class="primary" id="doSearch">Search</button>
        ${query ? '<a class="btn" href="#/platform">Clear</a>' : ''}
      </form>

      ${tenants.length ? `
        <div class="scroll">
          <table>
            <thead><tr>
              <th>Account</th><th>Status</th><th class="num">Every</th>
              <th>Next sync</th><th>Last sync</th><th>Automatic</th><th></th>
            </tr></thead>
            <tbody>
              ${tenants.map((t) => rowFor(t, t.id === openId, linkFor, plans)).join('')}
            </tbody>
          </table>
        </div>
        <div class="hint" style="margin-top:12px">
          A new interval counts from that account's <strong>last</strong> sync, not
          from now — so shortening it can make a customer due immediately. A plan's
          employee cap only holds back <em>new</em> matches — it never unmaps anyone
          already relying on one. Every change here is written into that customer's
          own audit trail under your name.
        </div>
      ` : empty('No accounts match', query ? 'Try a different search.' : '')}
    </div>`;

  wire(mount, route, tenants, linkFor);
}

/* The subscription gate: stop and restart one account's syncing.
 *
 * Its own control rather than the Status field in the configuration form below,
 * because this is the action taken when a subscription lapses or ends and it
 * should take one click from the list, not a form dive. It changes `status`,
 * never `sync_enabled` — that switch belongs to the customer, and moving it
 * would both look to them like they did it and silently switch syncing back on
 * for someone who had chosen to have it off.
 *
 * Two steps to stop, one to restart, and no confirm() dialog anywhere: a modal
 * blocks the whole page and nothing else in this app uses one. The second click
 * is also where the reason gets typed, so recording one costs nothing extra.
 */
function gateControl(t) {
  if (t.syncable === false) {
    return `
      <button class="sm gate-start">Activate</button>
      <div class="hint">stopped${t.suspended_at ? ` ${esc(fmtAgo(t.suspended_at))}` : ''}${
        t.suspension_reason ? `: ${esc(t.suspension_reason)}` : ''}</div>`;
  }
  return '<button class="sm gate-stop">Deactivate</button>';
}

/** The second step: a reason box and the button that means it. */
function gateConfirm() {
  return `
    <input class="gate-reason" maxlength="200" placeholder="Reason (optional)"
           style="width:150px" aria-label="Why this account is being stopped">
    <button class="sm gate-commit">Stop syncing</button>
    <button class="link sm gate-cancel" type="button">Cancel</button>`;
}

function rowFor(t, open, linkFor, plans) {
  const connected = t.odoo_connected && t.source_connected;
  return `
    <tr data-tenant="${esc(t.id)}">
      <td>
        <strong>${esc(t.name)}</strong>
        <div class="hint mono">${esc(t.slug)} · ${esc(t.timezone)} ·
          ${esc(t.users)} user${t.users === 1 ? '' : 's'}</div>
        <div class="hint">${t.plan_name ? esc(t.plan_name) : 'no plan'}${
          t.subscription_renews_at ? ` · renews ${esc(fmtIn(t.subscription_renews_at))}` : ''
        }</div>
        ${t.pending_plan_name ? `<div class="hint">→ ${esc(t.pending_plan_name)} queued</div>` : ''}
        ${t.renewal_warning ? `<div class="hint strong">${
          t.renewal_warning.urgent ? 'renewing very soon' : 'renewing soon'}</div>` : ''}
        ${connected ? '' : `<div class="hint strong">${
          !t.odoo_connected && !t.source_connected ? 'nothing connected'
            : !t.odoo_connected ? 'no Odoo connection' : 'no device platform'}</div>`}
      </td>
      <td>${pill(t.status)}</td>
      <td class="num" style="white-space:nowrap">
        <input type="number" min="1" max="1440" class="mins"
               value="${esc(t.sync_interval_minutes)}"
               style="width:74px;text-align:right" aria-label="Minutes between syncs">
        <span class="hint">min</span>
        ${t.interval_widened ? `<div class="hint strong">
          backed off to ${esc(t.effective_interval_minutes)} after
          ${esc(t.consecutive_failures)} failures</div>` : ''}
      </td>
      <td>${t.next_run_at ? esc(fmtIn(t.next_run_at))
            : '<span class="hint">not scheduled</span>'}</td>
      <td>${t.last_run_at
            ? `${pill(t.last_run_status)} <span class="hint">${esc(fmtAgo(t.last_run_at))}</span>`
            : '<span class="hint">never</span>'}</td>
      <td>
        <select class="enabled" aria-label="Automatic sync">
          <option value="true"${t.sync_enabled ? ' selected' : ''}>on</option>
          <option value="false"${t.sync_enabled ? '' : ' selected'}>off</option>
        </select>
      </td>
      <td style="text-align:right;white-space:nowrap">
        <button class="sm save">Save</button>
        <a class="link sm" href="${linkFor({ open: open ? '' : t.id })}"
           >${open ? 'Close' : 'Configure'}</a>
        <div class="gate" style="margin-top:6px">${gateControl(t)}</div>
      </td>
    </tr>
    ${open ? `
      <tr class="detail-row"><td colspan="7">
        <form class="config-form">
          <div class="grid cols-2">
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">Account</h3>
              ${field({ name: 'name', label: 'Company', value: t.name, required: true })}
              ${field({ name: 'status', label: 'Status', value: t.status, required: true,
                        options: STATUSES,
                        help: 'Suspended and cancelled stop this account syncing, '
                            + 'whatever its own settings say.' })}
              ${field({ name: 'timezone', label: 'Display timezone', value: t.timezone,
                        required: true })}
              ${field({ name: 'work_start_time', label: 'Work starts',
                        value: t.work_start_time, required: true, placeholder: '09:00' })}
              ${field({ name: 'late_grace_minutes', label: 'Grace (minutes)',
                        type: 'number', value: t.late_grace_minutes, required: true })}
            </div>
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">Pairing</h3>
              ${field({ name: 'pairing_mode', label: 'Mode', value: t.pairing_mode,
                        required: true, options: PAIRING, strongHelp: true,
                        help: 'Changes how punches become shifts from the next run. '
                            + 'Existing records are left alone.' })}
              ${field({ name: 'min_punch_interval_seconds',
                        label: 'Ignore repeats within (seconds)', type: 'number',
                        value: t.min_punch_interval_seconds, required: true })}
              ${field({ name: 'max_shift_hours', label: 'Maximum shift (hours)',
                        type: 'number', value: t.max_shift_hours, required: true })}
              ${field({ name: 'day_boundary_hour', label: 'Shift day starts at (hour)',
                        type: 'number', value: t.day_boundary_hour, required: true })}
              ${field({ name: 'orphan_out_policy', label: 'Check-out with no check-in',
                        value: t.orphan_out_policy, required: true, options: ORPHAN })}
            </div>
          </div>
          <div class="grid cols-2" style="margin-top:2px">
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">Subscription</h3>
              ${field({ name: 'plan_id', label: 'Plan', value: t.plan_id || '',
                        options: planOptions(plans),
                        help: 'Caps this account’s mapped-employee count and how '
                            + 'fast they can set their own sync interval. Lowering it '
                            + 'never unmaps anyone already matched — only new badges '
                            + 'are held back.' })}
            </div>
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">&nbsp;</h3>
              ${field({ name: 'subscription_renews_at', label: 'Renews / paid through',
                        type: 'date', value: toDateInput(t.subscription_renews_at),
                        help: 'Past this date, an active account moves itself to past '
                            + 'due and stops syncing — no grace period. Leave empty to '
                            + 'exempt this account from that automatic check entirely.' })}
            </div>
          </div>
          <div class="row" style="margin-top:6px">
            <button class="primary save-config">Save configuration</button>
            <button class="sync-now" type="button">Sync now</button>
            <button class="diagnose" type="button">Why is it stuck?</button>
            ${t.interval_widened
              ? '<button class="clear-failures" type="button">Clear failures</button>' : ''}
          </div>
        </form>
        <div class="diag"></div>
      </td></tr>` : ''}`;
}

function diagnosticsHtml(d) {
  const nothing = !d.punches_error && !d.punches_unmapped && !d.punches_pending
    && !d.unmapped_badges;
  if (nothing) {
    return `<div class="banner" style="margin:12px 0 0">
      <strong>Nothing is stuck</strong>
      No punches are pending, unmapped or in error for ${esc(d.name)}.</div>`;
  }
  return `
    <div class="card" style="margin:12px 0 0">
      <h2>Why ${esc(d.name)} is stuck <span class="hint">counts and error text
        only — no punch times, badges or employee names</span></h2>
      <table>
        <tbody>
          <tr><td>Pending</td><td class="num" style="text-align:right">${esc(d.punches_pending)}</td></tr>
          <tr><td>In error</td><td class="num" style="text-align:right">${esc(d.punches_error)}</td></tr>
          <tr><td>At the 5-attempt cap <span class="hint">never retried again
            until reset</span></td>
            <td class="num" style="text-align:right">${esc(d.punches_at_attempt_cap)}</td></tr>
          <tr><td>Unmapped punches</td><td class="num" style="text-align:right">${esc(d.punches_unmapped)}</td></tr>
          <tr><td>Badges with no Odoo employee</td><td class="num" style="text-align:right">${esc(d.unmapped_badges)}</td></tr>
        </tbody>
      </table>
      ${d.last_run_error ? `<div class="banner bad" style="margin-top:12px">
        <strong>Last run failed</strong>${esc(d.last_run_error)}</div>` : ''}
      ${d.errors.length ? `
        <h3 style="margin:14px 0 6px;font-size:13px">Distinct errors</h3>
        ${d.errors.map((e) => `
          <div class="banner bad" style="margin-bottom:8px">
            <strong>${esc(e.count)} punch${e.count === 1 ? '' : 'es'}</strong>
            ${esc(e.message)}</div>`).join('')}
        ${d.redacted ? `<div class="hint">Employee names are replaced with
          &lt;employee&gt; — Odoo writes them into its own error text.</div>` : ''}
      ` : ''}
    </div>`;
}

function wire(mount, route, tenants, linkFor) {
  $('#search', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('#q', mount).value.trim();
    window.location.hash = linkFor({ q: value, open: '' });
  });

  // --- create -------------------------------------------------------------
  $('#newTenant', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    // readForm yields '' for "No plan", the select's own empty-string
    // option — the API expects null for "assign nothing".
    if (values.plan_id === '') values.plan_id = null;
    busy($('#createTenant', mount), async () => {
      const result = await guard(() => api.post('/admin/tenants', values));
      if (!result) return;
      // Rendered rather than toasted: a password in a toast is gone in five
      // seconds, and this one cannot be retrieved afterwards.
      $('#createdBox', mount).innerHTML = `
        <div class="banner" style="margin-top:12px">
          <strong>${esc(result.tenant.name)} created</strong>
          Owner <span class="mono">${esc(result.owner_email)}</span>, password
          <span class="mono" style="user-select:all">${esc(result.owner_password)}</span>
          <div class="hint" style="margin-top:6px">${esc(result.note)}</div>
        </div>`;
      toast(`${result.tenant.name} created`, 'ok');
    });
  });

  // --- per row ------------------------------------------------------------
  $$('tr[data-tenant]', mount).forEach((row) => {
    const id = row.dataset.tenant;
    const detail = row.nextElementSibling?.classList.contains('detail-row')
      ? row.nextElementSibling : null;

    const refresh = async (message) => {
      await render(mount, route);
      if (message) toast(message, 'ok');
    };

    $('.save', row).addEventListener('click', () =>
      busy($('.save', row), async () => {
        const result = await guard(() => api.patch(`/admin/tenants/${id}/schedule`, {
          sync_interval_minutes: Number($('.mins', row).value),
          sync_enabled: $('.enabled', row).value === 'true',
        }));
        if (result) {
          await refresh(`${result.name}: every ${result.effective_interval_minutes} min`
            + (result.next_run_at ? `, next sync ${fmtIn(result.next_run_at)}`
                                  : ', automatic sync off'));
        }
      })
    );

    // --- the subscription gate ---------------------------------------------
    const gate = $('.gate', row);

    const wireGate = () => {
      const stop = $('.gate-stop', gate);
      if (stop) {
        stop.addEventListener('click', () => {
          gate.innerHTML = gateConfirm();
          $('.gate-reason', gate).focus();
          wireGate();
        });
      }

      const cancel = $('.gate-cancel', gate);
      if (cancel) {
        cancel.addEventListener('click', () => {
          // Back to the button, from the row's own data rather than a refetch:
          // cancelling changed nothing, so a round trip would be for show.
          gate.innerHTML = gateControl(tenants.find((x) => x.id === id) || {});
          wireGate();
        });
      }

      const commit = $('.gate-commit', gate);
      if (commit) {
        commit.addEventListener('click', () =>
          busy(commit, async () => {
            const reason = $('.gate-reason', gate).value.trim();
            const result = await guard(() => api.post(
              `/admin/tenants/${id}/deactivate`, { reason: reason || null }
            ));
            if (result) await refresh(`${result.name}: syncing stopped`);
          })
        );
      }

      const start = $('.gate-start', gate);
      if (start) {
        start.addEventListener('click', () =>
          busy(start, async () => {
            const result = await guard(() => api.post(`/admin/tenants/${id}/activate`));
            if (result) await refresh(`${result.name}: syncing restored`);
          })
        );
      }
    };
    wireGate();

    if (!detail) return;

    const form = $('.config-form', detail);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const values = readForm(event.target);
      // Both controls yield '' when cleared — the select's "No plan" option
      // and an emptied date input. The API takes null for "unassign" / "no
      // renewal date to watch", not an empty string.
      if (values.plan_id === '') values.plan_id = null;
      if (values.subscription_renews_at === '') values.subscription_renews_at = null;
      busy($('.save-config', detail), async () => {
        const result = await guard(() => api.patch(`/admin/tenants/${id}/config`, values));
        if (result) await refresh(`${result.name} updated`);
      });
    });

    $('.sync-now', detail).addEventListener('click', () =>
      busy($('.sync-now', detail), async () => {
        const run = await guard(() => api.post(`/admin/tenants/${id}/sync`));
        if (run) {
          await refresh(`${run.status} — ${run.punches_new} new punch(es), `
            + `${run.attendances_created} created, ${run.error_count} error(s)`);
        }
      })
    );

    $('.diagnose', detail).addEventListener('click', () =>
      busy($('.diagnose', detail), async () => {
        const d = await guard(() => api.get(`/admin/tenants/${id}/diagnostics`));
        if (d) $('.diag', detail).innerHTML = diagnosticsHtml(d);
      })
    );

    const clear = $('.clear-failures', detail);
    if (clear) {
      clear.addEventListener('click', () =>
        busy(clear, async () => {
          const result = await guard(() => api.post(`/admin/tenants/${id}/schedule/reset`));
          if (result) await refresh(result.message);
        })
      );
    }
  });
}
