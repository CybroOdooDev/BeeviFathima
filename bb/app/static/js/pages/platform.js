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
  readForm, timezoneNames, toast,
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

/** What the plan itself says about one limit, for the override fields. */
function planLimitText(plan, key, unit) {
  if (!plan) return 'No plan: unlimited';
  const v = plan[key];
  return v == null ? `${plan.name}: unlimited` : `${plan.name}: ${v}${unit}`;
}

/** One usage tile: "18 / 25 employees", tinted near or over the limit. */
function usageTile(used, limit, label) {
  const ratio = limit ? used / limit : 0;
  const cls = limit && used > limit ? ' over' : limit && ratio >= 0.8 ? ' near' : '';
  return `<div class="u${cls}"><b>${esc(used)}${limit != null ? ` / ${esc(limit)}` : ''}</b>
    <span>${esc(label)}${limit == null ? ' · no limit' : ''}</span></div>`;
}

function usageHtml(u) {
  if (!u) return '<div class="usage-strip"><div class="u"><span>Loading usage…</span></div></div>';
  return `<div class="usage-strip">
    ${usageTile(u.employees_mapped, u.max_employees, 'employees matched')}
    ${usageTile(u.devices, u.max_devices, 'devices')}
    <div class="u${u.punches_held ? ' over' : ''}"><b>${esc(u.punches_held)}</b>
      <span>punches held${u.devices_over_limit ? ` · ${esc(u.devices_over_limit)} device(s) over` : ''}</span></div>
    <div class="u"><b>${esc(u.sync_interval_minutes)} min</b>
      <span>sync${u.min_sync_interval_minutes ? ` · fastest ${esc(u.min_sync_interval_minutes)} min` : ' · any speed'}</span></div>
  </div>`;
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
  const linkFor = (overrides) => {
    const q = new URLSearchParams();
    Object.entries({ q: query, ...overrides })
      .forEach(([k, v]) => { if (v) q.set(k, v); });
    const s = q.toString();
    return `#/platform${s ? `?${s}` : ''}`;
  };

  // #/platform?open=<id> — how Overview's counts and the account list link
  // straight to one account (see console.js). Read once on arrival to open
  // its Configure dialog below, then dropped from the address bar and from
  // `route` itself, so neither a reload nor a background refresh triggered
  // from inside that dialog (Save, Sync now, Clear failures) reopens it again.
  const openId = route.query.open || '';
  if (openId) {
    delete route.query.open;
    history.replaceState(null, '', linkFor({}));
  }

  mount.innerHTML = loading();

  const [health, tenants, plans] = await Promise.all([
    api.get('/admin/scheduler').catch(() => null),
    api.get(`/admin/tenants${query ? `?q=${encodeURIComponent(query)}` : ''}`),
    api.get('/admin/plans').catch(() => []),
  ]);

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

    <div class="card">
      <div class="card-head" style="position:static">
        <h2>Accounts <span class="hint">${esc(tenants.length)} total</span></h2>
        <div class="actions"><button class="primary sm" id="addAccount">Add account</button></div>
      </div>
      <form id="search" class="row" style="gap:10px;margin-bottom:12px">
        <input type="text" name="q" id="q" value="${esc(query)}"
               placeholder="Filter by name or slug" style="max-width:280px">
        <button class="primary" id="doSearch">Search</button>
        ${query ? '<a class="btn" href="#/platform">Clear</a>' : ''}
      </form>

      ${tenants.length ? `
        <div class="scroll">
          <table class="accounts-table">
            <thead><tr>
              <th>Account</th><th>Status</th><th class="num">Every</th>
              <th>Sync</th><th>Automatic</th><th></th>
            </tr></thead>
            <tbody>
              ${tenants.map((t) => rowFor(t)).join('')}
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

  wire(mount, route, tenants, plans, linkFor);

  if (openId) {
    const tenant = tenants.find((x) => x.id === openId);
    if (tenant) openConfigDialog(tenant, { plans, onChange: () => render(mount, route) });
  }
}

/* The subscription gate: stop and restart one account's syncing.
 *
 * Its own control rather than the Status field in the configuration dialog
 * below, because this is the action taken when a subscription lapses or ends
 * and it should take one click from the list, not a form dive. It changes
 * `status`, never `sync_enabled` — that switch belongs to the customer, and
 * moving it would both look to them like they did it and silently switch
 * syncing back on for someone who had chosen to have it off.
 *
 * Two steps to stop, one to restart, and no confirm() dialog anywhere: a modal
 * blocks the whole page and the only other one in this app (the add-connection
 * and account-configuration wizards) is reserved for a deliberate, multi-field
 * action, not a one-word confirmation. The second click is also where the
 * reason gets typed, so recording one costs nothing extra.
 */
function gateControl(t) {
  if (t.syncable === false) return '<button class="sm gate-start">Activate</button>';
  return '<button class="sm danger-outline gate-stop">Deactivate</button>';
}

/** The second step: a reason box and the button that means it. */
function gateConfirm() {
  return `
    <input class="gate-reason" maxlength="200" placeholder="Reason (optional)"
           aria-label="Why this account is being stopped">
    <button class="sm danger gate-commit">Stop syncing</button>
    <button class="link sm gate-cancel" type="button">Cancel</button>`;
}

/* The back-off, as a small tag beside the interval instead of a red sentence
 * under it. The tag carries the interval the scheduler is actually using now
 * — it comes from the server on every refresh, so it moves on its own as the
 * back-off grows or clears — and the why is in its hover label. The box
 * beside it stays the configured value, which is what Save writes. */
function backoffTag(t) {
  const label = `Backed off: syncing every ${t.effective_interval_minutes} min instead of `
    + `${t.sync_interval_minutes} after ${t.consecutive_failures} failed syncs in a row. `
    + `It returns to ${t.sync_interval_minutes} min by itself after the next successful sync `
    + '(or use Clear failures under Configure).';
  return `<span class="tip-tag warn" tabindex="0" role="note" aria-label="${esc(label)}" data-tip="${esc(label)}">
      now ${esc(t.effective_interval_minutes)} min</span>`;
}

function rowFor(t) {
  return `
    <tr data-tenant="${esc(t.id)}">
      <td>
        <strong>${esc(t.name)}</strong>
      </td>
      <td>${pill(t.status)}</td>
      <td class="num" style="white-space:nowrap">
        <input type="number" min="1" max="1440" class="mins"
               value="${esc(t.sync_interval_minutes)}"
               style="width:74px;text-align:right" aria-label="Minutes between syncs">
        <span class="hint">min</span>
        ${t.interval_widened ? backoffTag(t) : ''}
      </td>
      <td class="sync-cell">
        <div>${t.next_run_at ? `next ${esc(fmtIn(t.next_run_at))}`
              : '<span class="hint">not scheduled</span>'}</div>
        <div class="hint">${t.last_run_at
              ? `last ${pill(t.last_run_status)} ${esc(fmtAgo(t.last_run_at))}`
              : 'never synced'}</div>
      </td>
      <td>
        <select class="enabled" aria-label="Automatic sync">
          <option value="true"${t.sync_enabled ? ' selected' : ''}>on</option>
          <option value="false"${t.sync_enabled ? '' : ' selected'}>off</option>
        </select>
      </td>
      <td class="actions-cell">
        <div class="row-actions">
          <button class="sm save">Save</button>
          <button type="button" class="sm configure-btn">Configure</button>
          <span class="gate">${gateControl(t)}</span>
          ${['suspended', 'cancelled'].includes(t.status)
            ? '<button type="button" class="sm danger-outline delete-tenant" title="Delete this deactivated account and all its data">Delete</button>' : ''}
        </div>
      </td>
    </tr>`;
}

/* The account's facts, at the top of Configure — what used to be stacked
 * under each name in the account list. Label / value pairs, with anything
 * that needs a person (stopped, renewing soon, nothing connected) tinted. */
function accountInfoHtml(t) {
  const setup = t.odoo_connected && t.source_connected ? 'Odoo and a biometric connection'
    : !t.odoo_connected && !t.source_connected ? 'Nothing connected'
      : !t.odoo_connected ? 'No Odoo connection' : 'No biometric connection';
  const custom = [t.limit_max_employees, t.limit_max_devices, t.limit_min_sync_interval_minutes]
    .some((v) => v != null);
  const items = [
    ['Slug', `<span class="mono">${esc(t.slug)}</span>`],
    ['Timezone', esc(t.timezone)],
    ['Users', esc(t.users)],
    ['Plan', `${t.plan_name ? esc(t.plan_name) : 'No plan'}${custom ? ' · custom limits' : ''}${
      t.pending_plan_name ? ` → ${esc(t.pending_plan_name)} queued` : ''}`],
    ['Renews', t.subscription_renews_at
      ? `${esc(fmtIn(t.subscription_renews_at))}${t.renewal_warning
        ? ` <span class="pill warn">${t.renewal_warning.urgent ? 'very soon' : 'soon'}</span>` : ''}`
      : 'No renewal date', ],
    ['Setup', setup, !(t.odoo_connected && t.source_connected)],
  ];
  if (t.syncable === false) {
    items.push(['Stopped', `${t.suspended_at ? esc(fmtAgo(t.suspended_at)) : 'yes'}${
      t.suspension_reason ? ` — ${esc(t.suspension_reason)}` : ''}`, true]);
  }
  return `<dl class="account-info">${items.map(([k, v, warn]) =>
    `<div${warn ? ' class="warn"' : ''}><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>`;
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

/* ===========================================================================
 * The account configuration dialog: a modal <dialog>, opened from a row's
 * Configure button, or from the #/platform?open=<id> deep link that
 * Overview's numbers and the account list use (see render() above).
 *
 * Unlike the add-connection wizard in settings.js this is one screen, not a
 * sequence — configuring an account usually means looking at several of its
 * settings together (the plan next to its renewal date, say), not being
 * walked through them one field group at a time.
 *
 * Lives on <body>, outside the account table, the same way the add-connection
 * wizard does — so refreshing that table in the background after Save, Sync
 * now or Clear failures never yanks the dialog out from under whoever is
 * using it. `onChange` is how it asks for that refresh; the dialog never
 * re-fetches or repaints the table itself, only its own contents.
 * ======================================================================== */
function openConfigDialog(tenant, { plans, onChange }) {
  document.querySelector('dialog.config-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard config-dialog';
  dialog.setAttribute('aria-labelledby', 'configTitle');
  document.body.append(dialog);

  // The tenant and its diagnostics, if fetched — local to this dialog so a
  // Save / Sync now / Clear failures updates what's on screen here right
  // away, without waiting on (or depending on) the table refresh it also
  // kicks off underneath via onChange.
  let t = tenant;
  let diag = null;
  let usage = null;
  const planById = (id) => plans.find((p) => p.id === id) || null;

  async function loadUsage() {
    usage = await api.get(`/admin/tenants/${t.id}/usage`).catch(() => null);
    const slot = $('.usage-slot', dialog);
    if (slot) slot.innerHTML = usageHtml(usage);
  }

  function close() {
    dialog.close();
    dialog.remove();
  }

  function paint() {
    dialog.innerHTML = `
      <div class="wiz-head">
        <strong id="configTitle">${esc(t.name)}</strong>
        <button type="button" class="link wiz-x" data-cfg="close" aria-label="Close">&times;</button>
      </div>
      <div class="wiz-body">
        ${accountInfoHtml(t)}
        <form id="cfgForm">
          <div class="grid cols-2">
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">Account</h3>
              ${field({ name: 'name', label: 'Company', value: t.name, required: true })}
              ${field({ name: 'status', label: 'Status', value: t.status, required: true,
                        options: STATUSES,
                        help: 'Suspended and cancelled stop this account syncing, '
                            + 'whatever its own settings say.' })}
              ${field({ name: 'timezone', label: 'Display Timezone', value: t.timezone,
                        required: true, datalist: timezoneNames() })}
              ${field({ name: 'work_start_time', label: 'Work Starts',
                        value: t.work_start_time, required: true, placeholder: '09:00' })}
              ${field({ name: 'late_grace_minutes', label: 'Grace (Minutes)',
                        type: 'number', value: t.late_grace_minutes, required: true })}
            </div>
            <div>
              <h3 style="margin:0 0 10px;font-size:13px">Pairing</h3>
              ${field({ name: 'pairing_mode', label: 'Mode', value: t.pairing_mode,
                        required: true, options: PAIRING, strongHelp: true,
                        help: 'Changes how punches become shifts from the next run. '
                            + 'Existing records are left alone.' })}
              ${field({ name: 'min_punch_interval_seconds',
                        label: 'Ignore Repeats Within (Seconds)', type: 'number',
                        value: t.min_punch_interval_seconds, required: true })}
              ${field({ name: 'max_shift_hours', label: 'Maximum Shift (Hours)',
                        type: 'number', value: t.max_shift_hours, required: true })}
              ${field({ name: 'day_boundary_hour', label: 'Shift Day Starts At (Hour)',
                        type: 'number', value: t.day_boundary_hour, required: true })}
              ${field({ name: 'orphan_out_policy', label: 'Check-Out With No Check-In',
                        value: t.orphan_out_policy, required: true, options: ORPHAN })}
            </div>
          </div>
          <h3 style="margin:6px 0 10px;font-size:13px">Subscription</h3>
          <div class="usage-slot">${usageHtml(usage)}</div>
          <div class="grid cols-2">
            ${field({ name: 'plan_id', label: 'Plan', value: t.plan_id || '',
                      options: planOptions(plans),
                      help: 'Sets this account’s employee, device and sync-speed limits. Assigning a '
                          + 'plan raises a faster sync interval to the plan’s floor. Lowering a plan '
                          + 'never unmaps anyone already matched — only new badges wait.' })}
            ${field({ name: 'subscription_renews_at', label: 'Renews / Paid Through',
                      type: 'date', value: toDateInput(t.subscription_renews_at),
                      help: 'Past this date, an active account moves itself to past '
                          + 'due and stops syncing — no grace period. Leave empty to '
                          + 'exempt this account from that automatic check entirely.' })}
          </div>
          <h3 style="margin:6px 0 4px;font-size:13px">Limits for this account
            <span class="hint">empty = use the plan · 0 = no limit</span></h3>
          <div class="grid cols-3 limit-fields">
            ${field({ tip: true, name: 'limit_max_employees', label: 'Employees', type: 'number',
                      value: t.limit_max_employees ?? '', placeholder: planLimitText(planById(t.plan_id), 'max_employees', ''),
                      help: 'An exception to the plan for this customer only, e.g. a custom deal. '
                          + 'Stays in place if the plan changes — clear it to go back to the plan.' })}
            ${field({ tip: true, name: 'limit_max_devices', label: 'Devices', type: 'number',
                      value: t.limit_max_devices ?? '', placeholder: planLimitText(planById(t.plan_id), 'max_devices', ''),
                      help: 'Raising it releases held punches from the newly covered terminals on the next sync.' })}
            ${field({ tip: true, name: 'limit_min_sync_interval_minutes', label: 'Fastest Sync (Min)', type: 'number',
                      value: t.limit_min_sync_interval_minutes ?? '',
                      placeholder: planLimitText(planById(t.plan_id), 'min_sync_interval_minutes', ' min'),
                      help: 'The fastest interval the customer can pick themselves.' })}
          </div>
        </form>
        <div class="diag">${diag ? diagnosticsHtml(diag) : ''}</div>
      </div>
      <div class="wiz-foot">
        <button type="button" class="link" data-cfg="close">Close</button>
        <div class="actions">
          <button type="button" class="diagnose">Why is it stuck?</button>
          ${t.interval_widened ? '<button type="button" class="clear-failures">Clear failures</button>' : ''}
          <button type="button" class="sync-now">Sync now</button>
          <button type="submit" form="cfgForm" class="primary save-config">Save configuration</button>
        </div>
      </div>`;
    wireInner();
  }

  function wireInner() {
    dialog.querySelectorAll('[data-cfg=close]').forEach((b) => b.addEventListener('click', close));

    // The override boxes show what the plan gives, so picking a plan
    // previews its limits before anything is saved.
    const planSelect = $('#plan_id', dialog);
    planSelect?.addEventListener('change', () => {
      const plan = planById(planSelect.value);
      $('#limit_max_employees', dialog).placeholder = planLimitText(plan, 'max_employees', '');
      $('#limit_max_devices', dialog).placeholder = planLimitText(plan, 'max_devices', '');
      $('#limit_min_sync_interval_minutes', dialog).placeholder = planLimitText(plan, 'min_sync_interval_minutes', ' min');
    });

    $('#cfgForm', dialog).addEventListener('submit', (event) => {
      event.preventDefault();
      const values = readForm(event.target);
      // Both controls yield '' when cleared — the select's "No plan" option
      // and an emptied date input. The API takes null for "unassign" / "no
      // renewal date to watch", not an empty string.
      if (values.plan_id === '') values.plan_id = null;
      if (values.subscription_renews_at === '') values.subscription_renews_at = null;
      busy($('.save-config', dialog), async () => {
        const result = await guard(() => api.patch(`/admin/tenants/${t.id}/config`, values));
        if (!result) return;
        const raised = result.sync_interval_minutes !== t.sync_interval_minutes;
        t = result;
        paint();
        loadUsage();
        toast(`${result.name} updated${raised ? ` — now syncs every ${result.sync_interval_minutes} min to fit its plan` : ''}`, 'ok');
        onChange();
      });
    });

    $('.sync-now', dialog).addEventListener('click', () =>
      busy($('.sync-now', dialog), async () => {
        const run = await guard(() => api.post(`/admin/tenants/${t.id}/sync`));
        if (!run) return;
        t = await api.get(`/admin/tenants/${t.id}`).catch(() => t);
        paint();
        toast(`${run.status} — ${run.punches_new} new punch(es), `
          + `${run.attendances_created} created, ${run.error_count} error(s)`, 'ok');
        onChange();
      })
    );

    $('.diagnose', dialog).addEventListener('click', () =>
      busy($('.diagnose', dialog), async () => {
        const d = await guard(() => api.get(`/admin/tenants/${t.id}/diagnostics`));
        if (d) { diag = d; paint(); }
      })
    );

    const clearBtn = $('.clear-failures', dialog);
    if (clearBtn) {
      clearBtn.addEventListener('click', () =>
        busy(clearBtn, async () => {
          const result = await guard(() => api.post(`/admin/tenants/${t.id}/schedule/reset`));
          if (!result) return;
          t = await api.get(`/admin/tenants/${t.id}`).catch(() => t);
          paint();
          toast(result.message, 'ok');
          onChange();
        })
      );
    }
  }

  // Esc and the backdrop both mean "close" — everything up to the last Save
  // is already persisted, unlike the add-connection wizard, so there is
  // nothing to lose by dismissing it this way.
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) close();
  });

  paint();
  dialog.showModal();
  loadUsage();
  return dialog;
}

/* "Add account": staff onboarding a customer, in the same modal chrome as
 * Configure. On success the dialog turns into the one-time password — it is
 * shown, not toasted, because it cannot be retrieved afterwards — and the
 * account list behind refreshes when it is closed. */
function openNewAccountDialog(plans, onCreated) {
  document.querySelector('dialog.new-account-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard new-account-dialog';
  dialog.setAttribute('aria-labelledby', 'newAccountTitle');
  document.body.append(dialog);
  const defaultPlanId = (plans.find((p) => p.is_default) || {}).id || '';
  let created = false;

  const close = () => {
    dialog.close();
    dialog.remove();
    if (created) onCreated();
  };

  dialog.innerHTML = `
    <div class="wiz-head">
      <strong id="newAccountTitle">Add account</strong>
      <button type="button" class="link wiz-x" data-close aria-label="Close">&times;</button>
    </div>
    <div class="wiz-body">
      <form id="newTenant">
        <div class="grid cols-2">
          ${field({ name: 'company_name', label: 'Company', required: true,
                    placeholder: 'Muscat Traders' })}
          ${field({ name: 'owner_email', label: 'Owner Email', type: 'email',
                    required: true, placeholder: 'boss@muscat.com' })}
          ${field({ name: 'plan_id', label: 'Plan', value: defaultPlanId,
                    options: planOptions(plans), tip: true,
                    help: 'Sets the account’s employee, device and sync-speed limits — a faster '
                        + 'interval than the plan allows is raised to it. The renewal date starts '
                        + 'as a standard trial from today.' })}
          ${field({ name: 'sync_interval_minutes', label: 'Sync Every (Minutes)',
                    type: 'number', required: true, value: 15 })}
          ${field({ name: 'timezone', label: 'Timezone', required: true,
                    value: 'Asia/Dubai', datalist: timezoneNames(), tip: true,
                    help: 'Used to render their attendance. Not the device zone.' })}
        </div>
      </form>
      <div class="hint">A password is generated and shown once — nothing stores it in the clear.</div>
    </div>
    <div class="wiz-foot">
      <button type="button" class="link" data-close>Cancel</button>
      <div class="actions">
        <button type="submit" form="newTenant" class="primary" id="createTenant">Create account</button>
      </div>
    </div>`;

  const wireClose = () => $$('[data-close]', dialog).forEach((b) => b.addEventListener('click', close));
  wireClose();
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  dialog.addEventListener('click', (event) => { if (event.target === dialog) close(); });

  // The interval follows the plan's floor as the plan changes.
  const planSel = $('[name=plan_id]', dialog);
  const mins = $('[name=sync_interval_minutes]', dialog);
  const fitInterval = () => {
    const floor = (plans.find((p) => p.id === planSel.value) || {}).min_sync_interval_minutes;
    if (floor && Number(mins.value) < floor) mins.value = floor;
  };
  planSel.addEventListener('change', fitInterval);
  fitInterval();

  $('#newTenant', dialog).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    // readForm yields '' for "No plan" — the API expects null.
    if (values.plan_id === '') values.plan_id = null;
    busy($('#createTenant', dialog), async () => {
      const result = await guard(() => api.post('/admin/tenants', values));
      if (!result) return;
      created = true;
      $('.wiz-body', dialog).innerHTML = `
        <div class="banner" style="margin:0">
          <strong>${esc(result.tenant.name)} created</strong>
          Owner <span class="mono">${esc(result.owner_email)}</span>, password
          <span class="mono" style="user-select:all">${esc(result.owner_password)}</span>
          <div class="hint" style="margin-top:6px">${esc(result.note)}</div>
        </div>
        <div class="hint" style="margin-top:12px">Copy the password now — it is not shown again.</div>`;
      $('.wiz-foot', dialog).innerHTML = `
        <span></span>
        <div class="actions"><button type="button" class="primary" data-close>Done</button></div>`;
      wireClose();
      toast(`${result.tenant.name} created`, 'ok');
    });
  });

  dialog.showModal();
  $('#company_name', dialog).focus();
}

function wire(mount, route, tenants, plans, linkFor) {
  $('#search', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('#q', mount).value.trim();
    window.location.hash = linkFor({ q: value });
  });

  // --- create -------------------------------------------------------------
  $('#addAccount', mount).addEventListener('click', () =>
    openNewAccountDialog(plans, () => render(mount, route)));

  // --- per row ------------------------------------------------------------
  $$('tr[data-tenant]', mount).forEach((row) => {
    const id = row.dataset.tenant;

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

    $('.delete-tenant', row)?.addEventListener('click', () => {
      const tenant = tenants.find((x) => x.id === id);
      if (tenant) openDeleteTenantDialog(tenant, (message) => refresh(message));
    });

    $('.configure-btn', row).addEventListener('click', () => {
      const tenant = tenants.find((x) => x.id === id);
      if (tenant) openConfigDialog(tenant, { plans, onChange: () => render(mount, route) });
    });

    // --- the subscription gate ---------------------------------------------
    const gate = $('.gate', row);

    const wireGate = () => {
      const stop = $('.gate-stop', gate);
      if (stop) {
        stop.addEventListener('click', () => {
          // The confirm step takes the place of Save / Configure rather than
          // sitting beside them, so the row keeps its width and stays on one line.
          gate.closest('.row-actions')?.classList.add('confirming');
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
          gate.closest('.row-actions')?.classList.remove('confirming');
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
  });
}


/* Delete a deactivated account: a modal, the account name typed back, and an
 * optional reason that goes into Closed accounts. Only offered for suspended
 * or cancelled accounts — the server enforces the same. */
function openDeleteTenantDialog(t, onDone) {
  document.querySelector('dialog.delete-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard delete-dialog';
  dialog.style.width = 'min(520px, calc(100vw - 24px))';
  document.body.append(dialog);
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.innerHTML = `
    <div class="wiz-head"><strong>Delete ${esc(t.name)}</strong>
      <button type="button" class="link wiz-x" data-close aria-label="Close">&times;</button></div>
    <form id="delTenantForm" style="display:contents" novalidate>
      <div class="wiz-body">
        ${banner('This cannot be undone',
          'Every connection, device, employee mapping, punch, attendance record, user and audit entry of this account is deleted. '
          + 'A running Stripe subscription is cancelled immediately. Only a closure record (name, owner, reason) is kept.', 'bad')}
        <div class="field"><label for="delReason">Reason <span class="opt">optional</span></label>
          <textarea id="delReason" name="reason" rows="2" maxlength="1000" placeholder="e.g. unpaid since March, customer asked to close"></textarea></div>
        <div class="field"><label for="delName">Type <strong>${esc(t.name)}</strong> to confirm</label>
          <input id="delName" name="confirm_name" autocomplete="off"></div>
        <p class="err" id="delErr"></p>
      </div>
      <div class="wiz-foot">
        <button type="button" data-close>Cancel</button>
        <button type="submit" class="danger" id="delGo" disabled>Delete account</button>
      </div>
    </form>`;
  dialog.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', close));
  const nameInput = $('#delName', dialog);
  const go = $('#delGo', dialog);
  nameInput.addEventListener('input', () => {
    go.disabled = nameInput.value.trim().toLowerCase() !== t.name.trim().toLowerCase();
  });
  $('#delTenantForm', dialog).addEventListener('submit', async (event) => {
    event.preventDefault();
    go.disabled = true;
    try {
      const result = await api.post(`/admin/tenants/${t.id}/delete`, {
        confirm_name: nameInput.value, reason: $('#delReason', dialog).value || null,
      });
      close();
      onDone(result.message);
    } catch (exc) {
      $('#delErr', dialog).textContent = exc.message || 'Not deleted.';
      go.disabled = false;
    }
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
  nameInput.focus();
}
