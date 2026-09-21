/* Tenant settings: the pairing rules that decide how punches become shifts. */

import { api, auth } from '../api.js';
import { $, banner, busy, esc, field, fmtIn, guard, loading, readForm } from '../ui.js';

export async function render(mount) {
  mount.innerHTML = loading();
  // The schedule's live state comes with the dashboard. Fetched alongside so the
  // interval field can say what is actually happening instead of "if a worker is
  // running" — which left the reader to go and find out. Plans come from the
  // same public list the signup picker uses — best-effort, since a failed
  // fetch should still leave the rest of Settings usable.
  const [tenant, dash, plans] = await Promise.all([
    api.get('/tenant'),
    api.get('/dashboard').catch(() => null),
    api.get('/auth/plans').catch(() => []),
  ]);
  const schedule = dash?.schedule;
  const readonly = !auth.canWrite;

  // The switch below is theirs and still works, so it would sit there looking
  // like the answer. Say plainly that it is not: someone whose sync stopped
  // comes straight here, finds Automatic sync already On, and has nowhere else
  // to look.
  const stopped = tenant.syncable === false;

  // The floor is a plan limit, not a validation rule this form invents — the
  // server is the one that actually enforces it (PATCH /tenant), so this is
  // purely to explain a rejection before it happens rather than after.
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

  // Only plans the account can actually switch to — retired plans (still
  // returned to staff by /admin/plans) never belong in a self-service picker.
  const activePlans = plans.filter((p) => p.is_active);
  const renewalWarning = dash?.renewal_warning;

  // A plan already paid for (status active) defers a switch instead of
  // applying it — see app.api.v1.sync.update_tenant. Mirrored here only to
  // pick the right copy and toast; the server is what actually enforces it.
  const willDefer = tenant.status === 'active' && Boolean(tenant.plan_id);

  const planCard = (tenant.plan_name || activePlans.length) ? `
    <div class="card" style="margin-bottom:14px">
      <h2>Plan <span class="hint">what this account is billed and limited by</span></h2>
      <div class="hint" style="margin-bottom:10px">
        ${tenant.plan_name ? `Currently <strong>${esc(tenant.plan_name)}</strong>` : 'No plan assigned — nothing is limited.'}
        ${tenant.plan_max_employees != null ? ` · up to ${esc(tenant.plan_max_employees)} employees` : ''}
        ${floor ? ` · syncs no faster than every ${esc(floor)} min` : ''}
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
      ` : ''}
    </div>` : '';

  mount.innerHTML = `
    ${stopped ? banner(
      'Syncing is stopped for this account',
      'BioBridge has stopped collecting new punches. Your settings below still '
      + 'save, and they take effect once the account is restored. Contact '
      + 'support about restoring it.',
      'bad') : ''}
    ${readonly ? banner('Read-only', 'Your role cannot change settings.', 'warn') : ''}

    ${planCard}

    <form id="form" ${readonly ? 'inert' : ''}>
      <div class="card" style="margin-bottom:14px">
        <h2>General</h2>
        ${field({ name: 'name', label: 'Company', value: tenant.name, required: true })}
        ${field({
          name: 'timezone', label: 'Display timezone', value: tenant.timezone, required: true,
          help: 'Used to render attendance for your team. Separate from each device platform’s own server timezone.',
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

      <div class="card" style="margin-bottom:14px">
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
      </div>

      <div class="card" style="margin-bottom:14px">
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
      </div>

      <div class="row end">
        <button class="primary" id="save">Save settings</button>
      </div>
    </form>`;

  const planForm = $('#planForm', mount);
  if (planForm) {
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
          await render(mount);
        }, message)
      );
    });
  }

  if (readonly) return;

  $('#form', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    busy($('#save', mount), () =>
      guard(async () => {
        await api.patch('/tenant', values);
        await render(mount);
      }, 'Settings saved')
    );
  });
}
