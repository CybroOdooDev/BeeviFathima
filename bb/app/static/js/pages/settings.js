/* Tenant settings: the pairing rules that decide how punches become shifts. */

import { api, auth } from '../api.js';
import { $, banner, busy, field, guard, loading, readForm } from '../ui.js';

export async function render(mount) {
  mount.innerHTML = loading();
  // The schedule's live state comes with the dashboard. Fetched alongside so the
  // interval field can say what is actually happening instead of "if a worker is
  // running" — which left the reader to go and find out.
  const [tenant, dash] = await Promise.all([
    api.get('/tenant'),
    api.get('/dashboard').catch(() => null),
  ]);
  const schedule = dash?.schedule;
  const readonly = !auth.canWrite;

  const intervalHelp = !schedule
    ? 'How often BioBridge pulls new punches.'
    : schedule.running
      ? `The scheduler is running${schedule.mode === 'celery' ? ' under Celery beat' : ''}`
        + `, so this takes effect on its own — no button, no cron entry.`
      : 'Nothing is scheduling syncs right now, so this value has no effect yet. '
        + 'Check SCHEDULER_MODE and the service log, or /health/scheduler.';

  mount.innerHTML = `
    ${readonly ? banner('Read-only', 'Your role cannot change settings.', 'warn') : ''}

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
