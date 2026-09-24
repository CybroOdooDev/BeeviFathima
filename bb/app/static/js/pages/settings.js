/* Settings: tenant configuration, split into submenus.
 *
 * General / Pairing / Working hours / Plan are the account's own rules —
 * previously one long scrolling form, now one submenu each so a change to
 * pairing does not require scrolling past working hours to find it. Odoo and
 * Biometric absorb what used to be the standalone "Connections" page: where
 * attendance is written, and what it is written from.
 */

import { api, auth } from '../api.js';
import {
  $, banner, busy, empty, esc, field, fmtAgo, fmtIn, guard, loading, pill, readForm, timezoneNames, toast,
} from '../ui.js';

const SECTIONS = [
  { slug: 'general', title: 'General' },
  { slug: 'pairing', title: 'Pairing' },
  { slug: 'hours', title: 'Working hours' },
  { slug: 'plan', title: 'Plan' },
  { slug: 'odoo', title: 'Odoo' },
  { slug: 'biometric', title: 'Biometric' },
];

const RENDERERS = {
  general: renderGeneral,
  pairing: renderPairing,
  hours: renderHours,
  plan: renderPlan,
  odoo: renderOdoo,
  biometric: renderBiometric,
};

export async function render(mount, route) {
  // A bare /settings is redirected to /settings/general by the router
  // before this ever runs — see REDIRECTS in app.js.
  const section = route.path.slice('/settings/'.length);

  mount.innerHTML = `
    <div class="tabs">
      ${SECTIONS.map((s) => `
        <a href="#/settings/${s.slug}" class="${s.slug === section ? 'active' : ''}">
          ${esc(s.title)}</a>`).join('')}
    </div>
    <div id="settingsBody">${loading()}</div>`;

  const body = $('#settingsBody', mount);
  const renderSection = RENDERERS[section];
  if (!renderSection) {
    body.innerHTML = empty('Not found', 'That settings page does not exist.');
    return;
  }
  await renderSection(body);
}

/* ===========================================================================
 * General / Pairing / Working hours / Plan — the account's own rules.
 * Each submenu is its own <form>, so saving one never touches another.
 * ======================================================================== */

async function tenantContext() {
  // The schedule's live state comes with the dashboard, so the interval
  // field can say what is actually happening rather than "if a worker is
  // running". Plans come from the same public list the signup picker uses —
  // best-effort, since a failed fetch should still leave the rest usable.
  const [tenant, dash, plans] = await Promise.all([
    api.get('/tenant'),
    api.get('/dashboard').catch(() => null),
    api.get('/auth/plans').catch(() => []),
  ]);
  const schedule = dash?.schedule;
  const readonly = !auth.canWrite;
  // The switch below is theirs and still works, so it would sit there
  // looking like the answer. Say plainly that it is not.
  const stopped = tenant.syncable === false;
  const floor = tenant.plan_min_sync_interval_minutes;
  const intervalHelp = stopped
    ? 'Syncing is stopped for this account, so this setting has no effect yet.'
    : !schedule
    ? 'How often BioBridge pulls new punches.'
      + (floor ? ` Your plan allows ${floor} minutes or slower.` : '')
    : schedule.running
      ? `The scheduler is running${schedule.mode === 'celery' ? ' under Celery beat' : ''}`
        + `, so this takes effect on its own — no button, no cron entry.`
        + (floor ? ` Your plan allows ${floor} minutes or slower.` : '')
      : 'Nothing is scheduling syncs right now, so this value has no effect yet. '
        + 'Check SCHEDULER_MODE and the service log, or /health/scheduler.';
  return {
    tenant, dash, plans, schedule, readonly, stopped, floor, intervalHelp,
    activePlans: plans.filter((p) => p.is_active),
    renewalWarning: dash?.renewal_warning,
    // A plan already paid for (status active) defers a switch instead of
    // applying it — see app.api.v1.sync.update_tenant.
    willDefer: tenant.status === 'active' && Boolean(tenant.plan_id),
  };
}

function topBanners(ctx) {
  return `
    ${ctx.stopped ? banner(
      'Syncing is stopped for this account',
      'BioBridge has stopped collecting new punches. Your settings below '
      + 'still save, and they take effect once the account is restored. '
      + 'Contact support about restoring it.',
      'bad') : ''}
    ${ctx.readonly ? banner('Read-only', 'Your role cannot change settings.', 'warn') : ''}`;
}

function saveTenantForm(mount, formId, buttonId, reRender) {
  if (!auth.canWrite) return;
  $(`#${formId}`, mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    busy($(`#${buttonId}`, mount), () =>
      guard(async () => {
        await api.patch('/tenant', values);
        await reRender(mount);
      }, 'Settings saved')
    );
  });
}

async function renderGeneral(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, readonly, intervalHelp, schedule } = ctx;

  mount.innerHTML = `
    ${topBanners(ctx)}
    <form id="form" ${readonly ? 'inert' : ''}>
      <div class="card">
        <h2>General</h2>
        ${field({ name: 'name', label: 'Company', value: tenant.name, required: true })}
        ${field({
          name: 'timezone', label: 'Display timezone', value: tenant.timezone, required: true,
          help: 'Used to render attendance for your team. Separate from each biometric connection’s own device timezone.',
          datalist: timezoneNames(),
        })}
        ${field({
          name: 'sync_interval_minutes', label: 'Sync every (minutes)', type: 'number',
          value: tenant.sync_interval_minutes, required: true,
          help: intervalHelp, strongHelp: schedule ? !schedule.running : false,
        })}
        ${field({
          name: 'sync_enabled', label: 'Automatic sync', boolean: true, required: true,
          value: String(Boolean(tenant.sync_enabled)),
          options: [
            { value: 'true', label: 'On — pull punches on the interval above' },
            { value: 'false', label: 'Off — only sync when someone asks' },
          ],
          help: 'Turning this off stops the schedule for this account only. '
              + 'Nothing is lost: the cursor stays where it is and the next run '
              + 'picks up from there.',
        })}
        <div class="row end" style="margin-top:14px">
          <button class="primary" id="save">Save settings</button>
        </div>
      </div>
    </form>`;

  saveTenantForm(mount, 'form', 'save', renderGeneral);
}

async function renderPairing(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, readonly } = ctx;

  mount.innerHTML = `
    ${topBanners(ctx)}
    <form id="form" ${readonly ? 'inert' : ''}>
      <div class="card">
        <h2>Pairing <span class="hint">how raw punches become shifts</span></h2>
        ${field({
          name: 'pairing_mode', label: 'Mode', value: tenant.pairing_mode, required: true,
          options: [
            { value: 'alternating', label: 'Alternating — in, out, in, out' },
            { value: 'state_based', label: 'State based — trust the device' },
            { value: 'first_last', label: 'First / last — first in, last out' },
          ],
          help: 'Alternating suits devices with no IN/OUT keys, which is most of the field. State based needs those keys configured correctly; it falls back automatically when a device stamps everything "Check In".',
        })}
        ${field({
          name: 'min_punch_interval_seconds', label: 'Ignore repeat punches within (seconds)',
          type: 'number', value: tenant.min_punch_interval_seconds, required: true,
          help: 'Drops double-taps. A punch in the opposite direction is always kept — "in then straight out" is a real, if brief, visit.',
        })}
        ${field({
          name: 'max_shift_hours', label: 'Maximum shift length (hours)', type: 'number',
          value: tenant.max_shift_hours, required: true,
          help: 'Anything longer is capped and flagged, so one forgotten badge-out cannot write a 300-hour attendance.',
        })}
        ${field({
          name: 'day_boundary_hour', label: 'Shift day starts at (hour)', type: 'number',
          value: tenant.day_boundary_hour, required: true,
          help: 'Only used by first/last mode. A night-shift site sets this after the shift ends — 12 for noon, not the small hours.',
        })}
        ${field({
          name: 'orphan_out_policy', label: 'Check-out with no check-in',
          value: tenant.orphan_out_policy, required: true,
          options: [
            { value: 'flag', label: 'Flag — write a zero-length record for review' },
            { value: 'create', label: 'Create — infer a check-in 8 hours earlier' },
            { value: 'ignore', label: 'Ignore — drop it' },
          ],
        })}
        <div class="row end" style="margin-top:14px">
          <button class="primary" id="save">Save settings</button>
        </div>
      </div>
    </form>`;

  saveTenantForm(mount, 'form', 'save', renderPairing);
}

async function renderHours(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, readonly } = ctx;

  mount.innerHTML = `
    ${topBanners(ctx)}
    <form id="form" ${readonly ? 'inert' : ''}>
      <div class="card">
        <h2>Working hours <span class="hint">used to score late arrivals</span></h2>
        ${field({
          name: 'work_start_time', label: 'Day starts', value: tenant.work_start_time,
          required: true, placeholder: '09:00',
          help: 'Format HH:MM. Only the first arrival of a shift-day is scored, so returning from lunch never reads as late.',
        })}
        ${field({
          name: 'late_grace_minutes', label: 'Grace period (minutes)', type: 'number',
          value: tenant.late_grace_minutes, required: true,
        })}
        <div class="row end" style="margin-top:14px">
          <button class="primary" id="save">Save settings</button>
        </div>
      </div>
    </form>`;

  saveTenantForm(mount, 'form', 'save', renderHours);
}

async function renderPlan(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, activePlans, renewalWarning, willDefer, readonly } = ctx;

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      <h2>Plan <span class="hint">what this account is billed and limited by</span></h2>
      <div class="hint" style="margin-bottom:10px">
        ${tenant.plan_name ? `Currently <strong>${esc(tenant.plan_name)}</strong>` : 'No plan assigned — nothing is limited.'}
        ${tenant.plan_max_employees != null ? ` · up to ${esc(tenant.plan_max_employees)} employees` : ''}
        ${tenant.plan_min_sync_interval_minutes ? ` · syncs no faster than every ${esc(tenant.plan_min_sync_interval_minutes)} min` : ''}
        ${tenant.subscription_renews_at ? ` · renews ${esc(fmtIn(tenant.subscription_renews_at))}` : ''}
      </div>
      ${tenant.pending_plan_name ? banner(
        `Switching to ${tenant.pending_plan_name}`,
        `Takes effect once the current plan's period ends (renews `
          + `${fmtIn(tenant.subscription_renews_at)}) — choose `
          + `${esc(tenant.plan_name)} again below to cancel it.`,
        '') : ''}
      ${renewalWarning ? banner(
        renewalWarning.days_left <= 0 ? 'Renews today'
          : `Renews in ${renewalWarning.days_left} day${renewalWarning.days_left === 1 ? '' : 's'}`,
        'Switching plans here does not change that date — contact support to renew.',
        renewalWarning.urgent ? 'bad' : 'warn') : ''}
      ${activePlans.length && !readonly ? `
        <form id="planForm" class="row" style="gap:10px;align-items:flex-end;flex-wrap:wrap">
          ${field({
            name: 'plan_id', label: 'Switch to',
            value: tenant.pending_plan_id || tenant.plan_id || activePlans[0].id,
            options: activePlans.map((p) => ({
              value: p.id,
              label: p.monthly_price_cents != null
                ? `${p.name} — $${(p.monthly_price_cents / 100).toFixed(0)}/mo`
                : p.name,
            })),
          })}
          <button class="primary" id="changePlan" type="submit">Change plan</button>
        </form>
        <div class="hint">${willDefer
          ? 'You’re on a paid plan already: a switch is queued and takes '
            + 'effect at your next renewal, not right away.'
          : 'Takes effect immediately, any time.'} A downgrade never
          unmatches an employee already mapped — only new matches beyond the new
          cap are held back.${willDefer ? '' : ' Your sync interval is raised '
          + 'automatically if the new plan needs a slower one.'}</div>
      ` : !activePlans.length ? empty('No plans available', '') : ''}
    </div>`;

  const planForm = $('#planForm', mount);
  if (!planForm) return;
  planForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    // Mirrors the server's own rule (see update_tenant) just to pick the
    // right toast; the response after render(mount) reflects what the
    // server actually decided, regardless of what this guesses here.
    const message = values.plan_id === tenant.plan_id
      ? (tenant.pending_plan_id ? 'Scheduled change cancelled' : 'Already on this plan')
      : willDefer ? 'Plan change scheduled for your next renewal'
      : 'Plan changed';
    busy($('#changePlan', mount), () =>
      guard(async () => {
        await api.patch('/tenant', { plan_id: values.plan_id });
        await renderPlan(mount);
      }, message)
    );
  });
}

/* ===========================================================================
 * Odoo — where attendance is written. Still a single connection.
 * ======================================================================== */

let odooConfirmDelete = false;
//: The company list from the most recent Test Connection, kept across the
//: re-render that follows it (which re-fetches the connection fresh and
//: would otherwise lose it) — cleared whenever it stops being about the
//: connection currently on screen. Purely informational: it's how a
//: customer with several Odoo companies finds the id to type into the
//: field below, and how a misconfigured one gets diagnosed.
let odooLastCompanies = null; // { connId, companies: [{id, name}] } | null

async function renderOdoo(mount) {
  mount.innerHTML = loading();
  const odooList = await api.get('/odoo-connections');
  const odoo = odooList[0] || null;
  const readonly = !auth.canWrite;

  if (!odoo || odooLastCompanies?.connId !== odoo.id) odooLastCompanies = null;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      <h2>Odoo <span class="hint">where attendance is written</span></h2>
      ${odoo ? statusRow(odoo) : ''}
      ${odoo && odoo.status === 'connected' ? deviceTrackingRow(odoo, readonly) : ''}
      ${odoo && odoo.status === 'connected' ? companyScopeRow(odoo) : ''}
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
          name: 'api_key', label: 'API key', type: 'password',
          required: !odoo,
          placeholder: odoo ? 'unchanged' : '',
          help: odoo
            ? 'Stored encrypted and never shown again. Leave blank to keep the current one.'
            : 'Odoo → Preferences → Account Security → New API Key.',
        })}
        ${field({
          name: 'company_id', label: 'Odoo company ID', type: 'number',
          value: odoo?.company_id ?? '',
          help: 'Only matters if this Odoo has more than one company. Leave blank for '
            + 'a single-company Odoo. Set on a multi-company one, or this connection can '
            + 'see and write every company the API user has access to, not just one — '
            + 'test the connection below to see the company IDs this login can reach.',
          strongHelp: true,
        })}
        <div class="row">
          <button class="primary" id="saveOdoo">${odoo ? 'Save changes' : 'Connect Odoo'}</button>
          ${odoo ? '<button type="button" id="testOdoo">Test connection</button>' : ''}
          ${odoo && !readonly ? (
            odooConfirmDelete
              ? '<span class="hint">Remove this connection?</span>'
                + '<button type="button" class="sm" id="odooRemoveCommit">Remove</button>'
                + '<button type="button" class="sm link" id="odooRemoveCancel">Cancel</button>'
              : '<button type="button" class="sm link" id="odooRemove">Remove connection</button>'
          ) : ''}
        </div>
      </form>
      ${odooLastCompanies ? companiesHint(odoo, odooLastCompanies.companies) : ''}
    </div>`;

  if (readonly) return;

  $('#odooForm', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    if (odoo && !values.api_key) delete values.api_key;
    busy($('#saveOdoo', mount), () =>
      guard(async () => {
        if (odoo) await api.patch(`/odoo-connections/${odoo.id}`, values);
        else await api.post('/odoo-connections', values);
        odooConfirmDelete = false;
        await renderOdoo(mount);
      }, 'Odoo connection saved')
    );
  });

  $('#testOdoo', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        const result = await api.post(`/odoo-connections/${odoo.id}/test`);
        toast(result.message, result.ok ? 'ok' : 'bad');
        odooLastCompanies = result.detail?.companies
          ? { connId: odoo.id, companies: result.detail.companies }
          : null;
        await renderOdoo(mount);
      })
    )
  );

  $('#odooRemove', mount)?.addEventListener('click', () => {
    odooConfirmDelete = true;
    renderOdoo(mount);
  });
  $('#odooRemoveCancel', mount)?.addEventListener('click', () => {
    odooConfirmDelete = false;
    renderOdoo(mount);
  });
  $('#odooRemoveCommit', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        await api.del(`/odoo-connections/${odoo.id}`);
        odooConfirmDelete = false;
        await renderOdoo(mount);
      }, 'Odoo connection removed')
    )
  );

  $('#enableDeviceTracking', mount)?.addEventListener('click', (event) =>
    busy(event.target, () =>
      guard(async () => {
        const result = await api.post(`/odoo-connections/${odoo.id}/device-tracking/bootstrap`);
        toast(result.message, result.ok ? 'ok' : 'bad');
        await renderOdoo(mount);
      })
    )
  );
}

/* Whether attendance records show which terminal punched them, and the
 * one-click way to turn it on when it's off. Two ways to get there —
 * odoo.device_tracking_mode is "module" (odoo_addon/biobridge_attendance/
 * installed — Odoo.sh/self-hosted only) or "bootstrap" (BioBridge created
 * the field itself over the API, no add-on — the path that works on Odoo
 * Online too). Both read the same on this card; only the button differs. */
function deviceTrackingRow(odoo, readonly) {
  if (odoo.has_device_tracking) {
    const isModule = odoo.device_tracking_mode === 'module';
    const via = isModule
      ? 'via the installed BioBridge Attendance Devices add-on'
      : 'set up automatically, no Odoo add-on installed';
    // Setup adds only what's missing, so re-running it is how a connection
    // set up by an older version gets fields added since — the device
    // Company field and the rule that hides other companies' devices. This
    // used to disappear once tracking was on, leaving no way to do that.
    // Not offered for the add-on: it brings its own fields and rules.
    const rerun = isModule || readonly ? '' : `
        <button type="button" class="sm link" id="enableDeviceTracking"
                title="Adds anything newer versions of BioBridge set up that this Odoo doesn't have yet. Never changes or removes what's there.">
          Update setup</button>`;
    return `
      <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
        ${pill('active', 'Device tracking on')}
        <span style="color:var(--muted);font-size:12.5px">${esc(via)}</span>
        ${rerun}
      </div>`;
  }
  return `
    <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
      ${pill('pending', 'Device tracking off')}
      <span class="hint">Attendance records won't show which terminal punched them.</span>
      ${readonly ? '' : '<button type="button" class="sm" id="enableDeviceTracking">Enable device tracking</button>'}
    </div>`;
}

/* Whether this connection is pinned to one Odoo company. Only matters on a
 * multi-company Odoo — a single-company one has nothing to isolate from —
 * but there's no way to tell from here whether it's multi-company without
 * testing the connection, so this stays a neutral, low-key line rather
 * than a warning by default. */
function companyScopeRow(odoo) {
  if (odoo.company_id == null) {
    return `
      <div class="row" style="margin-bottom:14px">
        <span class="hint">Not scoped to a single Odoo company — sees every company this login can access.</span>
      </div>`;
  }
  const label = odoo.company_name
    ? `${odoo.company_name} (id ${odoo.company_id})`
    : `company id ${odoo.company_id}`;
  return `
    <div class="row" style="margin-bottom:14px">
      ${pill('active', 'Scoped')}
      <span style="color:var(--muted);font-size:12.5px">${esc(label)}</span>
    </div>`;
}

/* The company list a Test Connection just returned, shown once so a
 * customer with several companies on this Odoo can read off the id to
 * type into the field above — and, if the id they already set doesn't
 * appear in this list, why the test just failed. */
function companiesHint(odoo, companies) {
  if (companies.length <= 1) return '';
  const rows = companies
    .map((c) => `<li><code>${esc(String(c.id))}</code> — ${esc(c.name)}</li>`)
    .join('');
  return `
    <div class="note" style="margin-top:10px">
      This Odoo login can see ${companies.length} companies — set <strong>Odoo company ID</strong>
      above to isolate this connection to one of them:
      <ul style="margin:6px 0 0 18px">${rows}</ul>
    </div>`;
}

/* ===========================================================================
 * Biometric — where punches come from. A tenant runs in one of two modes,
 * chosen once (and changeable): connecting to a shared platform server, or
 * connecting several standalone devices directly. Either way it can hold
 * several connections — separate sites, separate terminals — but not a mix
 * of both kinds at once, so "+ Add" only ever offers the one that matches.
 * Both kinds are built and tested through the same mechanism underneath.
 * ======================================================================== */

let addKind = null;         // null | 'platform' | 'device'
let addProvider = null;     // provider slug picked for the form currently open
let editingSourceId = null;
let confirmDeleteId = null; // a source id pending removal confirmation
let pickingMode = false;    // showing the mode picker to switch an existing choice

const MODE_LABEL = { platform: 'Platform servers', device: 'Individual devices' };
const PROVIDER_LABEL = { zk_device: 'ZKTeco protocol' };

async function renderBiometric(mount) {
  mount.innerHTML = loading();
  const [tenant, sources, devices, allProviders] = await Promise.all([
    api.get('/tenant'),
    api.get('/sources'),
    api.get('/devices').catch(() => []),
    api.get('/providers').catch(() => []),
  ]);
  const readonly = !auth.canWrite;
  // The server is the one that actually enforces this (see
  // _enforce_biometric_mode); a tenant that already has connections from
  // before this field existed reads its mode from them rather than asking
  // again.
  const mode = tenant.biometric_mode || sources[0]?.connection_kind || null;
  const showPicker = !mode || pickingMode;
  // Only offer what makes sense in this mode — a standalone-device protocol
  // has no business appearing while set up for a shared platform, and vice
  // versa. See AttendanceProvider.kinds.
  const providers = allProviders.filter((p) => (p.kinds || ['platform', 'device']).includes(mode));

  // Providers that can create a user on the device — "Import terminals"
  // also creates missing Odoo employees there for these (see
  // app/services/provisioning.py for the rule).
  const canProvision = new Set(allProviders
    .filter((p) => (p.capabilities || []).includes('read_employees')
      && (p.capabilities || []).includes('write_employees'))
    .map((p) => p.slug));

  const devicesBySource = {};
  devices.forEach((d) => { (devicesBySource[d.source_id] ||= []).push(d); });

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      <h2>Biometric <span class="hint">where punches come from</span></h2>

      ${mode && !showPicker ? `
        <div class="row" style="align-items:center;gap:8px;margin-bottom:14px">
          <span class="hint">Connection type: <strong>${esc(MODE_LABEL[mode])}</strong></span>
          ${!readonly ? '<button type="button" class="sm link" id="changeMode">Change</button>' : ''}
        </div>` : ''}

      ${showPicker ? modePicker(mode, readonly) : ''}

      ${sources.map((s) => sourceCard(s, devicesBySource[s.id] || [], readonly, canProvision.has(s.provider))).join('')}
      ${!sources.length ? empty(
        'No biometric connections yet',
        readonly || !mode ? '' : 'Add one below.'
      ) : ''}

      ${mode && !showPicker && !readonly ? `
        <div class="row" style="margin-top:14px">
          <button type="button" id="addConnection" ${addKind ? 'disabled' : ''}>
            + Add ${mode === 'device' ? 'individual device' : 'platform connection'}</button>
        </div>
        ${addKind ? sourceFormHtml(null, addKind, providers, addProvider) : ''}
      ` : ''}
    </div>`;

  wireBiometric(mount, mode);
}

function modePicker(currentMode, readonly) {
  return `
    <div class="hint" style="margin-bottom:10px">
      ${currentMode
        ? 'Switching only changes what “+ Add” offers next — nothing already connected is touched.'
        : 'Pick how this account’s biometric connections work before adding the first one.'}
    </div>
    ${readonly ? '<div class="hint">Your role cannot change this.</div>' : `
      <div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:8px">
        <button type="button" class="sm" data-set-mode="platform" ${currentMode === 'platform' ? 'disabled' : ''}>
          Platform / server (e.g. BioTime)</button>
        <button type="button" class="sm" data-set-mode="device" ${currentMode === 'device' ? 'disabled' : ''}>
          Individual devices</button>
        ${currentMode ? '<button type="button" class="sm link" id="cancelModePick">Cancel</button>' : ''}
      </div>
      <div class="hint" style="margin-bottom:14px">
        Platform: one connection manages many terminals through a shared
        server. Individual devices: each terminal connects on its own, with
        no shared server in between. Both are added and tested the same way —
        this only decides which one you can add.
      </div>`}`;
}

/** One line for the toast after Import terminals created employees. */
function provisionSummary(r) {
  const parts = [];
  const names = (list) => list.slice(0, 5).map((e) => `${e.name} (${e.emp_code})`).join(', ')
    + (list.length > 5 ? `, and ${list.length - 5} more` : '');
  if (r.created.length) parts.push(`Created on the device: ${names(r.created)}.`);
  else parts.push('No new employees to create on the device.');
  if (r.failed.length) {
    parts.push(`Could not create ${r.failed.length}: `
      + r.failed.slice(0, 3).map((e) => `${e.emp_code} — ${e.error}`).join('; ') + '.');
  }
  if (r.no_badge_or_pin) {
    parts.push(`${r.no_badge_or_pin} Odoo employee${r.no_badge_or_pin === 1 ? ' has' : 's have'} no Badge ID or PIN, so ${r.no_badge_or_pin === 1 ? 'was' : 'were'} skipped.`);
  }
  return parts.join(' ');
}

function sourceCard(source, devices, readonly, canProvision = false) {
  const kindLabel = source.connection_kind === 'device' ? 'Individual device' : 'Platform';
  const providerLabel = PROVIDER_LABEL[source.provider];
  const isEditing = editingSourceId === source.id;
  const isConfirming = confirmDeleteId === source.id;

  return `
    <div class="card" style="margin-bottom:14px" data-source="${esc(source.id)}">
      <div class="row" style="justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
        <div>
          <div class="row" style="gap:8px;align-items:center">
            <strong>${esc(source.name)}</strong>
            <span class="pill mute">${esc(kindLabel)}</span>
            ${providerLabel ? `<span class="pill mute">${esc(providerLabel)}</span>` : ''}
          </div>
          <div class="hint mono" style="margin-top:2px">${esc(source.base_url)}</div>
        </div>
        ${pill(source.status)}
      </div>
      <div class="hint" style="margin:8px 0">
        checked ${esc(fmtAgo(source.last_checked_at))}
        · ${devices.length} terminal${devices.length === 1 ? '' : 's'}
      </div>
      ${source.status_message ? banner('Last error', source.status_message, 'bad') : ''}

      ${!readonly ? `
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <button type="button" class="sm" data-edit="${esc(source.id)}">${isEditing ? 'Close' : 'Edit'}</button>
          <button type="button" class="sm" data-test="${esc(source.id)}">Test connection</button>
          <button type="button" class="sm" data-discover="${esc(source.id)}"
                  ${canProvision ? 'data-provision="1" title="Also creates Odoo employees who have a Badge ID or PIN and aren\'t on the device yet."' : ''}>Import terminals</button>
          ${!isConfirming ? `<button type="button" class="sm link" data-remove="${esc(source.id)}">Remove</button>` : ''}
        </div>
        ${isConfirming ? `
          <div class="row" style="margin-top:8px;gap:8px;align-items:center">
            <span class="hint">Remove this connection? Its terminals go with it.</span>
            <button type="button" class="sm" data-remove-commit="${esc(source.id)}">Remove</button>
            <button type="button" class="sm link" data-remove-cancel="${esc(source.id)}">Cancel</button>
          </div>` : ''}
        ${isEditing ? sourceFormHtml(source, source.connection_kind, []) : ''}
      ` : ''}

      ${devices.length ? `
        <div class="scroll" style="margin-top:12px">
          <table>
            <thead><tr><th>Device</th><th>Serial</th><th>IP</th><th class="num">Punches</th><th>Last seen</th><th>Pairing</th><th></th></tr></thead>
            <tbody>
              ${devices.map((d) => `
                <tr data-device="${esc(d.id)}">
                  <td>
                    ${esc(d.alias || '—')}
                    ${d.missing_since
                      ? ` ${pill('missing')} <span class="hint" title="Not reported by this connection's last &quot;Import terminals&quot; run">since ${esc(fmtAgo(d.missing_since))}</span>`
                      : ''}
                  </td>
                  <td class="mono">${esc(d.serial_number)}</td>
                  <td class="mono">${esc(d.ip_address || '—')}</td>
                  <td class="num">${esc(d.punch_count)}</td>
                  <td>${esc(fmtAgo(d.last_seen_at))}</td>
                  <td>${esc(d.pairing_override || 'account default')}</td>
                  <td style="text-align:right">
                    ${readonly ? pill(d.is_enabled ? 'active' : 'skipped')
                      : `<button type="button" class="sm" data-toggle="${esc(d.id)}" data-on="${d.is_enabled}">
                          ${d.is_enabled ? 'Disable' : 'Enable'}</button>`}
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>` : ''}
    </div>`;
}

/** Shared by "add a new connection" and "edit an existing one". Both
 * connection kinds — and, within "device", both providers — are built and
 * tested through the same mechanism, but a standalone-device protocol like
 * ZKTeco's has a genuinely different shape (a device address instead of a
 * server URL, no username, an optional comm key instead of a password), so
 * the field set itself now follows the chosen provider, not just `kind`. */
function sourceFormHtml(source, kind, providers, currentProvider) {
  const isDevice = kind === 'device';
  const provider = source ? source.provider : (currentProvider || providers[0]?.slug || 'biotime');
  const isZk = provider === 'zk_device';
  const addressValue = source
    ? (isZk ? source.base_url.replace(/^zk:\/\//i, '') : source.base_url)
    : '';

  return `
    <form id="sourceForm" class="sourceForm" data-kind="${esc(kind)}" data-provider="${esc(provider)}"
          ${source ? `data-editing="${esc(source.id)}"` : ''}
          style="margin-top:12px;padding-top:12px;border-top:1px solid var(--rule)">
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: isDevice ? 'Front door terminal' : 'Primary BioTime',
        help: 'Shown in this list — worth naming for the site or terminal it is.',
      })}
      ${field({
        name: 'base_url', label: isZk ? 'Device address' : isDevice ? 'Device address' : 'Server URL',
        required: true, value: addressValue,
        placeholder: isZk ? '192.168.1.50' : isDevice ? 'https://192.168.1.50:8081' : 'https://biotime.example.com:8081',
        help: isZk
          ? 'The device’s own IP, reachable from wherever BioBridge runs. A port is optional — defaults to 4370.'
          : isDevice
          ? 'The device’s own address, reachable from wherever BioBridge runs.'
          : undefined,
      })}
      ${!isZk ? field({ name: 'username', label: 'Username', required: true, value: source?.username || '' }) : ''}
      ${field({
        name: 'password', label: isZk ? 'Comm key' : 'Password', type: 'password',
        required: !source && !isZk,
        placeholder: source ? 'unchanged' : '',
        help: isZk
          ? 'Only if the device has a communication password set. Leave blank for the factory default (no password).'
          : source ? 'Leave blank to keep the current one.' : '',
      })}
      ${field({
        name: 'server_timezone', label: isDevice ? 'Device timezone' : 'Server timezone',
        required: true, value: source?.server_timezone || 'UTC',
        help: 'The zone the device itself runs in — not yours and not Odoo’s. Punch times arrive with no offset, so a wrong value shifts every attendance record by hours without any error.',
        strongHelp: true, datalist: timezoneNames(),
      })}
      ${!isZk ? field({
        name: 'auth_type', label: 'Auth style', value: source?.auth_type || 'token',
        options: ['token', 'jwt'],
        help: 'BioTime 8.5+ usually needs jwt; older builds use token.',
      }) : ''}
      <div class="row" style="margin-top:4px;gap:8px">
        <button class="primary sm" id="saveSource" type="submit">
          ${source ? 'Save changes' : isDevice ? 'Connect device' : 'Connect platform'}</button>
        <button class="sm link" type="button" data-cancel-form="1">Cancel</button>
      </div>
    </form>`;
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

function wireBiometric(mount, mode) {
  $('#changeMode', mount)?.addEventListener('click', () => {
    pickingMode = true;
    addKind = null;
    addProvider = null;
    editingSourceId = null;
    renderBiometric(mount);
  });
  $('#cancelModePick', mount)?.addEventListener('click', () => {
    pickingMode = false;
    renderBiometric(mount);
  });
  mount.querySelectorAll('[data-set-mode]').forEach((button) => {
    button.addEventListener('click', (event) =>
      busy(event.target, () =>
        guard(async () => {
          await api.patch('/tenant', { biometric_mode: button.dataset.setMode });
          pickingMode = false;
          await renderBiometric(mount);
        }, 'Connection type set')
      )
    );
  });

  $('#addConnection', mount)?.addEventListener('click', () => {
    addKind = mode;
    addProvider = null;
    editingSourceId = null;
    renderBiometric(mount);
  });

  mount.querySelector('select[name=provider]')?.addEventListener('change', (event) => {
    addProvider = event.target.value;
    renderBiometric(mount);
  });

  mount.querySelectorAll('[data-edit]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.edit;
      editingSourceId = editingSourceId === id ? null : id;
      addKind = null;
      renderBiometric(mount);
    });
  });

  mount.querySelectorAll('[data-cancel-form]').forEach((button) => {
    button.addEventListener('click', () => {
      addKind = null;
      addProvider = null;
      editingSourceId = null;
      renderBiometric(mount);
    });
  });

  mount.querySelectorAll('[data-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      confirmDeleteId = button.dataset.remove;
      renderBiometric(mount);
    });
  });
  mount.querySelectorAll('[data-remove-cancel]').forEach((button) => {
    button.addEventListener('click', () => {
      confirmDeleteId = null;
      renderBiometric(mount);
    });
  });
  mount.querySelectorAll('[data-remove-commit]').forEach((button) => {
    button.addEventListener('click', (event) =>
      busy(event.target, () =>
        guard(async () => {
          await api.del(`/sources/${button.dataset.removeCommit}`);
          confirmDeleteId = null;
          await renderBiometric(mount);
        }, 'Connection removed')
      )
    );
  });

  mount.querySelectorAll('[data-test]').forEach((button) => {
    button.addEventListener('click', (event) =>
      busy(event.target, () =>
        guard(async () => {
          const result = await api.post(`/sources/${button.dataset.test}/test`);
          toast(result.message, result.ok ? 'ok' : 'bad');
          await renderBiometric(mount);
        })
      )
    );
  });

  mount.querySelectorAll('[data-discover]').forEach((button) => {
    button.addEventListener('click', (event) =>
      busy(event.target, () =>
        guard(async () => {
          const sourceId = button.dataset.discover;
          await api.post(`/sources/${sourceId}/discover-devices`);
          toast('Terminals imported', 'ok');
          if (button.dataset.provision) {
            // Separate call on purpose: the terminals are already imported
            // whatever happens here, and this reports its own result.
            try {
              const result = await api.post(`/sources/${sourceId}/provision-employees`);
              toast(provisionSummary(result), result.failed.length ? 'bad' : 'ok');
            } catch (error) {
              if (error.status !== 401) toast(`Employees not created on the device: ${error.message}`, 'bad');
            }
          }
          await renderBiometric(mount);
        })
      )
    );
  });

  mount.querySelectorAll('.sourceForm').forEach((form) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const values = readForm(event.target);
      const editing = form.dataset.editing;
      if (editing && !values.password) delete values.password;
      if (!editing) {
        values.connection_kind = form.dataset.kind;
        // Only rendered when there's a real choice — otherwise the one
        // eligible provider for this kind still has to be sent explicitly.
        values.provider = values.provider || form.dataset.provider;
      }
      // A standalone device's address isn't a URL the way a server's is;
      // the backend expects it tagged so it knows not to treat it as HTTP.
      if (form.dataset.provider === 'zk_device' && values.base_url && !/^zk:\/\//i.test(values.base_url)) {
        values.base_url = `zk://${values.base_url}`;
      }
      busy(form.querySelector('button[type=submit]'), () =>
        guard(async () => {
          if (editing) await api.patch(`/sources/${editing}`, values);
          else await api.post('/sources', values);
          addKind = null;
          addProvider = null;
          editingSourceId = null;
          await renderBiometric(mount);
        }, editing ? 'Connection saved' : 'Connection added')
      );
    });
  });

  mount.querySelectorAll('[data-toggle]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          await api.patch(`/devices/${button.dataset.toggle}`, {
            is_enabled: button.dataset.on !== 'true',
          });
          await renderBiometric(mount);
        })
      )
    );
  });
}
