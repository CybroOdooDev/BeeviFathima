/* Settings: tenant configuration, split into submenus.
 *
 * General / Pairing / Plan are the account's own rules —
 * previously one long scrolling form, now one submenu each so a change to
 * pairing does not require scrolling past other settings to find it. Odoo and
 * Biometric absorb what used to be the standalone "Connections" page: where
 * attendance is written, and what it is written from.
 */

import { api, auth } from '../api.js';
import { openDeleteAccountWizard, openPasswordWizard } from './account.js';
import { render as renderBilling } from './billing.js';
import { needsMatch, unmappedCard, wireUnmapped } from './data.js';
import {
  $, $$, banner, busy, empty, esc, field as baseField, INFO_ICON, fmtAgo, fmtIn, guard, loading, pill, pricingCards, readForm,
  timezoneNames, toast, wirePricingCards,
} from '../ui.js';
import { setGuard } from '../nav-guard.js';

/* Every settings form shows its field help as an (i) beside the label,
 * on hover / focus, rather than a paragraph under every box. */
const field = (options) => baseField({ tip: true, ...options });

/* Settings is the one configuration menu: the account's own rules, then the
 * two connections — Odoo (where attendance goes, and the badges still waiting
 * for an Odoo employee) and Biometric (where punches come from). Each
 * submenu has its own entry in the sidebar's Configuration section — see NAV
 * in app.js — so there is nothing to switch between in-page here. */
const RENDERERS = {
  general: renderGeneral,
  pairing: renderPairing,
  billing: renderBillingPage,
  'billing/choose': renderChoosePlan,
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
    body.innerHTML = empty('Not Found', 'That settings page does not exist.');
    return;
  }
  await renderSection(body, route);
}

/** Settings → Billing: the plan this account is on, then how it is paid for —
 * renewals, card and invoices. One submenu; the two halves paint on their own. */
async function renderBillingPage(mount, route) {
  mount.innerHTML = '<div id="planPart"></div><div id="billPart" style="margin-top:14px"></div>';
  await Promise.all([
    renderPlan($('#planPart', mount), route),
    renderBilling($('#billPart', mount), route),
  ]);
}

/* ===========================================================================
 * General / Pairing / Plan — the account's own rules.
 * Each submenu is its own <form>, so saving one never touches another.
 * ======================================================================== */

async function tenantContext() {
  // The schedule's live state comes with the dashboard, so the interval
  // field can say what is actually happening rather than "if a worker is
  // running". Plans come from the same public list the signup picker uses —
  // best-effort, since a failed fetch should still leave the rest usable.
  const [tenant, dash, plans, billing] = await Promise.all([
    api.get('/tenant'),
    api.get('/dashboard').catch(() => null),
    api.get('/auth/plans').catch(() => []),
    api.get('/billing').catch(() => ({ enabled: false })),
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
    billing,
    // Choosing a plan means paying for it first, on Stripe's own page.
    paysAtCheckout: Boolean(billing?.enabled) && !tenant.billed_by_stripe,
  };
}

function topBanners(ctx) {
  return `
    ${ctx.stopped ? banner(
      'Syncing Is Stopped For This Account',
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
  readonly ? '' : '<button class="primary" id="save" type="submit" hidden>Save Settings</button>';

/** Tell the router this form has unsaved changes whenever it differs from
 * what was on screen when it was drawn, so leaving the page can ask first.
 * ``save`` persists the form and throws on failure; ``touched`` is for state
 * the form's own fields do not carry. */
function trackDirty(form, save, touched = () => false) {
  if (!auth.canWrite || !form) return;
  const baseline = JSON.stringify(readForm(form));
  setGuard({
    isDirty: () => form.isConnected && (JSON.stringify(readForm(form)) !== baseline || touched()),
    save: async () => {
      if (!form.reportValidity()) return false;
      try {
        await save();
        toast('Settings saved', 'ok');
        return true;
      } catch (error) {
        if (error.status !== 401) toast(error.message || 'Could not save', 'bad');
        return false;
      }
    },
  });
}

function saveTenantForm(mount, formId, buttonId, reRender) {
  if (!auth.canWrite) return;
  const form = $(`#${formId}`, mount);
  trackDirty(form, () => api.patch('/tenant', readForm(form)));
  // Save Settings only appears once the form differs from what was loaded.
  const saveBtn = $(`#${buttonId}`, mount);
  if (saveBtn) {
    const baseline = JSON.stringify(readForm(form));
    const sync = () => { saveBtn.hidden = JSON.stringify(readForm(form)) === baseline; };
    form.addEventListener('input', sync);
    form.addEventListener('change', sync);
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    busy($(`#${buttonId}`, mount), () =>
      guard(async () => {
        await api.patch('/tenant', values);
        await reRender(mount);
      }, 'Settings Saved')
    );
  });
}

const infoTip = (text, strong = false) =>
  `<span class="field-tip${strong ? ' strong' : ''}" tabindex="0" role="note" aria-label="${esc(text)}" data-tip="${esc(text)}">${INFO_ICON}</span>`;

async function renderGeneral(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, readonly, intervalHelp, schedule, floor } = ctx;
  const intervalWarn = Boolean(schedule && !schedule.running) || tenant.syncable === false;
  const intervalPresets = [5, 15, 30, 60].filter((m) => !floor || m >= floor);

  const accountButtons = '<button type="button" id="pwOpen">Change User Password</button>'
    + (auth.user?.role === 'owner'
      ? `<button type="button" class="danger-outline" id="delAccount" title="Permanently delete this account and everything BioBridge holds for it">${TRASH_ICON} Delete Account</button>` : '');

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      <form id="form" ${readonly ? 'inert' : ''}>
        ${cardHead('General', '', accountButtons + saveButton(readonly))}
        ${field({ name: 'name', label: 'Company', value: tenant.name, required: true })}
        ${field({
          name: 'timezone', label: 'Timezone', value: tenant.timezone, required: true,
          help: 'Used to render attendance for your team. Separate from each biometric connection’s own device timezone.',
          datalist: timezoneNames(), tip: true,
        })}
        <div class="field" id="intervalField">
          <label id="intervalLabel">Sync Every ${infoTip(intervalHelp, intervalWarn)}</label>
          <div class="chips" role="radiogroup" aria-labelledby="intervalLabel">
            ${intervalPresets.map((m) => `<button type="button" class="chip" role="radio" data-min="${m}">${m < 60 ? `${m} min` : `${m / 60} hr`}</button>`).join('')}
            <button type="button" class="chip" role="radio" data-min="custom">Custom</button>
          </div>
          <input type="number" name="sync_interval_minutes" id="sync_interval_minutes" class="chip-custom"
                 min="${floor || 1}" max="1440" value="${esc(tenant.sync_interval_minutes)}" aria-label="Minutes between syncs" hidden>
          ${intervalWarn ? `<div class="help strong">${esc(intervalHelp)}</div>` : ''}
        </div>
        <div class="field-pair">
        <div class="field">
          <label for="sync_enabled">Automatic Sync ${infoTip('When on, BioBridge pulls punches on the interval above (live scheduler). When off, it only syncs when someone clicks Sync Now (manual). Nothing is lost either way: the next sync picks up where the last one stopped.')}</label>
          <label class="switch-row">
            <span class="switch">
              <input type="checkbox" name="sync_enabled" id="sync_enabled" role="switch" ${tenant.sync_enabled ? 'checked' : ''}>
              <span class="switch-track" aria-hidden="true"></span>
            </span>
            <span class="switch-text" id="syncState"></span>
          </label>
        </div>
        <div class="field">
          <label for="alert_emails_enabled">Alert Emails ${infoTip('Sent to the owner and admins when a serious alert (like Odoo rejecting the API key) has lasted about 15 minutes, then once a day while it is unresolved. When off, alerts show in the app only.')}</label>
          <label class="switch-row">
            <span class="switch">
              <input type="checkbox" name="alert_emails_enabled" id="alert_emails_enabled" role="switch" ${tenant.alert_emails_enabled !== false ? 'checked' : ''}>
              <span class="switch-track" aria-hidden="true"></span>
            </span>
            <span class="switch-text" id="alertState"></span>
          </label>
        </div>
        </div>
      </form>
    </div>
    `;

  const form = $('#form', mount);
  const syncBox = $('#sync_enabled', mount);
  const syncState = $('#syncState', mount);
  const paintSync = () => {
    syncState.textContent = syncBox.checked ? 'On — live scheduler' : 'Off — manual only';
  };
  paintSync();
  syncBox.addEventListener('change', paintSync);
  // Sync Every: preset chips plus a Custom number box; greyed out while
  // Automatic Sync is off, since the interval then does nothing.
  const intervalInput = $('#sync_interval_minutes', mount);
  const intervalField = $('#intervalField', mount);
  const chips = [...intervalField.querySelectorAll('.chip')];
  let customOpen = !intervalPresets.includes(Number(intervalInput.value));
  const paintInterval = () => {
    const current = Number(intervalInput.value);
    chips.forEach((chip) => {
      const on = chip.dataset.min === 'custom' ? customOpen : (!customOpen && Number(chip.dataset.min) === current);
      chip.classList.toggle('on', on);
      chip.setAttribute('aria-checked', String(on));
    });
    intervalInput.hidden = !customOpen;
    const off = !syncBox.checked;
    intervalField.classList.toggle('is-off', off);
    chips.forEach((chip) => { chip.disabled = off; });
    intervalInput.disabled = off;
  };
  chips.forEach((chip) => chip.addEventListener('click', () => {
    if (chip.dataset.min === 'custom') {
      customOpen = true;
      paintInterval();
      intervalInput.focus();
    } else {
      customOpen = false;
      intervalInput.value = chip.dataset.min;
      paintInterval();
      form.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }));
  intervalInput.addEventListener('input', paintInterval);
  syncBox.addEventListener('change', paintInterval);
  paintInterval();

  const alertBox = $('#alert_emails_enabled', mount);
  const alertState = $('#alertState', mount);
  const paintAlert = () => {
    alertState.textContent = alertBox.checked ? 'On — email owner and admins' : 'Off — in-app alerts only';
  };
  paintAlert();
  alertBox.addEventListener('change', paintAlert);

  saveTenantForm(mount, 'form', 'save', renderGeneral);
  $('#pwOpen', mount).addEventListener('click', openPasswordWizard);
  $('#delAccount', mount)?.addEventListener('click', () =>
    guard(() => openDeleteAccountWizard({ companyName: tenant.name, billedByStripe: tenant.billed_by_stripe })));
}

/** A number setting as preset chips plus a Custom box. The real input
 * (name=``name``) always carries the value, so readForm and the dirty check
 * work unchanged; chips just set it. */
function chipField({ name, label, help, presets, value, min, max, unit, fmt }) {
  const chipLabel = fmt || ((m) => `${m} ${unit}`);
  return `
    <div class="field" data-chipfield="${esc(name)}">
      <label id="${esc(name)}Label">${esc(label)} ${help ? infoTip(help) : ''}</label>
      <div class="chips" role="radiogroup" aria-labelledby="${esc(name)}Label">
        ${presets.map((m) => `<button type="button" class="chip" role="radio" data-min="${m}">${esc(chipLabel(m))}</button>`).join('')}
        <button type="button" class="chip" role="radio" data-min="custom">Custom</button>
      </div>
      <input type="number" name="${esc(name)}" id="${esc(name)}" class="chip-custom"
             min="${min}" max="${max}" value="${esc(value)}" aria-label="${esc(label)}" hidden>
    </div>`;
}

function wireChipField(mount, form, name, presets) {
  const box = mount.querySelector(`[data-chipfield="${name}"]`);
  if (!box) return;
  const input = box.querySelector('input');
  const chips = [...box.querySelectorAll('.chip')];
  let customOpen = !presets.includes(Number(input.value));
  const paint = () => {
    const current = Number(input.value);
    chips.forEach((chip) => {
      const on = chip.dataset.min === 'custom' ? customOpen : (!customOpen && Number(chip.dataset.min) === current);
      chip.classList.toggle('on', on);
      chip.setAttribute('aria-checked', String(on));
    });
    input.hidden = !customOpen;
  };
  chips.forEach((chip) => chip.addEventListener('click', () => {
    if (chip.dataset.min === 'custom') {
      customOpen = true;
      paint();
      input.focus();
    } else {
      customOpen = false;
      input.value = chip.dataset.min;
      paint();
      form.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }));
  input.addEventListener('input', paint);
  paint();
}

async function renderPairing(mount) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, readonly } = ctx;

  mount.innerHTML = `
    ${topBanners(ctx)}
    <form id="form" ${readonly ? 'inert' : ''}>
      <div class="card">
        ${cardHead('Pairing', '', saveButton(readonly))}
        ${field({
          name: 'pairing_mode', label: 'Mode', value: tenant.pairing_mode, required: true,
          options: [
            { value: 'alternating', label: 'Alternating — In, Out, In, Out' },
            { value: 'state_based', label: 'State Based — Trust The Device' },
            { value: 'first_last', label: 'First / last — first in, last out' },
          ],
          help: 'Alternating suits devices with no IN/OUT keys, which is most of the field. State based needs those keys configured correctly; it falls back automatically when a device stamps everything "Check In".',
        })}
        ${chipField({
          name: 'min_punch_interval_seconds', label: 'Ignore Repeat Punches Within',
          value: tenant.min_punch_interval_seconds, presets: [0, 30, 60, 120, 300], min: 0, max: 3600,
          fmt: (m) => (m === 0 ? 'Off' : m < 60 ? `${m} sec` : `${m / 60} min`),
          help: 'Drops double-taps within this time. A punch in the opposite direction is always kept — "in then straight out" is a real, if brief, visit.',
        })}
        ${chipField({
          name: 'max_shift_hours', label: 'Maximum Shift Length',
          value: tenant.max_shift_hours, presets: [8, 12, 16, 24], min: 1, max: 48, unit: 'hr',
          help: 'Anything longer is capped and flagged, so one forgotten badge-out cannot write a 300-hour attendance.',
        })}
        <div id="dayBoundary" ${tenant.pairing_mode === 'first_last' ? '' : 'hidden'}>
        ${field({
          name: 'day_boundary_hour', label: 'Shift Day Starts At (Hour)', type: 'number',
          value: tenant.day_boundary_hour, required: true,
          help: 'Decides which calendar day a punch belongs to. A night-shift site sets this after the shift ends — 12 for noon, not the small hours.',
        })}
        </div>
        ${field({
          name: 'orphan_out_policy', label: 'Check-Out With No Check-In',
          value: tenant.orphan_out_policy, required: true,
          options: [
            { value: 'flag', label: 'Flag — write a zero-length record for review' },
            { value: 'create', label: 'Create — infer a check-in 8 hours earlier' },
            { value: 'ignore', label: 'Ignore — Drop It' },
          ],
          help: 'What to do with a check-out that has no matching check-in: flag it for review, infer a check-in 8 hours earlier, or drop it.',
        })}
      </div>
    </form>`;

  // Only first/last mode groups punches by day, so only it needs a day boundary.
  const modeSel = mount.querySelector('[name=pairing_mode]');
  const dayBox = mount.querySelector('#dayBoundary');
  modeSel?.addEventListener('change', () => { dayBox.hidden = modeSel.value !== 'first_last'; });

  const pairForm = mount.querySelector('#form');
  wireChipField(mount, pairForm, 'min_punch_interval_seconds', [0, 30, 60, 120, 300]);
  wireChipField(mount, pairForm, 'max_shift_hours', [8, 12, 16, 24]);

  saveTenantForm(mount, 'form', 'save', renderPairing);
}

async function renderPlan(mount, route) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, activePlans, renewalWarning, readonly, billing } = ctx;
  // Back from Stripe Checkout. The payment is confirmed to BioBridge by a
  // webhook, usually within seconds of this redirect — so wait for it here
  // rather than show the old plan as if nothing happened.
  const justPaid = route?.query?.checkout === 'success';
  if (justPaid) history.replaceState(null, '', '#/settings/billing');

  // A plan retired since this account chose it is no longer offered, but it is
  // still what they are on — say so rather than showing no card as current.
  const retired = tenant.plan_id && !activePlans.some((p) => p.id === tenant.plan_id);

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      ${cardHead('Plan', 'what this account is billed and limited by',
        `${activePlans.length && !readonly
          ? `<a class="btn primary-link" href="#/settings/billing/choose">${tenant.plan_id && !ctx.paysAtCheckout ? 'Change Plan' : 'Choose A Plan'}</a>`
          : ''}`)}
      ${justPaid && !tenant.billed_by_stripe ? banner(
        'Payment received — activating your plan',
        'Stripe is confirming the payment with BioBridge. This page updates on its own in a few seconds.',
        '') : ''}
      ${tenant.billed_by_stripe ? '<div class="hint" style="margin-bottom:12px">Renews automatically every month through Stripe. Card, invoices, overdue payments and cancellation are just below.</div>' : ''}
      <div class="hint" style="margin-bottom:12px">
        ${tenant.plan_name ? `Currently <strong>${esc(tenant.plan_name)}</strong>` : 'No plan assigned — nothing is limited.'}
        ${tenant.plan_max_employees != null ? ` · up to ${esc(tenant.plan_max_employees)} employees` : ''}
        ${tenant.plan_max_devices != null ? ` · up to ${esc(tenant.plan_max_devices)} device${tenant.plan_max_devices === 1 ? '' : 's'}` : ''}
        ${tenant.plan_min_sync_interval_minutes ? ` · syncs no faster than every ${esc(tenant.plan_min_sync_interval_minutes)} min` : ''}
        ${tenant.subscription_renews_at ? ` · renews ${esc(fmtIn(tenant.subscription_renews_at))}` : ''}
      </div>
      ${tenant.pending_plan_name ? banner(
        `Switching to ${tenant.pending_plan_name}`,
        `Takes effect once the current plan's period ends (renews `
          + `${fmtIn(tenant.subscription_renews_at)}) — choose `
          + `${tenant.plan_name} again to cancel it.`,
        '', !readonly ? { href: '#/settings/billing/choose', label: 'Change' } : null) : ''}
      ${renewalWarning && (tenant.status === 'trialing' || tenant.plan_id) ? banner(
        tenant.status === 'trialing'
          ? (renewalWarning.days_left <= 0 ? 'Trial Ends Today'
              : `Trial ends in ${renewalWarning.days_left} day${renewalWarning.days_left === 1 ? '' : 's'}`)
          : (renewalWarning.days_left <= 0 ? 'Renews Today'
              : `Renews in ${renewalWarning.days_left} day${renewalWarning.days_left === 1 ? '' : 's'}`),
        tenant.status === 'trialing'
          ? 'Choose a plan to keep syncing once it ends.'
          : (tenant.billed_by_stripe
            ? 'Charged automatically to your saved card — see Billing to check or change it.'
            : 'Changing plans here does not change that date — contact support to renew.'),
        renewalWarning.urgent ? 'bad' : 'warn') : ''}
      ${retired ? banner(
        `${tenant.plan_name || 'Your plan'} is no longer offered`,
        'You stay on it until you choose another.',
        'warn', !readonly ? { href: '#/settings/billing/choose', label: 'Choose A Plan' } : null) : ''}
      ${!activePlans.length ? empty('No Plans Available', '') : ''}
      ${readonly ? '<div class="hint">Your role cannot change the plan.</div>' : ''}
      ${tenant.status === 'cancelled' ? banner(
        'This Plan Has Ended',
        'Your account, connections and history are kept, but nothing syncs. Choose a plan to start again, '
        + 'or delete the account under General if you are finished with BioBridge.',
        'warn') : ''}
      ${!readonly && !tenant.billed_by_stripe && ['trialing', 'active', 'past_due'].includes(tenant.status) ? `
        <div class="row" style="margin-top:14px">
          <button type="button" id="discontinuePlan">Discontinue Plan</button>
        </div>
        <div id="discontinueConfirm" hidden class="banner warn" style="margin-top:12px">
          <strong>Discontinue This Plan?</strong>
          Syncing stops right away. Your account, connections and history are kept; you can choose a plan again
          later, and the account is only removed if you or BioBridge staff delete it.
          <div class="row" style="margin-top:10px">
            <button type="button" class="primary sm" id="discontinueYes">Yes, Discontinue</button>
            <button type="button" class="sm" id="discontinueNo">Keep It</button>
          </div>
        </div>` : ''}
    </div>`;

  const dConfirm = mount.querySelector('#discontinueConfirm');
  mount.querySelector('#discontinuePlan')?.addEventListener('click', () => { dConfirm.hidden = false; });
  mount.querySelector('#discontinueNo')?.addEventListener('click', () => { dConfirm.hidden = true; });
  mount.querySelector('#discontinueYes')?.addEventListener('click', (e) => busy(e.currentTarget, async () => {
    if (await guard(() => api.post('/billing/cancel', { when: 'now' }),
      'Plan discontinued — syncing has stopped. Your account is kept.')) renderPlan(mount);
  }));


  if (justPaid && !tenant.billed_by_stripe) {
    for (let attempt = 0; attempt < 15; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!mount.isConnected) return;
      const fresh = await api.get('/tenant').catch(() => null);
      if (fresh?.billed_by_stripe) {
        toast(`You're on ${fresh.plan_name || 'your new plan'} — thank you!`, 'ok');
        await renderPlan(mount);
        return;
      }
    }
    toast('Payment is still being confirmed — refresh in a minute.', '');
  } else if (justPaid) {
    toast(`You're on ${tenant.plan_name || 'your new plan'} — thank you!`, 'ok');
  }
}

/** "Choose a plan" / "Change plan" from the card above — the full pricing
 * grid, one plan per card, reached only by someone who can already write to
 * this account (renderPlan hides the link otherwise, and this still checks
 * for a hand-typed URL). Picking one PATCHes /tenant directly: unlike the
 * public #/plans page, there is an account here for a click to act on. */
async function renderChoosePlan(mount, route) {
  mount.innerHTML = loading();
  const ctx = await tenantContext();
  const { tenant, activePlans, willDefer, readonly, paysAtCheckout } = ctx;
  if (route?.query?.checkout === 'cancelled') {
    history.replaceState(null, '', '#/settings/billing/choose');
    toast('Checkout cancelled — nothing was charged.', '');
  }

  if (readonly) {
    mount.innerHTML = `
      ${topBanners(ctx)}
      <div class="card">
        ${cardHead('Choose A Plan', '', '<a class="btn" href="#/settings/billing">&larr; Billing</a>')}
        <div class="hint">Your role cannot change the plan.</div>
      </div>`;
    return;
  }

  const tags = {};
  if (tenant.plan_id && !paysAtCheckout) {
    // Normally locked — nothing to do with the plan you're already on. The
    // one exception: a switch is already queued, in which case picking the
    // current plan again is how it gets cancelled, so that card stays live.
    tags[tenant.plan_id] = tenant.pending_plan_id
      ? { label: 'Current Plan', tone: 'current', locked: false, ctaLabel: 'Cancel Scheduled Switch' }
      : { label: 'Current Plan', tone: 'current' };
  }
  if (tenant.pending_plan_id) tags[tenant.pending_plan_id] = { label: 'Already Scheduled', tone: 'warn' };

  mount.innerHTML = `
    ${topBanners(ctx)}
    <div class="card">
      ${cardHead('Choose A Plan', '', '<a class="btn" href="#/settings/billing">&larr; Billing</a>')}
      <p class="hint" style="margin:-4px 0 16px">${paysAtCheckout
        ? 'Pick a plan to pay for it securely on Stripe. It starts as soon as the payment goes through, and renews monthly — cancel any time from Manage billing.'
        : tenant.billed_by_stripe && willDefer
        ? 'Your switch is queued for your next renewal — the next invoice is at the new plan’s price, and nothing is charged or refunded for this month.'
        : willDefer
        ? 'You’re on a paid plan already: switching here queues the change for '
          + 'your next renewal rather than applying it right away.'
        : 'Takes effect immediately.'} A downgrade never unmatches an employee
        already mapped — only new matches beyond the new cap are held
        back.${willDefer ? '' : ' Your sync interval is raised automatically '
        + 'if the new plan needs a slower one.'}</p>
      ${activePlans.length ? pricingCards({
        plans: activePlans, tags, showRecommended: false,
        ctaLabel: paysAtCheckout ? 'Continue To Payment' : willDefer ? 'Switch At Renewal' : 'Switch To This Plan',
      }) : empty('No Plans Available', '')}
    </div>`;

  if (!activePlans.length) return;

  const nameOf = (id) => activePlans.find((p) => p.id === id)?.name || 'this plan';
  wirePricingCards(mount, (planId, button) => {
    if (paysAtCheckout) {
      busy(button, () => guard(async () => {
        const { url } = await api.post('/billing/checkout', { plan_id: planId });
        window.location.href = url;
      }));
      return;
    }
    // Cancelling a scheduled switch means picking the current plan again —
    // that card is never locked even though it's tagged, so this still
    // needs its own message rather than the plain "Plan changed" default.
    const message = planId === tenant.plan_id
      ? (tenant.pending_plan_id ? 'Scheduled Change Cancelled' : 'Already On This Plan')
      : willDefer ? `Switch to ${nameOf(planId)} scheduled for your next renewal`
      : `Switched to ${nameOf(planId)}`;
    busy(button, () =>
      guard(async () => {
        await api.patch('/tenant', { plan_id: planId });
        window.location.hash = '#/settings/billing';
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
  const [odooList, mappings, sources] = await Promise.all([
    api.get('/odoo-connections'),
    api.get('/mappings?limit=500').catch(() => []),
    api.get('/sources').catch(() => []),
  ]);
  const odoo = odooList[0] || null;
  // Badges can only be matched once a biometric connection is working too.
  const biometricUp = sources.some((x) => x.status === 'connected');
  const readonly = !auth.canWrite;

  if (!odoo || odooLastCompanies?.connId !== odoo.id) odooLastCompanies = null;

  // Test sits before Connect, left to right, and on a new connection Connect
  // only unlocks once the values in the form have been tested — see
  // wireTestFirst. Remove is kept apart from both, on the far left.
  const inSetup = Boolean(mount.closest('#setupBody'));
  const removeControls = odoo && !readonly && !inSetup ? (
    odooConfirmDelete
      ? '<span class="hint">Remove this connection?</span>'
        + '<button type="button" class="sm danger" id="odooRemoveCommit">Remove</button>'
        + '<button type="button" class="sm link" id="odooRemoveCancel">Cancel</button>'
      : '<button type="button" class="sm link" id="odooRemove">Remove Connection</button>'
  ) : '';
  const actions = readonly ? '' : `
    ${removeControls}
    <button type="button" id="testOdoo">Test Connection</button>
    <button class="primary" id="saveOdoo" type="submit" form="odooForm">
      ${odoo ? 'Save Changes' : 'Connect Odoo'}</button>`;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      ${cardHead('Odoo', '', inSetup ? '' : actions)}
      ${!odoo && !readonly ? testFirstHint('Connect Odoo') : ''}
      <div id="odooTestResult"></div>
      <div id="odooStatus">${odoo ? statusRow(odoo) : ''}</div>
      <form id="odooForm" ${readonly ? 'inert' : ''}>
        ${field({
          name: 'url', label: 'Server URL', required: true, tip: true, strongHelp: true,
          value: odoo?.url || '', placeholder: 'https://acme.odoo.com',
          help: 'Just the address, without /odoo or /web on the end — the most common cause of a failed connection.',
        })}
        ${field({
          name: 'db_name', label: 'Database', required: true, tip: true, value: odoo?.db_name || '',
          help: 'On Odoo Online this is usually the subdomain.',
        })}
        ${field({ name: 'username', label: 'Login', required: true, value: odoo?.username || '' })}
        ${field({
          name: 'api_key', label: 'API Key', type: 'password', tip: true,
          required: true, noRequiredAttr: !!odoo,
          placeholder: odoo ? 'unchanged' : '',
          help: odoo
            ? 'Stored encrypted and never shown again. Leave blank to keep the current one.'
            : 'Odoo → Preferences → Account Security → New API Key.',
        })}
        <div id="companyField">${companyFieldHtml(odoo, odooLastCompanies?.companies, savedDisabled(odoo, odooLastCompanies?.companies))}</div>
      </form>
      ${inSetup && actions ? `<div class="setup-foot actions">${actions}</div>` : ''}
    </div>
    ${odoo && biometricUp && mappings.some(needsMatch) ? unmappedCard(mappings.filter(needsMatch)) : ''}`;

  if (odoo && biometricUp) wireUnmapped(mount, () => renderOdoo(mount));
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
    // The company switches are checkboxes without a name, so readForm does not
    // see them. Sent only while the list is on screen: before Odoo has been
    // reached there is nothing to switch, and an absent field leaves what is
    // saved alone.
    delete values.company_id;
    const boxes = [...mount.querySelectorAll('[data-company]')];
    if (boxes.length) {
      values.company_id = null; // retires the older single-company pin
      values.disabled_company_ids = boxes.filter((b) => !b.checked).map((b) => Number(b.dataset.company));
    }
    return values;
  };

  // Repaint just the company list with a fresh set of companies, keeping
  // whatever is switched off right now (which may be an unsaved change).
  const paintCompanies = (companies) => {
    const box = $('#companyField', mount);
    if (!box) return;
    const shown = [...box.querySelectorAll('[data-company]')];
    const off = shown.length
      ? new Set(shown.filter((b) => !b.checked).map((b) => b.dataset.company))
      : savedDisabled(odoo, companies);
    box.innerHTML = companyFieldHtml(odoo, companies, off);
    // The status line under the pills depends on how many companies there are.
    const extras = $('#odooStatusExtras', mount);
    if (extras) extras.innerHTML = trackingExtrasHtml(odoo, companies);
  };
  // At least one company has to stay on — an empty list would mean "nothing
  // to sync", not "everything".
  $('#companyField', mount)?.addEventListener('change', (event) => {
    const box = event.target.closest('[data-company]');
    if (!box) return;
    if (!mount.querySelector('[data-company]:checked')) {
      box.checked = true;
      toast('At least one company has to stay on.', 'bad');
    }
    updateCompanyCount(mount);
  });
  // A saved connection fills its list on its own, in the background — the
  // page never waits on Odoo to render. Quiet on failure: the saved choice
  // is still shown, and Test connection reports what is actually wrong.
  if (odoo && !odooLastCompanies) {
    api.get(`/odoo-connections/${odoo.id}/companies`)
      .then((companies) => {
        odooLastCompanies = { connId: odoo.id, companies };
        paintCompanies(companies);
      })
      .catch(() => {});
  }

  // The device-tracking rows live in their own container so a successful
  // Test connection can repaint just them — see the probe below — without
  // disturbing the rest of the form or the test-result panel next to it.
  const paintTrackingExtras = () => {
    const box = $('#odooStatusExtras', mount);
    if (box) box.innerHTML = trackingExtrasHtml(odoo, odooLastCompanies?.companies);
  };

  wireTestFirst({
    form,
    test: $('#testOdoo', mount),
    commit: $('#saveOdoo', mount),
    result: $('#odooTestResult', mount),
    label: odoo ? 'Save Changes' : 'Connect Odoo',
    ignore: ['company_id'],
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
      // A test that passes on the connection exactly as saved is also the
      // connection's own check: record it, so a "Degraded" or "Error" pill
      // goes back to "Connected" now, not only after Save. Edited values are
      // a different connection — the saved one's status is not theirs to set.
      const v = odooValues();
      const asSaved = odoo && !v.api_key && v.url === odoo.url
        && v.db_name === odoo.db_name && v.username === odoo.username;
      if (result.ok && asSaved) {
        try {
          const saved = await api.post(`/odoo-connections/${odoo.id}/test`);
          if (saved.ok) {
            const fresh = (await api.get('/odoo-connections')).find((c) => c.id === odoo.id);
            if (fresh) {
              Object.assign(odoo, fresh);
              const box = $('#odooStatus', mount);
              if (box) box.innerHTML = statusRow(odoo);
            }
          }
        } catch { /* the test itself passed; the pill catches up on the next load */ }
      }
      return result;
    },
    after: (result) => {
      if (!result.ok) {
        // A failed test hides the picker again — it only belongs to a
        // connection that works.
        const box = $('#companyField', mount);
        if (box) box.innerHTML = '';
        return '';
      }
      if (Array.isArray(result.detail?.companies)) {
        if (odoo) odooLastCompanies = { connId: odoo.id, companies: result.detail.companies };
        paintCompanies(result.detail.companies);
      }
      return '';
    },
  });

  // Shared by the Save button and by "Save" in the leave-this-page prompt.
  const persistOdoo = async () => {
    const values = odooValues();
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
  };

  let companiesTouched = false;
  $('#companyField', mount)?.addEventListener('change', () => { companiesTouched = true; });
  trackDirty(form, async () => {
    await persistOdoo();
  }, () => companiesTouched);

  // A saved connection offers Save changes only once something differs from
  // what is stored; the page redraws after a save, which hides it again.
  if (odoo && !readonly) {
    const saveBtn = $('#saveOdoo', mount);
    const baseline = JSON.stringify(readForm(form));
    const syncSave = () => {
      saveBtn.hidden = !(JSON.stringify(readForm(form)) !== baseline || companiesTouched);
    };
    syncSave();
    form.addEventListener('input', syncSave);
    form.addEventListener('change', syncSave);
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    busy($('#saveOdoo', mount), () =>
      guard(async () => {
        await persistOdoo();
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
      }, 'Odoo Connection Removed')
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
      <strong>${result.ok ? 'Connection Works' : 'Connection failed'}</strong>
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
function wireTestFirst({ form, test, commit, result, probe, label, gate, after, ignore = [] }) {
  let tested = null;   // { fingerprint, ok } for the values last tested
  let last = null;     // the TestResult itself
  // ``ignore``: fields a test does not need to be repeated for (the Odoo
  // company picker, which the test itself is what reveals).
  const fingerprint = () => {
    const values = readForm(form);
    ignore.forEach((name) => delete values[name]);
    return JSON.stringify(values);
  };

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
      : '';
    return `
      <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
        ${pill('active', 'Device Tracking On')}
        ${via ? `<span style="color:var(--muted);font-size:12.5px">${esc(via)}</span>` : ''}
      </div>`;
  }
  return `
    <div class="row" style="margin-bottom:14px;align-items:center;gap:10px">
      ${pill('pending', 'Device Tracking Off')}
      <span class="hint">Attendance records won't show which terminal punched them. Test connection turns this on.</span>
    </div>`;
}

/* One line under the status pills: how many of the Odoo companies are on.
 * Only says something once some are off — "all on" is the default and needs
 * no pill — and still understands the older single-company pin. */
function companyScopeRow(odoo, companies) {
  // A single-company Odoo has nothing to scope, so say nothing at all.
  const known = Array.isArray(companies);
  if (known && companies.length <= 1) return '';
  const off = (odoo.disabled_company_ids || []).length;
  if (odoo.company_id != null) {
    const label = odoo.company_name
      ? `${odoo.company_name} (id ${odoo.company_id})`
      : `company id ${odoo.company_id}`;
    return `
    <div class="row" style="margin-bottom:14px">
      ${pill('active', 'Scoped')}
      <span style="color:var(--muted);font-size:12.5px">${esc(label)} only</span>
    </div>`;
  }
  if (off) {
    return `
    <div class="row" style="margin-bottom:14px">
      ${pill('active', 'Scoped')}
      <span style="color:var(--muted);font-size:12.5px">${off} compan${off === 1 ? 'y' : 'ies'} disabled</span>
    </div>`;
  }
  if (!known) return '';   // not yet known to be multi-company
  return `
      <div class="row" style="margin-bottom:14px">
        <span class="hint">Every company this login can access is on.</span>
      </div>`;
}

/** Both rows above, together — everything inside #odooStatusExtras, so a
 * successful Test connection can repaint just this much of the card once it
 * has brought device tracking up to date, without touching the rest of the
 * form or the test-result panel. */
function trackingExtrasHtml(odoo, companies) {
  return '';
}

/** Which companies are switched off, as a Set of id strings, for what is
 * saved. The older single-company pin reads as "every other company is off". */
function savedDisabled(odoo, companies) {
  if (odoo?.company_id != null && Array.isArray(companies)) {
    return new Set(companies.filter((c) => c.id !== odoo.company_id).map((c) => String(c.id)));
  }
  return new Set((odoo?.disabled_company_ids || []).map(String));
}

/** "2 of 3 on", kept in step with the switches. */
function updateCompanyCount(mount) {
  const boxes = [...mount.querySelectorAll('[data-company]')];
  const label = mount.querySelector('#companyCount');
  if (label) label.textContent = `${boxes.filter((b) => b.checked).length} of ${boxes.length} enabled`;
}

/** The company switches. The list comes from Odoo itself — every company
 * this login can reach, fetched on its own for a saved connection and
 * returned by every Test connection — and each one is on unless it was
 * switched off. Companies on are the ones whose employees BioBridge syncs
 * and shows; one created in Odoo later arrives switched on.
 *
 * ``companies``: [{id, name}] once known, or null while not (yet).
 * ``off``: Set of company id strings currently switched off. */
function companyFieldHtml(odoo, companies, off) {
  // Only once Odoo has actually answered with its companies — a successful
  // Test connection, or the saved connection's own background fetch.
  if (!Array.isArray(companies)) return '';
  if (companies.length <= 1) return '';   // nothing to switch on a single-company Odoo
  const rows = companies.map((c) => `
      <label class="company-row">
        <input type="checkbox" class="co-switch" data-company="${esc(String(c.id))}"
          ${off.has(String(c.id)) ? '' : 'checked'}>
        <span class="company-name">${esc(c.name)}</span>
        <span class="company-id">id ${esc(String(c.id))}</span>
      </label>`).join('');
  const on = companies.filter((c) => !off.has(String(c.id))).length;
  return `
    <div class="field">
      <div class="co-heading">Odoo Companies <span class="opt" id="companyCount">${on} of ${companies.length} enabled</span></div>
      <div class="company-list">${rows}</div>
      <div class="help">Employees of the enabled companies are synced and shown in BioBridge. Disabled
        companies are left alone — their employees are hidden and their attendance is not written. A company
        added in Odoo later starts enabled.</div>
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

const PROVIDER_LABEL = { zk_device: 'ZKTeco protocol', zk_adms: 'Cloud push', hik_isapi: 'Hikvision', biostar2: 'Suprema BioStar 2', cosec: 'Matrix COSEC', cosec_centra: 'COSEC CENTRA', crosschex: 'Anviz CrossChex', hikconnect: 'Hik-Connect', hikcentral: 'HikCentral', cams: 'Cams biometrics', dahua: 'Dahua' };
/** Which direct-device protocol is offered first. */
const DIRECT_ORDER = { zk_device: 0, hik_isapi: 1, dahua: 2, cosec: 3, cams: 4, biotime: 0, biostar2: 1, hikcentral: 2, hikconnect: 3, cosec_centra: 4, crosschex: 5 };
/** Where push devices send to — from /providers (setup), for the forms. */
let pushSetup = null;

/** The box that tells a customer what to type into the terminal. */
function pushSetupHtml(serial) {
  const host = pushSetup?.server_address || window.location.hostname;
  const port = pushSetup?.server_port || 80;
  return `
    <div class="push-setup">
      <strong>On The Device</strong>
      <ol>
        <li>Open <b>Menu → Comm. → Cloud Server Setting</b> (on some models <b>ADMS</b>).</li>
        <li>Server address <code>${esc(host)}</code>, server port <code>${esc(port)}</code>.</li>
        <li>Turn <b>HTTPS</b> and <b>Enable Domain Name</b> off unless the address is a domain name; turn <b>Proxy</b> off.</li>
        <li>Save. The device calls in within a minute — then test here${serial ? ` (serial <code>${esc(serial)}</code>)` : ''}.</li>
      </ol>
    </div>`;
}

async function renderBiometric(mount, route) {
  mount.innerHTML = loading();
  const [sources, devices, allProviders] = await Promise.all([
    api.get('/sources'),
    api.get('/devices').catch(() => []),
    api.get('/providers').catch(() => []),
  ]);
  pushSetup = allProviders.find((p) => p.slug === 'zk_adms')?.setup || pushSetup;
  const readonly = !auth.canWrite;
  // The add wizard offers every protocol in one searchable list; what kind
  // of connection it is (platform server, standalone device, cloud push) comes
  // from the protocol picked — see AttendanceProvider.kinds.
  const providersFor = () => [...allProviders]
    .sort((a, b) => String(a.label).localeCompare(String(b.label)));

  // Providers that can create a user on the device — "Test connection"
  // also creates missing Odoo employees there for these (see
  // app/services/provisioning.py for the rule). Import terminals never does.
  const canProvision = new Set(allProviders
    .filter((p) => (p.capabilities || []).includes('read_employees')
      && (p.capabilities || []).includes('write_employees'))
    .map((p) => p.slug));

  // Platforms that can't list their terminals (they arrive with punches).
  const noInventory = new Set(allProviders
    .filter((p) => !(p.capabilities || []).includes('list_terminals')).map((p) => p.slug));

  const devicesBySource = {};
  devices.forEach((d) => { (devicesBySource[d.source_id] ||= []).push(d); });

  const actions = readonly ? '' : `
    <button type="button" class="primary" id="addConnection">+ Add Connection</button>`;

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change connections.', 'warn') : ''}

    <div class="card">
      ${cardHead('Biometric', 'where punches come from', actions)}


      ${sources.map((s) => sourceCard(s, devicesBySource[s.id] || [], readonly, canProvision.has(s.provider), !noInventory.has(s.provider))).join('')}
      ${!sources.length ? empty(
        'No Biometric Connections Yet',
        readonly ? '' : 'Use “+ Add connection” above to connect a platform server or a device.'
      ) : ''}
    </div>`;

  wireBiometric(mount, canProvision, sources, providersFor);

  // #/settings/biometric?add=1 — the Overview's setup checklist links here
  // to open the wizard straight away. Dropped from the address afterwards so
  // a reload does not open it again.
  if (route?.query?.add && auth.canWrite) {
    history.replaceState(null, '', '#/settings/biometric');
    openAddWizard({ providersFor, canProvision, onDone: () => renderBiometric(mount) });
  }
}

/* ===========================================================================
 * The add-connection wizard: a modal <dialog> in two steps.
 *
 *   1 Details  one searchable list of protocols; the fields below it follow
 *              the protocol picked (platform server, device or cloud push)
 *   2 Connect  the test runs by itself on arrival; Connect unlocks once the
 *              values have been tested, and reads "… anyway" after a failure
 *
 * Lives on <body>, outside the settings page, so nothing the page re-renders
 * can close it mid-way, and the browser's own modal handling gives focus
 * trapping and Esc-to-close for free. Nothing is saved until Connect.
 * ======================================================================== */

function openAddWizard({ providersFor, onDone, canProvision = new Set() }) {
  document.querySelector('dialog.wizard')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard';
  dialog.setAttribute('aria-labelledby', 'wizTitle');
  document.body.append(dialog);

  const state = {
    step: 1,
    provider: null,    // chosen in the one searchable list; decides everything below
    values: {},        // what step 1 holds, kept across Back/Next
    tested: null,      // { fingerprint, result }
  };
  const STEPS = ['Details', 'Test & Connect'];

  const allProviders = providersFor();
  const metaOf = () => allProviders.find((p) => p.slug === state.provider);
  // Every protocol is one kind of connection only.
  const kind = () => (metaOf()?.kinds || ['platform'])[0];
  const isDevice = () => kind() === 'device';
  const KIND_HINT = { platform: 'Platform server', device: 'Standalone device' };
  const hintOf = (p) => (p.pushes ? 'Cloud Push Device' : KIND_HINT[(p.kinds || ['platform'])[0]]);
  const commitLabel = () => (isDevice() ? 'Connect Device' : 'Connect Platform');

  /** Step 2's values shaped the way the API takes them. */
  const payload = () => {
    const values = { ...state.values };
    values.connection_kind = kind();
    values.provider = state.provider;
    if (state.provider === 'zk_device' && values.base_url && !/^zk:\/\//i.test(values.base_url)) {
      values.base_url = `zk://${values.base_url}`;
    }
    if (state.provider === 'zk_adms' && values.base_url && !/^adms:\/\//i.test(values.base_url)) {
      values.base_url = `adms://${values.base_url.trim().toUpperCase()}`;
    }
    if (['hik_isapi', 'dahua', 'cosec', 'cosec_centra'].includes(state.provider) && values.base_url && !/^https?:\/\//i.test(values.base_url)) {
      values.base_url = `http://${values.base_url.trim()}`;
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
        <form id="wizForm" novalidate>
          ${field({
            name: 'provider', label: 'Connection Protocol', required: true, value: state.provider || '',
            items: allProviders.map((p) => ({ value: p.slug, label: p.label, hint: hintOf(p) })),
            placeholder: 'Search or choose a protocol…', emptyNote: 'No Matching Protocol',
          })}
          ${state.provider
            ? sourceFieldsHtml(null, kind(), [metaOf()], state.provider)
            : '<p class="hint">Pick a protocol to see what it needs — BioTime, ZKTeco, Hikvision, Dahua and more.</p>'}
        </form>`;
    }
    const v = payload();
    const current = state.tested && state.tested.fingerprint === fingerprint();
    const push = state.provider === 'zk_adms';
    const rows = [
      ['Protocol', metaOf()?.label || state.provider],
      ['Type', hintOf(metaOf() || {})],
      ['Name', v.name],
      ...(isDevice() ? [['Location', v.location]] : []),
      [push ? 'Device Serial' : ['crosschex', 'hikconnect'].includes(state.provider) ? 'Region' : state.provider === 'cams' ? 'Endpoint URL' : isDevice() ? 'Device Address' : 'Server URL',
        push ? String(v.base_url || '').replace(/^adms:\/\//i, '') : v.base_url],
      ...(v.username ? [[state.provider === 'crosschex' ? 'API Key' : state.provider === 'hikconnect' ? 'App Key' : state.provider === 'hikcentral' ? 'Partner Key' : state.provider === 'cams' ? 'Service Tag ID' : 'Username', v.username]] : []),
      ['Timezone', v.server_timezone],
    ];
    return `
      <dl class="wiz-summary">
        ${rows.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val || '—')}</dd>`).join('')}
      </dl>
      <div id="wizResult">${current && state.tested.result.blocked
        ? `<div class="test-result bad"><strong>Already Connected</strong><span>${esc(state.tested.result.message)}</span>
             <span class="hint">Go back and enter a different address, or close this and edit the existing connection.</span></div>`
        : current
        ? testResultHtml(state.tested.result, false)
          + (push && !state.tested.result.ok ? pushSetupHtml(String(v.base_url || '').replace(/^adms:\/\//i, '')) : '')
        : state.testing
        ? '<div class="test-result pending"><strong>Testing The Connection…</strong><span>Nothing is saved yet.</span></div>'
        : '<div class="test-result pending"><strong>Not Tested Yet</strong><span>Click Test Connection to check these details. Nothing is saved yet.</span></div>'}</div>`;
  }

  function footHtml() {
    const back = state.step > 1
      ? '<button type="button" class="link" data-wiz="back">&larr; Back</button>'
      : '<button type="button" class="link" data-wiz="cancel">Cancel</button>';
    if (state.step === 1) {
      return `${back}<button type="submit" form="wizForm" class="primary" data-wiz="next" ${
        state.provider ? '' : 'disabled'}>Next &rarr;</button>`;
    }
    const current = state.tested && state.tested.fingerprint === fingerprint();
    const ok = current && state.tested.result.ok;
    const blocked = current && state.tested.result.blocked;
    return `${back}
      <div class="actions">
        <button type="button" data-wiz="test" id="wizTest">Test Connection</button>
        <button type="button" class="primary" data-wiz="connect" ${current && !blocked ? '' : 'disabled'}>
          ${esc(blocked ? 'Already Connected' : current && !ok ? `${commitLabel()} anyway` : commitLabel())}</button>
      </div>`;
  }

  function render() {
    dialog.innerHTML = `
      <div class="wiz-head">
        <strong id="wizTitle">Add A Biometric Connection</strong>
        <button type="button" class="link wiz-x" data-wiz="cancel" aria-label="Close">&times;</button>
      </div>
      ${stepper()}
      <div class="wiz-body${state.step === 1 ? ' wiz-tall' : ''}">${bodyHtml()}<p class="err" id="wizError"></p></div>
      <div class="wiz-foot">${footHtml()}</div>`;
    wire();
    if (state.step === 1) restoreValues();
  }

  function restoreValues() {
    const form = $('#wizForm', dialog);
    Object.entries(state.values).forEach(([k, v]) => {
      const el = form.querySelector(`[name="${k}"]`);
      // An empty value from another provider's form must not blank a field
      // that has its own default (CrossChex's region, COSEC's "sa" login).
      if (el && k !== 'provider' && !(!v && el.value)) el.value = v ?? '';
    });
    // First visit: the protocol search takes focus; afterwards the first field.
    (state.provider ? form.querySelector('input:not([type=hidden]):not(.ss-input)') : form.querySelector('.ss-input')
      || form).focus?.();
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
    if (!dialog.isConnected || state.step !== 2) return;
    state.testing = false;
    state.tested = { fingerprint: fp, result };
    render();
  }

  function wire() {
    dialog.querySelectorAll('[data-wiz=cancel]').forEach((b) => b.addEventListener('click', close));
    dialog.querySelector('[data-wiz=back]')?.addEventListener('click', () => {
      state.step -= 1;
      state.testing = false;
      render();
    });

    const form = $('#wizForm', dialog);
    if (form) {
      // Picking a protocol redraws the fields for it; what was typed in the
      // shared ones (name, timezone…) is carried over.
      form.addEventListener('change', (event) => {
        if (event.target.name !== 'provider' || event.target.value === state.provider) return;
        state.values = readForm(form);
        state.provider = event.target.value;
        state.tested = null;
        render();
      });
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!state.provider || !form.reportValidity()) return;
        state.values = readForm(form);
        state.step = 2;
        render();
      });
    }

    dialog.querySelector('[data-wiz=test]')?.addEventListener('click', () => {
      state.tested = null;
      state.testing = true;
      render();
      runTest();
    });
    // Errors are shown in the wizard, not as a toast: the modal sits above
    // everything on the page, toasts included.
    dialog.querySelector('[data-wiz=connect]')?.addEventListener('click', (event) =>
      busy(event.target, async () => {
        let created;
        try {
          created = await api.post('/sources', payload());
        } catch (error) {
          if (error.status !== 401) $('#wizError', dialog).textContent = error.message || 'Could not connect';
          return;
        }
        close();
        toast(isDevice() ? 'Device Connected And Added To Terminals' : 'Connection Added', 'ok');
        // Connect already tested it; a source that answered also gets any
        // Odoo employees it is missing, same as a later Test connection.
        if (created?.status === 'connected' && canProvision.has(state.provider)) {
          await provisionAfterTest(created.id);
        }
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


/* ========================================================================
 * Edit connection wizard
 *
 * One screen on the same modal chrome as the add wizard: the connection's
 * fields, a result banner, and Test connection / Save. Test only reports
 * (nothing is saved); Save writes the changes, re-tests the saved connection
 * and, for providers that can, adds any Odoo employees the device is missing.
 * ======================================================================== */

function openEditWizard({ source, onDone, canProvision = new Set() }) {
  document.querySelector('dialog.wizard')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard';
  dialog.setAttribute('aria-labelledby', 'wizTitle');
  document.body.append(dialog);

  const provider = source.provider;
  const kind = source.connection_kind;

  dialog.innerHTML = `
    <div class="wiz-head">
      <strong id="wizTitle">Edit ${esc(source.name)}</strong>
      <button type="button" class="link wiz-x" data-wiz="cancel" aria-label="Close">&times;</button>
    </div>
    <div class="wiz-body">
      <div id="wizResult"></div>
      <form id="wizForm" novalidate>${sourceFieldsHtml(source, kind, [], provider)}</form>
      <p class="err" id="wizError"></p>
    </div>
    <div class="wiz-foot">
      <button type="button" class="link" data-wiz="cancel">Cancel</button>
      <div class="actions">
        <button type="button" data-wiz="test">Test Connection</button>
        <button type="button" class="primary" data-wiz="save">Save</button>
      </div>
    </div>`;

  const form = $('#wizForm', dialog);
  const result = $('#wizResult', dialog);
  const error = $('#wizError', dialog);

  /** The form's values shaped the way the API takes them. A blank password
   * means "keep the saved one", so it is left out. */
  const payload = () => {
    const values = readForm(form);
    if (!values.password) delete values.password;
    if (provider === 'zk_device' && values.base_url && !/^zk:\/\//i.test(values.base_url)) {
      values.base_url = `zk://${values.base_url}`;
    }
    if (provider === 'zk_adms' && values.base_url && !/^adms:\/\//i.test(values.base_url)) {
      values.base_url = `adms://${values.base_url.trim().toUpperCase()}`;
    }
    if (['hik_isapi', 'dahua', 'cosec', 'cosec_centra'].includes(provider) && values.base_url && !/^https?:\/\//i.test(values.base_url)) {
      values.base_url = `http://${values.base_url.trim()}`;
    }
    return values;
  };

  function close() {
    dialog.close();
    dialog.remove();
  }

  // A result describes the values that were tested; once a field changes the
  // banner goes, rather than vouching for something that was never checked.
  let testedFor = null;
  const clearStale = () => {
    if (testedFor !== null && testedFor !== JSON.stringify(payload())) {
      result.innerHTML = '';
      testedFor = null;
    }
  };
  form.addEventListener('input', clearStale);
  form.addEventListener('change', clearStale);

  dialog.querySelectorAll('[data-wiz=cancel]').forEach((b) => b.addEventListener('click', close));

  dialog.querySelector('[data-wiz=test]').addEventListener('click', (event) => {
    if (!form.reportValidity()) return;
    error.textContent = '';
    const values = payload();
    busy(event.target, async () => {
      result.innerHTML = '<div class="test-result pending"><strong>Testing the connection\u2026</strong><span>Nothing is saved yet.</span></div>';
      const { name: _n, connection_kind: _k, auto_provision_employees: _p, ...probe } = values;
      let outcome;
      try {
        outcome = await api.post('/sources/test', { ...probe, source_id: source.id });
      } catch (e) {
        if (e.status === 401) return;
        outcome = { ok: false, message: e.message || 'The test could not run.' };
      }
      testedFor = JSON.stringify(values);
      result.innerHTML = testResultHtml(outcome, false);
      result.scrollIntoView({ block: 'nearest' });
    });
  });

  dialog.querySelector('[data-wiz=save]').addEventListener('click', (event) => {
    if (!form.reportValidity()) return;
    error.textContent = '';
    busy(event.target, async () => {
      try {
        await api.patch(`/sources/${source.id}`, payload());
        // Re-check at once, so the status shown is about the values just
        // saved rather than "unverified".
        const outcome = await api.post(`/sources/${source.id}/test`);
        close();
        toast(outcome.message, outcome.ok ? 'ok' : 'bad');
        if (outcome.ok && canProvision.has(provider)) await provisionAfterTest(source.id);
      } catch (e) {
        if (e.status !== 401) error.textContent = e.message || 'Could not save';
        return;
      }
      await onDone();
    });
  });

  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) close();
  });

  dialog.showModal();
  (form.querySelector('input:not([type=hidden])') || dialog).focus();
  return dialog;
}


/** One line for the toast after Import terminals created employees. */
/** After a connection answers a test, put any Odoo employees it is
 * missing onto it — the only place this happens on demand. Only speaks
 * up when something happened: people added, or some could not be. A
 * setup that simply has nothing to add (or no Odoo yet) stays quiet, since
 * this runs on every test. */
async function provisionAfterTest(sourceId) {
  let result;
  try {
    result = await api.post(`/sources/${sourceId}/provision-employees`);
  } catch (error) {
    if (error.status >= 500) toast(`Employees not added to the device: ${error.message}`, 'bad');
    return;
  }
  if (result.created.length || result.failed.length) {
    toast(provisionSummary(result), result.failed.length ? 'bad' : 'ok');
  }
}

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

const TRASH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';

/** "Delete this terminal?" — a small confirmation dialog. Resolves true once
 * the person confirms, false on Cancel / Escape / backdrop click. Modal and
 * outside the page's own markup, so a re-render can't close it mid-way. */
function confirmDeleteTerminal(name, serial, punches = 0) {
  document.querySelector('dialog.confirm-terminal')?.remove();
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'wizard confirm-terminal';
    dialog.setAttribute('aria-labelledby', 'delTermTitle');
    dialog.style.width = 'min(460px, calc(100vw - 24px))';
    dialog.innerHTML = `
      <div class="wiz-head"><strong id="delTermTitle">Delete Terminal?</strong>
        <button type="button" class="link wiz-x" data-no aria-label="Close">&times;</button></div>
      <div class="wiz-body">
        <p style="margin:0 0 10px"><strong>${esc(name)}</strong> <span class="mono hint">${esc(serial)}</span></p>
        ${punches > 0 ? `
        <p class="hint" style="margin:0 0 8px">This terminal has <strong>${esc(punches)} punch${punches === 1 ? '' : 'es'}</strong> on record,
          so it <strong>comes back after the next sync</strong>. Its punches belong to it and are never dropped.</p>
        <p class="hint" style="margin:0">To stop using it, choose <strong>Disable</strong> instead.</p>` : `
        <p class="hint" style="margin:0 0 8px">This removes the terminal from BioBridge. Attendance already in Odoo is kept.</p>
        <p class="hint" style="margin:0">If the terminal sends punches later, it is added back. To stop using one
          without losing it, choose <strong>Disable</strong> instead.</p>`}
      </div>
      <div class="wiz-foot">
        <button type="button" data-no>Cancel</button>
        <button type="button" class="danger" data-yes>Delete Terminal</button>
      </div>`;
    document.body.append(dialog);
    let answer = false;
    const finish = () => { dialog.close(); dialog.remove(); resolve(answer); };
    dialog.querySelectorAll('[data-no]').forEach((b) => b.addEventListener('click', finish));
    dialog.querySelector('[data-yes]').addEventListener('click', () => { answer = true; finish(); });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) finish(); });
    dialog.showModal();
    dialog.querySelector('[data-no]:not(.wiz-x)').focus();
  });
}

function sourceCard(source, devices, readonly, canProvision = false, canImport = true) {
  const kindLabel = source.provider === 'zk_adms' ? 'Cloud Push Device'
    : source.connection_kind === 'device' ? 'Standalone Device' : 'Platform Server';
  const providerLabel = PROVIDER_LABEL[source.provider];
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
          <div class="hint mono" style="margin-top:2px">${esc(source.provider === 'zk_adms'
            ? `serial ${source.base_url.replace(/^adms:\/\//i, '')}` : source.base_url)}</div>
        </div>
        ${!readonly ? `
          <div class="actions">
            ${!isConfirming ? `<button type="button" class="sm link" data-remove="${esc(source.id)}">Remove</button>` : ''}
            <button type="button" class="sm" data-edit="${esc(source.id)}">Edit</button>
            <button type="button" class="sm" data-test="${esc(source.id)}"
                    ${canProvision ? 'data-provision="1" title="Also creates Odoo employees who have a Badge ID or PIN and aren\'t on the device yet."' : ''}>Test Connection</button>
            ${source.connection_kind === 'device' || !canImport ? ''
              // One terminal, registered by the connection test itself (see
              // _register_standalone_device), so nothing to import. Creating
              // missing Odoo employees on the device/platform belongs to
              // Test connection (data-provision), never to this button.
              : `<button type="button" class="sm" data-discover="${esc(source.id)}">Import Terminals</button>`}
            ${auth.canWrite && source.is_active !== false ? `<button type="button" class="sm primary" data-sync-source="${esc(source.id)}"
                    title="Pull this connection's punches now and push them to Odoo">Sync Now</button>` : ''}
          </div>` : ''}
      </div>
      ${!readonly && isConfirming ? `
        <div class="row confirm-row">
          <span class="hint">Remove this connection? Its terminals go with it.</span>
          <button type="button" class="sm danger" data-remove-commit="${esc(source.id)}">Remove</button>
          <button type="button" class="sm link" data-remove-cancel="${esc(source.id)}">Cancel</button>
        </div>` : ''}
      ${source.status_message ? banner('Last Error', source.status_message, 'bad') : ''}

      ${devices.length ? `
        <div class="scroll" style="margin-top:12px">
          <table>
            <thead><tr><th>Device</th><th>Serial</th><th>IP</th><th class="num">Punches</th><th>Last Seen</th><th>Pairing</th><th></th></tr></thead>
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
                  <td class="num">${esc(d.matched_punch_count ?? d.punch_count)}</td>
                  <td>${esc(fmtAgo(d.last_seen_at))}</td>
                  <td>${esc(d.pairing_override || 'account default')}</td>
                  <td style="text-align:right">
                    ${readonly ? pill(d.is_enabled ? 'active' : 'skipped')
                      : `<div class="row" style="justify-content:flex-end;gap:6px;flex-wrap:nowrap">
                          <button type="button" class="sm" data-toggle="${esc(d.id)}" data-on="${d.is_enabled}">
                            ${d.is_enabled ? 'Disable' : 'Enable'}</button>
                          <button type="button" class="icon-btn" data-delete-device="${esc(d.id)}"
                            data-name="${esc(d.alias || d.serial_number)}" data-serial="${esc(d.serial_number)}" data-punches="${esc(d.punch_count || 0)}"
                            title="Delete terminal" aria-label="Delete terminal ${esc(d.alias || d.serial_number)}">${TRASH_ICON}</button>
                        </div>`}
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
function sourceFormHtml(source, kind, providers, currentProvider, provision = false) {
  const isDevice = kind === 'device';
  const provider = source ? source.provider : (currentProvider || providers[0]?.slug || 'biotime');
  const commitLabel = source ? 'Save Changes' : isDevice ? 'Connect Device' : 'Connect Platform';

  return `
    <form class="sourceForm" data-kind="${esc(kind)}" data-provider="${esc(provider)}"
          data-label="${esc(commitLabel)}"
          ${source ? `data-editing="${esc(source.id)}"` : ''}
          ${provision ? 'data-provision="1"' : ''}>
      <div class="form-head">
        <strong>${esc(source ? `Edit ${source.name}` : isDevice ? 'New Standalone Device' : 'New Platform Server')}</strong>
        <div class="actions">
          <button class="sm link" type="button" data-cancel-form="1">Cancel</button>
          <button class="sm" type="button" data-test-form="1">Test Connection</button>
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
  const isPush = provider === 'zk_adms';
  const isBioStar = provider === 'biostar2';
  const addressValue = source
    ? (isZk ? source.base_url.replace(/^zk:\/\//i, '')
      : isPush ? source.base_url.replace(/^adms:\/\//i, '') : source.base_url)
    : '';
  if (provider === 'cosec_centra') {
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'COSEC — Head office',
        help: 'Shown in this list — worth naming for the site it serves.',
      })}
      ${field({
        name: 'base_url', label: 'Server URL', required: true, value: source?.base_url || '',
        placeholder: 'http://cosec-server/cosec',
        help: 'The address you open COSEC at, up to /cosec — reachable from wherever BioBridge runs.',
      })}
      ${field({ name: 'username', label: 'Username', required: true, value: source?.username || 'sa',
                help: 'The COSEC API accepts only the System Administrator account (sa).' })}
      ${field({
        name: 'password', label: 'Password', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.' : 'The sa account’s COSEC password.',
      })}
      ${field({
        name: 'server_timezone', label: 'Server Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'The zone the COSEC server’s clock runs in. Event times arrive with no offset.',
        strongHelp: true, datalist: timezoneNames(),
      })}
      <div class="push-setup">
        <strong>In COSEC First</strong>
        <ol>
          <li>Open <b>Admin → Utility → API Configuration</b>.</li>
          <li>In the <b>T&amp;A events</b> template include <b>User ID</b> and <b>Event Date/Time</b> — and ideally <b>Entry/Exit</b>, <b>Device</b> and <b>Index No</b>.</li>
          <li>Save, then test here. The test tells you if a field is missing.</li>
        </ol>
      </div>
      <p class="hint" style="margin:4px 0 0">Panels appear here on their own as their punches arrive — there is nothing to import.</p>`;
  }
  if (provider === 'hikcentral') {
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'HikCentral — Head office',
        help: 'Shown in this list — worth naming for the site it serves.',
      })}
      ${field({
        name: 'base_url', label: 'Server URL', required: true, value: source?.base_url || '',
        placeholder: 'https://hcp.example.com',
        help: 'The HikCentral server, reachable from wherever BioBridge runs (VPN or forwarded port). Add :port if it isn’t 443.',
      })}
      ${field({ name: 'username', label: 'Partner key (AK)', required: true, value: source?.username || '',
                help: 'The API key of the OpenAPI partner created for BioBridge.' })}
      ${field({
        name: 'password', label: 'Partner Secret (SK)', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.' : 'Shown with the partner key when it is created.',
      })}
      ${field({
        name: 'server_timezone', label: 'Server Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'The zone the HikCentral server runs in.',
        datalist: timezoneNames(),
      })}
      ${field({
        name: 'verify_ssl', label: 'HTTPS Certificate', boolean: true,
        value: String(source ? source.verify_ssl : false),
        options: [{ value: 'false', label: 'Accept The Server’s Own Certificate' },
                  { value: 'true', label: 'Require A Trusted Certificate' }],
        help: 'HikCentral installs with a self-signed certificate unless you replaced it.',
      })}
      <div class="push-setup">
        <strong>In HikCentral First</strong>
        <ol>
          <li>Install the <b>HikCentral Professional OpenAPI</b> add-on that matches your HCP version.</li>
          <li>In the OpenAPI settings, add a <b>partner</b> for BioBridge and copy its <b>AK</b> and <b>SK</b>.</li>
          <li>Give every person an <b>Employee ID</b> in HCP — it is what matches them to Odoo.</li>
        </ol>
      </div>
      <p class="hint" style="margin:4px 0 0">Doors appear here on their own as their punches arrive — there is nothing to import.</p>`;
  }
  if (provider === 'hikconnect') {
    const regions = [
      { value: 'https://ieu.hikcentralconnect.com', label: 'Europe (ieu.hikcentralconnect.com)' },
      { value: 'https://ius.hikcentralconnect.com', label: 'North America (ius.hikcentralconnect.com)' },
    ];
    const saved = source?.base_url;
    if (saved && !regions.some((r) => r.value === saved)) regions.push({ value: saved, label: saved });
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'Hik-Connect — Head office',
        help: 'Shown in this list — worth naming for the team or site it serves.',
      })}
      ${field({
        name: 'base_url', label: 'Region', required: true,
        value: saved || regions[0].value, options: regions,
        help: 'Where your Hik-Connect for Teams account lives — the region you picked when signing up.',
      })}
      ${field({ name: 'username', label: 'App key', required: true, value: source?.username || '',
                help: 'Hik-Connect for Teams → Team Management → API Integration.' })}
      ${field({
        name: 'password', label: 'Secret Key', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.' : 'Created with the app key under API Integration.',
      })}
      ${field({
        name: 'server_timezone', label: 'Site Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'Records arrive with their offset; this is the zone they are shown in.',
        datalist: timezoneNames(),
      })}
      <p class="hint" style="margin:4px 0 0">Needs a Hik-Connect <b>for Teams</b> account — the free Hik-Connect app has no API. Terminals appear here on their own as their punches arrive.</p>`;
  }
  if (provider === 'crosschex') {
    const regions = [
      { value: 'https://api.us.crosschexcloud.com', label: 'United States (us.crosschexcloud.com)' },
      { value: 'https://api.eu.crosschexcloud.com', label: 'Europe (eu.crosschexcloud.com)' },
      { value: 'https://api.ap.crosschexcloud.com', label: 'Asia-Pacific (ap.crosschexcloud.com)' },
    ];
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'Anviz — Head office',
        help: 'Shown in this list — worth naming for the account or site it serves.',
      })}
      ${field({
        name: 'base_url', label: 'Region', required: true,
        value: source?.base_url || regions[0].value, options: regions,
        help: 'The one in your CrossChex Cloud address (us., eu. or ap.crosschexcloud.com).',
      })}
      ${field({ name: 'username', label: 'API key', required: true, value: source?.username || '',
                help: 'CrossChex Cloud → Settings → API → API key.' })}
      ${field({
        name: 'password', label: 'API Secret', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.' : 'Shown next to the API key in CrossChex Cloud.',
      })}
      ${field({
        name: 'server_timezone', label: 'Site Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'CrossChex Cloud sends times with their offset; this is the zone they are shown in.',
        datalist: timezoneNames(),
      })}
      <p class="hint" style="margin:4px 0 0">Terminals appear here on their own as their punches arrive — there is nothing to import.</p>`;
  }
  if (provider === 'cams') {
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Protocol', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'Front door',
        help: 'What this device is called here, on the Terminals page, and on its device record in Odoo.',
      })}
      ${field({
        name: 'location', label: 'Location', value: source?.location || '',
        placeholder: 'Main entrance, ground floor',
        help: 'Where the device is. Saved on its device record in Odoo.',
      })}
      ${field({
        name: 'base_url', label: 'Endpoint URL', required: true, value: source?.base_url || '',
        placeholder: 'https://…',
        help: 'The RESTful endpoint URL in your Cams API Monitor account (without the ?stgid part).',
      })}
      ${field({ name: 'username', label: 'Service tag ID', required: true, value: source?.username || '',
                help: 'This device’s stgid in API Monitor.' })}
      ${field({
        name: 'password', label: 'AuthToken', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.' : 'The 32-character token set for this device in API Monitor.',
      })}
      ${field({
        name: 'server_timezone', label: 'Device Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'The zone the device’s clock is set to.',
        datalist: timezoneNames(),
      })}
      <div class="push-setup">
        <strong>In Cams API Monitor First</strong>
        <ol>
          <li>Register the device and note its <b>Service Tag ID</b>, <b>AuthToken</b> and <b>endpoint URL</b>.</li>
          <li>Add this BioBridge server’s address as an <b>allowed origin</b> (otherwise Cams answers “invalid origin”).</li>
          <li>Make sure REST log loading is allowed for the device.</li>
        </ol>
      </div>`;
  }
  if (provider === 'hik_isapi' || provider === 'cosec' || provider === 'dahua') {
    const cosec = provider === 'cosec';
    return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: 'Protocol', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'Front door',
        help: 'What this device is called here, on the Terminals page, and on its device record in Odoo.',
      })}
      ${field({
        name: 'location', label: 'Location', value: source?.location || '',
        placeholder: 'Main entrance, ground floor',
        help: 'Where the device is. Saved on its device record in Odoo once the connection test recognises it.',
      })}
      ${field({
        name: 'base_url', label: 'Device Address', required: true, value: source?.base_url || '',
        placeholder: cosec ? 'http://192.168.1.80' : provider === 'dahua' ? 'http://192.168.1.108' : 'http://192.168.1.64',
        help: 'The device’s IP, reachable from wherever BioBridge runs. Add :port if it isn’t 80; use https:// only if HTTPS is on.',
      })}
      ${field({ name: 'username', label: 'Username', required: true, value: source?.username || 'admin',
                help: cosec ? 'The device’s web login (factory default admin / 1234 — change it).'
                  : provider === 'dahua' ? 'The device’s admin account (set when it was first activated).'
                  : 'The device’s admin account, or an operator with access-control rights.' })}
      ${field({
        name: 'password', label: 'Password', type: 'password', required: !source,
        placeholder: source ? 'unchanged' : '',
        help: source ? 'Leave blank to keep the current one.'
          : cosec ? 'The device’s web password.' : 'Five wrong tries lock the account on the device for 30 minutes.',
      })}
      ${field({
        name: 'server_timezone', label: 'Device Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'The zone the device’s clock is set to.',
        datalist: timezoneNames(),
      })}
      ${cosec ? '' : field({
        name: 'verify_ssl', label: 'HTTPS Certificate', boolean: true,
        value: String(source ? source.verify_ssl : false),
        options: [{ value: 'false', label: 'Accept The Device’s Own Certificate' },
                  { value: 'true', label: 'Require A Trusted Certificate' }],
        help: 'Only matters for https:// addresses. Most terminals use a self-signed certificate.',
      })}`;
  }
  if (isPush) {
    return `
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: 'Front door',
        help: 'What this device is called here, on the Terminals page, and on its device record in Odoo.',
      })}
      ${field({
        name: 'location', label: 'Location', value: source?.location || '',
        placeholder: 'Main entrance, ground floor',
        help: 'Where the device is. Saved on its device record in Odoo.',
      })}
      ${field({
        name: 'base_url', label: 'Device Serial Number', required: true, value: addressValue,
        placeholder: 'CKJG201760123',
        help: 'On the device: Menu → System Info → Device Info → Serial Number, or the label on its back.',
      })}
      ${field({
        name: 'server_timezone', label: 'Device Timezone', required: true,
        value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: 'The zone the device’s clock is set to. Punch times arrive with no offset, so a wrong value shifts every attendance record by hours without any error.',
        strongHelp: true, datalist: timezoneNames(),
      })}
      ${pushSetupHtml(addressValue)}`;
  }
  return `
      ${!source && providers.length > 1 ? field({
        name: 'provider', label: isDevice ? 'Protocol' : 'Platform', required: true, value: provider,
        options: providers.map((p) => ({ value: p.slug, label: p.label })),
      }) : ''}
      ${field({
        name: 'name', label: 'Name', required: true, value: source?.name || '',
        placeholder: isDevice ? 'Front door' : 'Primary BioTime',
        help: isDevice
          ? 'What this device is called here, on the Terminals page, and on its device record in Odoo.'
          : 'Shown in this list — worth naming for the site it serves.',
      })}
      ${isDevice ? field({
        name: 'location', label: 'Location', value: source?.location || '',
        placeholder: 'Main entrance, ground floor',
        help: 'Where the device is. Saved on its device record in Odoo once the connection test recognises it.',
      }) : ''}
      ${field({
        name: 'base_url', label: isZk ? 'Device Address' : isDevice ? 'Device Address' : 'Server URL',
        required: true, value: addressValue,
        placeholder: isZk ? '192.168.1.50' : isBioStar ? 'https://biostar.example.com'
          : isDevice ? 'https://192.168.1.50:8081' : 'https://biotime.example.com:8081',
        help: isZk
          ? 'The device’s own IP, reachable from wherever BioBridge runs. A port is optional — defaults to 4370.'
          : isDevice
          ? 'The device’s own address, reachable from wherever BioBridge runs.'
          : undefined,
      })}
      ${!isZk ? field({ name: 'username', label: 'Username', required: true, value: source?.username || '' }) : ''}
      ${field({
        name: 'password', label: isZk ? 'Comm Key' : 'Password', type: 'password',
        required: !source && !isZk,
        placeholder: source ? 'unchanged' : '',
        help: isZk
          ? 'Only if the device has a communication password set. Leave blank for the factory default (no password).'
          : source ? 'Leave blank to keep the current one.' : '',
      })}
      ${!isZk && !isBioStar ? '<div class="field-pair">' : ''}
      ${field({
        name: 'server_timezone', label: isBioStar ? 'Site Timezone' : isDevice ? 'Device Timezone' : 'Server timezone',
        required: true, value: source?.server_timezone || auth.tenant?.timezone || 'UTC',
        help: isBioStar
          ? 'BioStar 2 reports punch times in UTC; this is the zone they are shown in on this connection.'
          : 'The zone the device itself runs in — not yours and not Odoo’s. Punch times arrive with no offset, so a wrong value shifts every attendance record by hours without any error.',
        strongHelp: true, datalist: timezoneNames(),
      })}
      ${isBioStar ? field({
        name: 'verify_ssl', label: 'HTTPS Certificate', boolean: true,
        value: String(source ? source.verify_ssl : false),
        options: [{ value: 'false', label: 'Accept The Server’s Own Certificate' },
                  { value: 'true', label: 'Require A Trusted Certificate' }],
        help: 'BioStar 2 installs with a self-signed certificate unless you replaced it.',
      }) : ''}
      ${!isZk && !isBioStar ? field({
        name: 'auth_type', label: 'Auth Style', required: true, value: source?.auth_type || 'token',
        options: ['token', 'jwt'],
        help: 'BioTime 8.5+ usually needs jwt; older builds use token.',
      }) : ''}
      ${!isZk && !isBioStar ? '</div>' : ''}
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
    </div>
    ${connection.status_message
      ? banner('Last Error', connection.status_message, 'bad') : ''}`;
}

function wireBiometric(mount, canProvision = new Set(), sources = [], providersFor) {
  $('#addConnection', mount)?.addEventListener('click', () => {
    openAddWizard({ providersFor, canProvision, onDone: () => renderBiometric(mount) });
  });

  mount.querySelectorAll('[data-edit]').forEach((button) => {
    button.addEventListener('click', () => {
      const source = sources.find((x) => x.id === button.dataset.edit);
      if (source) openEditWizard({ source, canProvision, onDone: () => renderBiometric(mount) });
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
        }, 'Connection Removed')
      )
    );
  });

  mount.querySelectorAll('[data-test]').forEach((button) => {
    button.addEventListener('click', (event) =>
      busy(event.target, () =>
        guard(async () => {
          const result = await api.post(`/sources/${button.dataset.test}/test`);
          toast(result.message, result.ok ? 'ok' : 'bad');
          if (result.ok && button.dataset.provision) await provisionAfterTest(button.dataset.test);
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
      if (form.dataset.provider === 'zk_adms' && values.base_url && !/^adms:\/\//i.test(values.base_url)) {
        values.base_url = `adms://${values.base_url.trim().toUpperCase()}`;
      }
      if (['hik_isapi', 'dahua', 'cosec', 'cosec_centra'].includes(form.dataset.provider) && values.base_url && !/^https?:\/\//i.test(values.base_url)) {
        values.base_url = `http://${values.base_url.trim()}`;
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
            if (result.ok && form.dataset.provision) await provisionAfterTest(editing);
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

  mount.querySelectorAll('[data-delete-device]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!(await confirmDeleteTerminal(button.dataset.name, button.dataset.serial, Number(button.dataset.punches) || 0))) return;
      busy(button, () =>
        guard(async () => {
          await api.del(`/devices/${button.dataset.deleteDevice}`);
          await renderBiometric(mount);
        }, 'Terminal Deleted')
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
