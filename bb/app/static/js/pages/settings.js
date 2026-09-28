/* Settings: tenant configuration, split into submenus.
 *
 * General / Pairing / Working hours / Plan are the account's own rules —
 * previously one long scrolling form, now one submenu each so a change to
 * pairing does not require scrolling past working hours to find it. Odoo and
 * Biometric absorb what used to be the standalone "Connections" page: where
 * attendance is written, and what it is written from.
 */

import { api, auth } from '../api.js';
import { needsMatch, unmappedCard, wireUnmapped } from './data.js';
import {
  $, $$, banner, busy, empty, esc, field, fmtAgo, fmtIn, guard, loading, pill, pricingCards, readForm,
  timezoneNames, toast, wirePricingCards,
} from '../ui.js';

/* Settings is the one configuration menu: the account's own rules, then the
 * two connections — Odoo (where attendance goes, and the badges still waiting
 * for an Odoo employee) and Biometric (where punches come from). Each
 * submenu has its own entry in the sidebar's Configuration section — see NAV
 * in app.js — so there is nothing to switch between in-page here. */
const RENDERERS = {
  general: renderGeneral,
  pairing: renderPairing,
  hours: renderHours,
  plan: renderPlan,
  'plan/choose': renderChoosePlan,
  odoo: renderOdoo,
  biometric: renderBiometric,
};

export async function render(mount, route) {
  // A bare /settings is redirected to /settings/general by the router
  // before this ever runs — see REDIRECTS in app.js.
  const section = route.path.slice('/settings/'.length);

  mount.innerHTML = `<div id="settingsBody">${loading()}</div>`;

  const body = $('#settingsBody', mount);
  const renderSection = RENDERERS[section];
  if (!renderSection) {
    body.innerHTML = empty('Not found', 'That settings page does not exist.');
    return;
  }
  await renderSection(body, route);
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

/** A card's title bar, with the card's actions on the right.
 *
 * Every settings page puts its buttons here — at the top, where they are
 * found without scrolling past the whole form first. The bar is sticky, so
 * on a long form Save is still in reach from the bottom of it. */
function cardHead(title, hint, actions = '') {
  return `
    <div class="card-head">
      <h2>${esc(title)}${hint ? ` <span class="hint">${esc(hint)}</span>` : ''}</h2>
      ${actions ? `<div class="actions">${actions}</div>` : ''}
    </div>`;
}

const saveButton = (readonly) =>
  readonly ? '' : '<button class="primary" id="save" type="submit">Save settings</button>';

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
        ${cardHead('General', '', saveButton(readonly))}
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
        ${cardHead('Pairing', 'how raw punches become shifts', saveButton(readonly))}
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
        ${cardHead('Working hours', 'used to score late arrivals', saveButton(readonly))}
        ${field({
          name: 'work_start_time', label: 'Day starts', value: tenant.work_start_time,
          required: true, placeholder: '09:00',
          help: 'Format HH:MM. Only the first arrival of a shift-day is scored, so returning from lunch never reads as late.',
        })}
        ${field({
          name: 'late_grace_minutes', label: 'Grace period (minutes)', type: 'number',
          value: tenant.late_grace_minutes, required: true,
        })}
      </div>
    </form>`;

  saveTenantForm(mount, 'form', 'save', renderHours);
}

async function renderPlan(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, activePlans, renewalWarning, readonly } = ctx;

  // A plan retired since this account chose it is no longer offered, but it is
  // still what they are on — say so rather than showing no card as current.
  const retired = tenant.plan_id && !activePlans.some((p) => p.id === tenant.plan_id);

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      ${cardHead('Plan', 'what this account is billed and limited by',
        activePlans.length && !readonly
          ? `<a class="btn primary-link" href="#/settings/plan/choose">${tenant.plan_id ? 'Change plan' : 'Choose a plan'}</a>`
          : '')}
      <div class="hint" style="margin-bottom:12px">
        ${tenant.plan_name ? `Currently <strong>${esc(tenant.plan_name)}</strong>` : 'No plan assigned — nothing is limited.'}
        ${tenant.plan_max_employees != null ? ` · up to ${esc(tenant.plan_max_employees)} employees` : ''}
        ${tenant.plan_min_sync_interval_minutes ? ` · syncs no faster than every ${esc(tenant.plan_min_sync_interval_minutes)} min` : ''}
        ${tenant.subscription_renews_at ? ` · renews ${esc(fmtIn(tenant.subscription_renews_at))}` : ''}
      </div>
      ${tenant.pending_plan_name ? banner(
        `Switching to ${tenant.pending_plan_name}`,
        `Takes effect once the current plan's period ends (renews `
          + `${fmtIn(tenant.subscription_renews_at)}) — choose `
          + `${tenant.plan_name} again to cancel it.`,
        '', !readonly ? { href: '#/settings/plan/choose', label: 'Change' } : null) : ''}
      ${renewalWarning && (tenant.status === 'trialing' || tenant.plan_id) ? banner(
        tenant.status === 'trialing'
          ? (renewalWarning.days_left <= 0 ? 'Trial ends today'
              : `Trial ends in ${renewalWarning.days_left} day${renewalWarning.days_left === 1 ? '' : 's'}`)
          : (renewalWarning.days_left <= 0 ? 'Renews today'
              : `Renews in ${renewalWarning.days_left} day${renewalWarning.days_left === 1 ? '' : 's'}`),
        tenant.status === 'trialing'
          ? 'Choose a plan to keep syncing once it ends.'
          : 'Changing plans here does not change that date — contact support to renew.',
        renewalWarning.urgent ? 'bad' : 'warn') : ''}
      ${retired ? banner(
        `${tenant.plan_name || 'Your plan'} is no longer offered`,
        'You stay on it until you choose another.',
        'warn', !readonly ? { href: '#/settings/plan/choose', label: 'Choose a plan' } : null) : ''}
      ${!activePlans.length ? empty('No plans available', '') : ''}
      ${readonly ? '<div class="hint">Your role cannot change the plan.</div>' : ''}
    </div>`;
}

/** "Choose a plan" / "Change plan" from the card above — the full pricing
 * grid, one plan per card, reached only by someone who can already write to
 * this account (renderPlan hides the link otherwise, and this still checks
 * for a hand-typed URL). Picking one PATCHes /tenant directly: unlike the
 * public #/plans page, there is an account here for a click to act on. */
async function renderChoosePlan(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, activePlans, willDefer, readonly } = ctx;

  if (readonly) {
    mount.innerHTML = `
      ${topBanners(ctx)}
      <div class="card">
        ${cardHead('Choose a plan', '', '<a class="btn" href="#/settings/plan">&larr; Plan</a>')}
        <div class="hint">Your role cannot change the plan.</div>
      </div>`;
    return;
  }

  const tags = {};
  if (tenant.plan_id) {
    // Normally locked — nothing to do with the plan you're already on. The
    // one exception: a switch is already queued, in which case picking the
    // current plan again is how it gets cancelled, so that card stays live.
    tags[tenant.plan_id] = tenant.pending_plan_id
      ? { label: 'Current plan', tone: 'current', locked: false, ctaLabel: 'Cancel scheduled switch' }
      : { label: 'Current plan', tone: 'current' };
  }
  if (tenant.pending_plan_id) tags[tenant.pending_plan_id] = { label: 'Already scheduled', tone: 'warn' };

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      ${cardHead('Choose a plan', '', '<a class="btn" href="#/settings/plan">&larr; Plan</a>')}
      <p class="hint" style="margin:-4px 0 16px">${willDefer
        ? 'You’re on a paid plan already: switching here queues the change for '
          + 'your next renewal rather than applying it right away.'
        : 'Takes effect immediately.'} A downgrade never unmatches an employee
        already mapped — only new matches beyond the new cap are held
        back.${willDefer ? '' : ' Your sync interval is raised automatically '
        + 'if the new plan needs a slower one.'}</p>
      ${activePlans.length ? pricingCards({
        plans: activePlans, tags, showRecommended: false,
        ctaLabel: willDefer ? 'Switch at renewal' : 'Switch to this plan',
      }) : empty('No plans available', '')}
    </div>`;

  if (!activePlans.length) return;

  const nameOf = (id) => activePlans.find((p) => p.id === id)?.name || 'this plan';
  wirePricingCards(mount, (planId, button) => {
    // Cancelling a scheduled switch means picking the current plan again —
    // that card is never locked even though it's tagged, so this still
    // needs its own message rather than the plain "Plan changed" default.
    const message = planId === tenant.plan_id
      ? (tenant.pending_plan_id ? 'Scheduled change cancelled' : 'Already on this plan')
      : willDefer ? `Switch to ${nameOf(planId)} scheduled for your next renewal`
      : `Switched to ${nameOf(planId)}`;
    busy(button, () =>
      guard(async () => {
        await api.patch('/tenant', { plan_id: planId });
        window.location.hash = '#/settings/plan';
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

async function renderOdoo(mount, route) {
  mount.innerHTML = loading();
  const [odooList, mappings] = await Promise.all([
    api.get('/odoo-connections'),
    api.get('/mappings?limit=500').catch(() => []),
  ]);
  const odoo = odooList[0] || null;
  const readonly = !auth.canWrite;

  if (!odoo || odooLastCompanies?.connId !== odoo.id) odooLastCompanies = null;

  // Test sits before Connect, left to right, and on a new connection Connect
  // only unlocks once the values in the form have been tested — see
  // wireTestFirst. Remove is kept apart from both, on the far left.
  const removeControls = odoo && !readonly ? (
    odooConfirmDelete
      ? '<span class="hint">Remove this connection?</span>'
        + '<button type="button" class="sm danger" id="odooRemoveCommit">Remove</button>'
        + '<button type="button" class="sm link" id="odooRemoveCancel">Cancel</button>'
      : '<button type="button" class="sm link" id="odooRemove">Remove connection</button>'
  ) : '';
  const actions = readonly ? '' : `
    ${removeControls}
    <button type="button" id="testOdoo">Test connection</button>
    <button class="primary" id="saveOdoo" type="submit" form="odooForm">
      ${odoo ? 'Save changes' : 'Connect Odoo'}</button>`;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      ${cardHead('Odoo', 'where attendance is written', actions)}
      ${!odoo && !readonly ? testFirstHint('Connect Odoo') : ''}
      <div id="odooTestResult"></div>
      ${odoo ? statusRow(odoo) : ''}
      <div id="odooStatusExtras">${trackingExtrasHtml(odoo)}</div>
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
            + 'Test connection lists the company IDs this login can reach.',
          strongHelp: true,
        })}
      </form>
      ${odooLastCompanies ? companiesHint(odoo, odooLastCompanies.companies) : ''}
    </div>
    ${odoo ? unmappedCard(mappings.filter(needsMatch)) : ''}`;

  if (odoo) wireUnmapped(mount, () => renderOdoo(mount));
  // "Show unmapped employees" on the Employees page lands here.
  if (route?.query?.show === 'unmapped') {
    const card = $('#unmapped', mount);
    if (card) {
      card.classList.add('flash');
      card.scrollIntoView({ block: 'start' });
    }
    history.replaceState(null, '', '#/settings/odoo');
  }

  if (readonly) return;

  const form = $('#odooForm', mount);
  const odooValues = () => {
    const values = readForm(form);
    if (odoo && !values.api_key) delete values.api_key;
    return values;
  };

  // The device-tracking rows live in their own container so a successful
  // Test connection can repaint just them — see the probe below — without
  // disturbing the rest of the form or the test-result panel next to it.
  const paintTrackingExtras = () => {
    const box = $('#odooStatusExtras', mount);
    if (box) box.innerHTML = trackingExtrasHtml(odoo);
  };

  wireTestFirst({
    form,
    test: $('#testOdoo', mount),
    commit: $('#saveOdoo', mount),
    result: $('#odooTestResult', mount),
    label: odoo ? 'Save changes' : 'Connect Odoo',
    gate: !odoo,
    probe: async () => {
      const result = await api.post('/odoo-connections/test', {
        ...odooValues(), ...(odoo ? { conn_id: odoo.id } : {}),
      });
      // Keeping device tracking set up used to mean its own click —
      // "Enable device tracking", then "Update setup" whenever a newer
      // BioBridge added something new to create. Every successful test on
      // an already-saved connection now does that too, and rides along on
      // this same toast rather than a button and a status line of its own
      // (see the pill above, which is the lasting record of where it
      // landed). Quiet when nothing changed — still on, as before — since
      // that's not news; only a fresh turn-on or a failure earns a word.
      // Best-effort either way: it must never turn a working connectivity
      // test into a failed one. The add-on path ('module') manages its own
      // fields, so it's left alone.
      if (result.ok && odoo && odoo.device_tracking_mode !== 'module') {
        const wasOn = odoo.has_device_tracking;
        try {
          const boot = await api.post(`/odoo-connections/${odoo.id}/device-tracking/bootstrap`);
          if (boot.detail) {
            odoo.has_device_tracking = Boolean(boot.detail.has_device_tracking);
            odoo.device_tracking_mode = boot.detail.device_tracking_mode ?? odoo.device_tracking_mode;
            odoo.status = 'connected';
            paintTrackingExtras();
          }
          // Said every time, not just the first — the pill is the lasting
          // record, but the toast is what someone actually reads right
          // after clicking, so it should say where things stand, on or off,
          // not only when something just changed. The last branch below
          // should not be reachable — a successful bootstrap re-probe
          // always leaves has_device_tracking true — but it is not
          // guaranteed by anything the frontend can see, so it gets its own
          // explicit word rather than silently saying nothing.
          if (!boot.ok) result.message += `. Device tracking: ${boot.message}`;
          else if (odoo.has_device_tracking) {
            result.message += `. Device tracking ${wasOn ? 'is' : 'turned'} on.`;
          } else {
            result.message += '. Device tracking is still off.';
          }
        } catch {
          result.message += '. Device tracking could not be kept up to date just now.';
        }
      }
      return result;
    },
    after: (result) => companiesHint(odoo, result.detail?.companies || []),
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = odooValues();
    busy($('#saveOdoo', mount), () =>
      guard(async () => {
        if (odoo) {
          await api.patch(`/odoo-connections/${odoo.id}`, values);
          // Saving an edit used to leave the connection "unverified" until
          // someone remembered to press Test. Check it straight away instead,
          // so the status on screen is about the values just saved.
          const result = await api.post(`/odoo-connections/${odoo.id}/test`);
          toast(result.message, result.ok ? 'ok' : 'bad');
          odooLastCompanies = result.detail?.companies
            ? { connId: odoo.id, companies: result.detail.companies }
            : null;
        } else {
          await api.post('/odoo-connections', values);
          toast('Odoo connected', 'ok');
        }
        odooConfirmDelete = false;
        await renderOdoo(mount);
      })
    );
  });

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
}

/* ===========================================================================
 * Test before you connect.
 *
 * Both connection forms used to offer Test Connection only once Connect had
 * already stored the credentials — so the first anyone heard of a wrong URL
 * or an unreachable device was after committing it. Now Test runs on the
 * values in the form (POST /odoo-connections/test, /sources/test — nothing is
 * saved), and on a new connection the Connect button unlocks only once the
 * values currently in the form have been tested.
 *
 * A failed test does not trap anyone: the button then reads "… anyway",
 * because a device that is switched off right now can still be configured
 * correctly. Changing a field after a test asks for a fresh one.
 * ======================================================================== */

function testFirstHint(connectLabel) {
  return `
    <div class="steps">
      <span class="step"><b>1</b> Fill in the details</span>
      <span class="step"><b>2</b> Test connection</span>
      <span class="step"><b>3</b> ${esc(connectLabel)}</span>
    </div>`;
}

/** A connection's test outcome, still shown inline for the add-connection
 * wizard (see openAddWizard) — a brand-new, not-yet-saved connection has no
 * status pill anywhere else on screen for "Connection works"/"failed" to be
 * redundant with, so it stays put there rather than becoming a toast. */
function testResultHtml(result, stale) {
  if (!result) return '';
  return `
    <div class="test-result ${result.ok ? 'ok' : 'bad'}${stale ? ' stale' : ''}">
      <strong>${result.ok ? 'Connection works' : 'Connection failed'}</strong>
      <span>${esc(result.message)}</span>
      ${stale ? '<span class="hint">The form has changed since this test — test again.</span>' : ''}
    </div>`;
}

/** Test on an existing connection's own card (the Odoo card, a Biometric
 * source's inline edit form) is different from the wizard: the card already
 * carries a static status pill, so a second, sticky "Connection works" panel
 * next to it was saying the same thing twice. The outcome now surfaces as a
 * toast instead — long enough to read, gone once read — and the panel here
 * is left for `after()` alone: follow-up detail a toast is too small for,
 * like Odoo's list of company ids to pick from. */
function wireTestFirst({ form, test, commit, result, probe, label, gate, after }) {
  let tested = null;   // { fingerprint, ok } for the values last tested
  let last = null;     // the TestResult itself
  const fingerprint = () => JSON.stringify(readForm(form));

  const paint = () => {
    const current = Boolean(tested) && tested.fingerprint === fingerprint();
    if (gate) {
      commit.disabled = !current;
      commit.textContent = current && !tested.ok ? `${label} anyway` : label;
      commit.title = current ? '' : 'Test the connection first';
    }
    result.innerHTML = last && current && after ? after(last) : '';
  };

  form.addEventListener('input', paint);
  form.addEventListener('change', paint);
  test.addEventListener('click', () => {
    // The same required-field check Connect would do, so a test is never
    // spent on a form that could not have been saved anyway.
    if (!form.reportValidity()) return;
    const fp = fingerprint();
    busy(test, () =>
      guard(async () => {
        last = await probe();
        tested = { fingerprint: fp, ok: last.ok };
        toast(last.message, last.ok ? 'ok' : 'bad');
      })
    ).then(paint);
  });
  paint();
}

/* Whether attendance records show which terminal punched them — purely a
 * status line now, no button. Two ways it gets there — odoo.device_tracking_mode
 * is "module" (odoo_addon/biobridge_attendance/ installed — Odoo.sh/
 * self-hosted only), detected on its own the moment Test connection sees it,
 * or "bootstrap" (BioBridge created the field itself over the API, no
 * add-on — the path that works on Odoo Online too), kept current by every
 * Test connection click — see that probe, above. Nothing to click here
 * either way; this row just says which one it is, or that it's still off. */
function deviceTrackingRow(odoo) {
  if (odoo.has_device_tracking) {
    const via = odoo.device_tracking_mode === 'module'
      ? 'via the installed BioBridge Attendance Devices add-on'
      : 'set up automatically, no Odoo add-on installed';
    return `
      <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
        ${pill('active', 'Device tracking on')}
        <span style="color:var(--muted);font-size:12.5px">${esc(via)}</span>
      </div>`;
  }
  return `
    <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
      ${pill('pending', 'Device tracking off')}
      <span class="hint">Attendance records won't show which terminal punched them. Test connection turns this on.</span>
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

/** Both rows above, together — everything inside #odooStatusExtras, so a
 * successful Test connection can repaint just this much of the card once it
 * has brought device tracking up to date, without touching the rest of the
 * form or the test-result panel. */
function trackingExtrasHtml(odoo) {
  if (!odoo || odoo.status !== 'connected') return '';
  return deviceTrackingRow(odoo) + companyScopeRow(odoo);
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
 * Biometric — where punches come from. "+ Add connection" opens a wizard
 * (openAddWizard): 1 which kind — a platform server that manages many
 * terminals (BioTime), or a standalone device reached directly (ZKTeco) —
 * 2 its details, 3 test, then connect. Both kinds can sit side by side, and
 * both are built and tested the same way underneath.
 * ======================================================================== */

let editingSourceId = null;
let confirmDeleteId = null; // a source id pending removal confirmation

const KIND_CHOICES = [
  {
    kind: 'platform',
    title: 'Platform server',
    example: 'e.g. ZKTeco BioTime',
    body: 'One connection to a server that already collects punches from many terminals.',
  },
  {
    kind: 'device',
    title: 'Standalone device',
    example: 'e.g. a ZKTeco terminal on your network',
    body: 'Connect straight to one terminal by its IP address — no server in between.',
  },
];
const PROVIDER_LABEL = { zk_device: 'ZKTeco protocol' };

async function renderBiometric(mount, route) {
  mount.innerHTML = loading();
  const [sources, devices, allProviders] = await Promise.all([
    api.get('/sources'),
    api.get('/devices').catch(() => []),
    api.get('/providers').catch(() => []),
  ]);
  const readonly = !auth.canWrite;
  // Only offer what fits the kind picked — a standalone-device protocol has no
  // business in the platform form, and vice versa. See AttendanceProvider.kinds.
  // Providers built only for this kind come first, so a standalone device
  // defaults to the device protocol rather than to BioTime's device mode.
  const providersFor = (kind) => allProviders
    .filter((p) => (p.kinds || ['platform', 'device']).includes(kind))
    .sort((a, b) => (a.kinds?.length || 2) - (b.kinds?.length || 2));

  // Providers that can create a user on the device — "Import terminals"
  // also creates missing Odoo employees there for these (see
  // app/services/provisioning.py for the rule).
  const canProvision = new Set(allProviders
    .filter((p) => (p.capabilities || []).includes('read_employees')
      && (p.capabilities || []).includes('write_employees'))
    .map((p) => p.slug));

  const devicesBySource = {};
  devices.forEach((d) => { (devicesBySource[d.source_id] ||= []).push(d); });

  const actions = readonly ? '' : `
    <button type="button" class="primary" id="addConnection">+ Add connection</button>`;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      ${cardHead('Biometric', 'where punches come from', actions)}


      ${sources.map((s) => sourceCard(s, devicesBySource[s.id] || [], readonly, canProvision.has(s.provider))).join('')}
      ${!sources.length ? empty(
        'No biometric connections yet',
        readonly ? '' : 'Use “+ Add connection” above to connect a platform server or a device.'
      ) : ''}
    </div>`;

  wireBiometric(mount, providersFor);

  // #/settings/biometric?add=1 — the Overview's setup checklist links here
  // to open the wizard straight away. Dropped from the address afterwards so
  // a reload does not open it again.
  if (route?.query?.add && auth.canWrite) {
    history.replaceState(null, '', '#/settings/biometric');
    openAddWizard({ providersFor, onDone: () => renderBiometric(mount) });
  }
}

/* ===========================================================================
 * The add-connection wizard: a modal <dialog> in three steps.
 *
 *   1 Type     platform server or standalone device (a click moves on)
 *   2 Details  the connection's fields, for the protocol picked
 *   3 Connect  the test runs by itself on arrival; Connect unlocks once the
 *              values have been tested, and reads "… anyway" after a failure
 *
 * Lives on <body>, outside the settings page, so nothing the page re-renders
 * can close it mid-way, and the browser's own modal handling gives focus
 * trapping and Esc-to-close for free. Nothing is saved until Connect.
 * ======================================================================== */

function openAddWizard({ providersFor, onDone }) {
  document.querySelector('dialog.wizard')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard';
  dialog.setAttribute('aria-labelledby', 'wizTitle');
  document.body.append(dialog);

  const state = {
    step: 1,
    kind: null,
    provider: null,
    values: {},        // what step 2 holds, kept across Back/Next
    tested: null,      // { fingerprint, result }
  };
  const STEPS = ['Type', 'Details', 'Test & connect'];

  const providers = () => providersFor(state.kind);
  const isDevice = () => state.kind === 'device';
  const commitLabel = () => (isDevice() ? 'Connect device' : 'Connect platform');

  /** Step 2's values shaped the way the API takes them. */
  const payload = () => {
    const values = { ...state.values };
    values.connection_kind = state.kind;
    values.provider = state.provider;
    if (state.provider === 'zk_device' && values.base_url && !/^zk:\/\//i.test(values.base_url)) {
      values.base_url = `zk://${values.base_url}`;
    }
    return values;
  };
  const fingerprint = () => JSON.stringify(payload());

  function close() {
    dialog.close();
    dialog.remove();
  }

  function stepper() {
    return `
      <ol class="wiz-steps">
        ${STEPS.map((label, i) => {
          const n = i + 1;
          const cls = n === state.step ? 'current' : n < state.step ? 'done' : '';
          return `<li class="${cls}"><b>${n < state.step ? '✓' : n}</b>${esc(label)}</li>`;
        }).join('')}
      </ol>`;
  }

  function bodyHtml() {
    if (state.step === 1) {
      return `
        <p class="hint" style="margin:0 0 12px">What are you connecting?</p>
        <div class="choice-grid">
          ${KIND_CHOICES.map((c) => `
            <button type="button" class="choice${state.kind === c.kind ? ' picked' : ''}" data-choose-kind="${esc(c.kind)}">
              <span class="choice-icon" aria-hidden="true">${KIND_ICON[c.kind]}</span>
              <span class="choice-title">${esc(c.title)}</span>
              <span class="choice-example">${esc(c.example)}</span>
              <span class="choice-body">${esc(c.body)}</span>
            </button>`).join('')}
        </div>`;
    }
    if (state.step === 2) {
      return `
        <form id="wizForm" novalidate>
          ${sourceFieldsHtml(null, state.kind, providers(), state.provider)}
        </form>`;
    }
    const v = payload();
    const current = state.tested && state.tested.fingerprint === fingerprint();
    const rows = [
      ['Type', isDevice() ? 'Standalone device' : 'Platform server'],
      ['Protocol', providers().find((p) => p.slug === state.provider)?.label || state.provider],
      ['Name', v.name],
      [isDevice() ? 'Device address' : 'Server URL', v.base_url],
      ...(v.username ? [['Username', v.username]] : []),
      ['Timezone', v.server_timezone],
    ];
    return `
      <dl class="wiz-summary">
        ${rows.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val || '—')}</dd>`).join('')}
      </dl>
      <div id="wizResult">${current && state.tested.result.blocked
        ? `<div class="test-result bad"><strong>Already connected</strong><span>${esc(state.tested.result.message)}</span>
             <span class="hint">Go back and enter a different address, or close this and edit the existing connection.</span></div>`
        : current
        ? testResultHtml(state.tested.result, false)
        : '<div class="test-result pending"><strong>Testing the connection…</strong><span>Nothing is saved yet.</span></div>'}</div>`;
  }

  function footHtml() {
    const back = state.step > 1
      ? '<button type="button" class="link" data-wiz="back">&larr; Back</button>'
      : '<button type="button" class="link" data-wiz="cancel">Cancel</button>';
    if (state.step === 1) return `${back}<span></span>`;
    if (state.step === 2) {
      return `${back}<button type="submit" form="wizForm" class="primary" data-wiz="next">Next: test &rarr;</button>`;
    }
    const current = state.tested && state.tested.fingerprint === fingerprint();
    const ok = current && state.tested.result.ok;
    const blocked = current && state.tested.result.blocked;
    return `${back}
      <div class="actions">
        <button type="button" data-wiz="test" ${current ? '' : 'disabled'}>Test again</button>
        <button type="button" class="primary" data-wiz="connect" ${current && !blocked ? '' : 'disabled'}>
          ${esc(blocked ? 'Already connected' : current && !ok ? `${commitLabel()} anyway` : commitLabel())}</button>
      </div>`;
  }

  function render() {
    dialog.innerHTML = `
      <div class="wiz-head">
        <strong id="wizTitle">Add a biometric connection</strong>
        <button type="button" class="link wiz-x" data-wiz="cancel" aria-label="Close">&times;</button>
      </div>
      ${stepper()}
      <div class="wiz-body">${bodyHtml()}<p class="err" id="wizError"></p></div>
      <div class="wiz-foot">${footHtml()}</div>`;
    wire();
    if (state.step === 2) restoreValues();
    if (state.step === 3 && !(state.tested && state.tested.fingerprint === fingerprint())) runTest();
  }

  function restoreValues() {
    const form = $('#wizForm', dialog);
    Object.entries(state.values).forEach(([k, v]) => {
      const el = form.querySelector(`[name="${k}"]`);
      if (el && k !== 'provider') el.value = v ?? '';
    });
    (form.querySelector('input:not([type=hidden])') || form).focus();
  }

  async function runTest() {
    const fp = fingerprint();
    const { name: _n, connection_kind: _k, ...values } = payload();
    let result;
    try {
      result = await api.post('/sources/test', values);
    } catch (error) {
      if (error.status === 401) return;
      // 409: this address is already one of this account's connections. Not
      // something "Connect anyway" should get past — the server refuses it.
      result = {
        ok: false,
        blocked: error.status === 409,
        message: error.message || 'The test could not run.',
      };
    }
    if (!dialog.isConnected || state.step !== 3) return;
    state.tested = { fingerprint: fp, result };
    render();
  }

  function wire() {
    dialog.querySelectorAll('[data-wiz=cancel]').forEach((b) => b.addEventListener('click', close));
    dialog.querySelector('[data-wiz=back]')?.addEventListener('click', () => {
      if (state.step === 2) state.values = readForm($('#wizForm', dialog));
      state.step -= 1;
      render();
    });
    dialog.querySelectorAll('[data-choose-kind]').forEach((b) => b.addEventListener('click', () => {
      if (state.kind !== b.dataset.chooseKind) {
        state.kind = b.dataset.chooseKind;
        state.provider = providers()[0]?.slug || 'biotime';
        state.values = {};
        state.tested = null;
      }
      state.step = 2;
      render();
    }));

    const form = $('#wizForm', dialog);
    if (form) {
      form.querySelector('select[name=provider]')?.addEventListener('change', (event) => {
        state.values = readForm(form);
        state.provider = event.target.value;
        render();
      });
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        state.values = readForm(form);
        state.step = 3;
        render();
      });
    }

    dialog.querySelector('[data-wiz=test]')?.addEventListener('click', () => {
      state.tested = null;
      render();
    });
    // Errors are shown in the wizard, not as a toast: the modal sits above
    // everything on the page, toasts included.
    dialog.querySelector('[data-wiz=connect]')?.addEventListener('click', (event) =>
      busy(event.target, async () => {
        try {
          await api.post('/sources', payload());
        } catch (error) {
          if (error.status !== 401) $('#wizError', dialog).textContent = error.message || 'Could not connect';
          return;
        }
        close();
        toast('Connection added', 'ok');
        await onDone();
      })
    );
  }

  // Esc and the backdrop both mean "cancel" — nothing has been saved.
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) close();
  });

  render();
  dialog.showModal();
  return dialog;
}

const KIND_ICON = {
  // A server stack, and a single terminal — drawn inline, no assets.
  platform: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="4" y="3.5" width="16" height="7" rx="1.5"/><rect x="4" y="13.5" width="16" height="7" rx="1.5"/><path d="M8 7h.01M8 17h.01M12 7h4M12 17h4"/></svg>`,
  device: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="6" y="2.5" width="12" height="19" rx="2"/><rect x="8.5" y="5.5" width="7" height="5" rx="1"/><circle cx="12" cy="15.5" r="2.2"/></svg>`,
};

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
  const kindLabel = source.connection_kind === 'device' ? 'Standalone device' : 'Platform server';
  const providerLabel = PROVIDER_LABEL[source.provider];
  const isEditing = editingSourceId === source.id;
  const isConfirming = confirmDeleteId === source.id;

  return `
    <div class="card" style="margin-bottom:14px" data-source="${esc(source.id)}">
      <div class="item-head">
        <div style="min-width:0">
          <div class="row" style="gap:8px;align-items:center">
            <strong>${esc(source.name)}</strong>
            ${pill(source.status)}
            <span class="pill mute">${esc(kindLabel)}</span>
            ${providerLabel ? `<span class="pill mute">${esc(providerLabel)}</span>` : ''}
          </div>
          <div class="hint mono" style="margin-top:2px">${esc(source.base_url)}</div>
          <div class="hint">
            checked ${esc(fmtAgo(source.last_checked_at))}
            · ${devices.length} terminal${devices.length === 1 ? '' : 's'}
          </div>
        </div>
        ${!readonly ? `
          <div class="actions">
            ${!isConfirming ? `<button type="button" class="sm link" data-remove="${esc(source.id)}">Remove</button>` : ''}
            <button type="button" class="sm" data-edit="${esc(source.id)}">${isEditing ? 'Close' : 'Edit'}</button>
            <button type="button" class="sm" data-test="${esc(source.id)}">Test connection</button>
            ${auth.canWrite && source.is_active !== false ? `<button type="button" class="sm primary" data-sync-source="${esc(source.id)}"
                    title="Pull this connection's punches now and push them to Odoo">Sync now</button>` : ''}
            <button type="button" class="sm" data-discover="${esc(source.id)}"
                    ${canProvision ? 'data-provision="1" title="Also creates Odoo employees who have a Badge ID or PIN and aren\'t on the device yet."' : ''}>Import terminals</button>
          </div>` : ''}
      </div>
      ${!readonly && isConfirming ? `
        <div class="row confirm-row">
          <span class="hint">Remove this connection? Its terminals go with it.</span>
          <button type="button" class="sm danger" data-remove-commit="${esc(source.id)}">Remove</button>
          <button type="button" class="sm link" data-remove-cancel="${esc(source.id)}">Cancel</button>
        </div>` : ''}
      ${source.status_message ? banner('Last error', source.status_message, 'bad') : ''}
      ${!readonly && isEditing ? sourceFormHtml(source, source.connection_kind, []) : ''}

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
  const commitLabel = source ? 'Save changes' : isDevice ? 'Connect device' : 'Connect platform';

  return `
    <form class="sourceForm" data-kind="${esc(kind)}" data-provider="${esc(provider)}"
          data-label="${esc(commitLabel)}"
          ${source ? `data-editing="${esc(source.id)}"` : ''}>
      <div class="form-head">
        <strong>${esc(source ? `Edit ${source.name}` : isDevice ? 'New standalone device' : 'New platform server')}</strong>
        <div class="actions">
          <button class="sm link" type="button" data-cancel-form="1">Cancel</button>
          <button class="sm" type="button" data-test-form="1">Test connection</button>
          <button class="primary sm" type="submit" data-commit="1">${esc(commitLabel)}</button>
        </div>
      </div>
      ${source ? '' : testFirstHint(commitLabel)}
      <div class="form-test-result"></div>
      ${sourceFieldsHtml(source, kind, providers, provider)}
    </form>`;
}

/** The connection's own fields — shared by the inline edit form and the
 * add-connection wizard. The field set follows the provider: a standalone
 * ZKTeco device has an address and an optional comm key, no username. */
function sourceFieldsHtml(source, kind, providers, provider) {
  const isDevice = kind === 'device';
  const isZk = provider === 'zk_device';
  const addressValue = source
    ? (isZk ? source.base_url.replace(/^zk:\/\//i, '') : source.base_url)
    : '';
  return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: isDevice ? 'Protocol' : 'Platform', required: true, value: provider,
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
`;
}

/** Sync now for one connection — shared with the Terminals page. */
export async function syncSource(sourceId) {
  try {
    const run = await api.post(`/sources/${sourceId}/sync`);
    toast(run.status === 'failed'
      ? `Sync failed: ${run.error_message || 'see Activity'}`
      : `Synced — ${run.punches_new} new punch${run.punches_new === 1 ? '' : 'es'}, `
        + `${run.attendances_created} created, ${run.attendances_closed} closed`,
    run.status === 'failed' ? 'bad' : 'ok');
    return run;
  } catch (error) {
    if (error.status !== 401) toast(error.message || 'Sync failed', 'bad');
    return null;
  }
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

function wireBiometric(mount, providersFor) {
  $('#addConnection', mount)?.addEventListener('click', () => {
    openAddWizard({ providersFor, onDone: () => renderBiometric(mount) });
  });

  mount.querySelectorAll('[data-edit]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.edit;
      editingSourceId = editingSourceId === id ? null : id;
      renderBiometric(mount);
    });
  });

  mount.querySelectorAll('[data-cancel-form]').forEach((button) => {
    button.addEventListener('click', () => {
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

  mount.querySelectorAll('[data-sync-source]').forEach((button) => {
    button.addEventListener('click', () =>
      busy(button, async () => {
        await syncSource(button.dataset.syncSource);
        await renderBiometric(mount);
      })
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
    const editing = form.dataset.editing;

    /** What the form holds, shaped the way the API takes it — shared by
     * Connect and by Test, so the test is of exactly what would be saved. */
    const sourceValues = () => {
      const values = readForm(form);
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
      return values;
    };

    const commit = form.querySelector('[data-commit]');
    wireTestFirst({
      form,
      test: form.querySelector('[data-test-form]'),
      commit,
      result: form.querySelector('.form-test-result'),
      label: form.dataset.label,
      gate: !editing,
      probe: () => {
        const { name, connection_kind: _kind, auto_provision_employees: _p, ...values } = sourceValues();
        return api.post('/sources/test', {
          ...values,
          ...(editing ? { source_id: editing } : { provider: values.provider || form.dataset.provider }),
        });
      },
    });

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const values = sourceValues();
      busy(commit, () =>
        guard(async () => {
          if (editing) {
            await api.patch(`/sources/${editing}`, values);
            // As with Odoo: re-check at once, so the status shown is about
            // the values just saved rather than "unverified".
            const result = await api.post(`/sources/${editing}/test`);
            toast(result.message, result.ok ? 'ok' : 'bad');
          } else {
            await api.post('/sources', values);
            toast('Connection added', 'ok');
          }
          editingSourceId = null;
          await renderBiometric(mount);
        })
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
