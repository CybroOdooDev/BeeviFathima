/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth } from '../api.js';
import {
  $, banner, busy, empty, esc, fmtAgo, fmtIn, guard, loading, pill, stat,
} from '../ui.js';

/* The automatic-sync strip.
 *
 * This is the one thing an operator checks after setup: is it running by
 * itself, or am I going to be pressing a button every morning? So it reports
 * the scheduler's actual heartbeat rather than the configured interval — a
 * deployment whose scheduler died looks perfectly healthy from every other
 * angle, right up until payroll notices the missing days. */
function scheduleCard(schedule, needsSetup) {
  const s = schedule || {};
  const stopped = !auth.tenant?.syncable;
  const tone = stopped ? 'bad' : s.running ? 'ok' : 'warn';
  const modeLabel = s.mode === 'celery' ? 'Celery beat' : s.mode === 'inprocess'
    ? 'in the API process' : null;

  let headline;
  let detail;
  if (!auth.tenant?.syncable) {
    // First in the chain, and above the scheduler's own state, because it is
    // the true answer for this reader: whatever the scheduler is doing, it is
    // not going to sync this account. Getting this wrong was worse than saying
    // nothing — next_run_at is null for a stopped account, so this card used to
    // fall through to "sync is turned off in Settings" and send people to a
    // screen where their own switch is plainly still on.
    headline = 'Syncing is stopped for this account';
    detail = 'Your records are unchanged and still here. New punches are not '
      + 'being collected while the account is stopped. Contact support to have '
      + 'it restored.';
  } else if (!s.running) {
    headline = 'Automatic sync is not running';
    detail = s.last_tick_at
      ? `Nothing has scheduled a sync since ${esc(fmtAgo(s.last_tick_at))}. `
        + 'Punches are still collected when you press Sync now.'
      : 'No scheduler has ever reported in. Attendance will only move when '
        + 'someone presses Sync now.';
  } else if (needsSetup) {
    headline = 'Automatic sync is running, with nothing to sync';
    detail = 'The schedule is alive; it starts pulling punches once both sides '
      + 'are connected.';
  } else if (!s.next_run_at) {
    headline = 'Automatic sync is paused for this account';
    detail = 'The scheduler is running, but this account has sync turned off in '
      + 'Settings.';
  } else {
    headline = `Next sync ${esc(fmtIn(s.next_run_at))}`;
    detail = `Every ${esc(s.effective_interval_minutes)} minute`
      + `${s.effective_interval_minutes === 1 ? '' : 's'}`
      + (modeLabel ? `, ${modeLabel}` : '')
      + `. Last checked ${esc(fmtAgo(s.last_tick_at))}.`;
  }

  return `
    <div class="card" style="margin-bottom:14px">
      <div class="row" style="justify-content:space-between;align-items:flex-start">
        <div>
          <h2 style="margin:0">${headline}</h2>
          <div class="hint" style="margin-top:4px">${detail}</div>
          ${s.interval_widened ? `<div class="hint strong" style="margin-top:6px">
            Backed off to ${esc(s.effective_interval_minutes)} minutes after
            repeated connection failures. It returns to your configured interval
            as soon as one run succeeds.</div>` : ''}
        </div>
        <span class="pill ${tone}">${
          stopped ? 'account stopped' : s.running ? 'scheduler live' : 'scheduler down'
        }</span>
      </div>
    </div>`;
}

export async function render(mount) {
  mount.innerHTML = loading();
  const [data, runs] = await Promise.all([
    api.get('/dashboard'),
    api.get('/sync/runs?limit=5').catch(() => []),
  ]);

  const health = data.connection_health || {};
  const needsSetup = health.odoo === 'missing' || health.source === 'missing';
  const run = data.last_run;

  const banners = [];
  if (!auth.tenant?.syncable) {
    // Above the setup and unmapped-badge banners on purpose: those ask the
    // customer to go and fix something, and none of it will change anything
    // while the account is stopped.
    banners.push(banner(
      'This account is not syncing',
      'Syncing has been stopped by BioBridge'
        + (auth.tenant?.suspended_at ? ` ${fmtAgo(auth.tenant.suspended_at)}` : '')
        + '. Everything already recorded is still here and still visible — only '
        + 'the collection of new punches has stopped. Contact support to have it '
        + 'restored.',
      'bad'
    ));
  }
  if (needsSetup) {
    banners.push(banner(
      'Finish connecting',
      health.odoo === 'missing' && health.source === 'missing'
        ? 'Neither Odoo nor a device platform is connected yet. Nothing will sync until both are.'
        : health.odoo === 'missing'
          ? 'Odoo is not connected. Punches are captured but cannot be pushed.'
          : 'No device platform is connected, so there is nothing to pull punches from.',
      'warn'
    ));
  }
  if (data.renewal_warning) {
    // Ranked above the routine operational banners below (unmapped badges,
    // failed punches) even though nothing is actually broken yet — an
    // account about to stop syncing entirely is more consequential than
    // either, and the whole point of a warning is to be seen before it
    // becomes one of those two banners instead.
    const { days_left: daysLeft, urgent } = data.renewal_warning;
    banners.push(banner(
      daysLeft <= 0 ? 'Your subscription ends today'
        : `Your subscription ends in ${daysLeft} day${daysLeft === 1 ? '' : 's'}`,
      'Syncing stops automatically when it does. Nothing already recorded is '
        + 'ever affected — only the collection of new punches would stop. '
        + 'Contact support to renew.',
      // Same message either way — just louder once it's close. 'bad' inside
      // subscription_urgent_days (default 3), 'warn' from the wider
      // subscription_warning_days window down to that point.
      urgent ? 'bad' : 'warn'
    ));
  }
  if (data.unmapped_employees > 0) {
    banners.push(banner(
      `${data.unmapped_employees} badge${data.unmapped_employees === 1 ? '' : 's'} waiting to be matched`,
      'Their attendance is held until each badge is matched to an Odoo employee.',
      'warn'
    ));
  }
  if (data.punches_error > 0) {
    banners.push(banner(
      `${data.punches_error} punch${data.punches_error === 1 ? '' : 'es'} failed to reach Odoo`,
      'Open Activity to see the error on each one.',
      'bad'
    ));
  }

  mount.innerHTML = `
    ${banners.join('')}
    ${scheduleCard(data.schedule, needsSetup)}

    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Punches today', value: data.punches_today })}
      ${stat({
        label: 'Pending', value: data.punches_pending,
        tone: data.punches_pending > 0 ? 'warn' : '',
        note: 'awaiting the next run',
      })}
      ${stat({
        label: 'Unmatched badges', value: data.unmapped_employees,
        tone: data.unmapped_employees > 0 ? 'warn' : '',
      })}
      ${stat({
        label: 'Errors', value: data.punches_error,
        tone: data.punches_error > 0 ? 'bad' : '',
      })}
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Connections</h2>
        <table>
          <tbody>
            <tr><td>Odoo</td><td style="text-align:right">${pill(health.odoo)}</td></tr>
            <tr><td>Biometric</td><td style="text-align:right">${pill(health.source)}</td></tr>
          </tbody>
        </table>
        <div class="row" style="margin-top:14px">
          <a class="btn" href="#/settings/odoo">Manage connections</a>
        </div>
      </div>

      <div class="card">
        <h2>Last sync</h2>
        ${run ? `
          <table>
            <tbody>
              <tr><td>Result</td><td style="text-align:right">${pill(run.status)}</td></tr>
              <tr><td>When</td><td style="text-align:right">${esc(fmtAgo(run.started_at))}</td></tr>
              <tr><td>New punches</td><td class="num" style="text-align:right">${esc(run.punches_new)}</td></tr>
              <tr><td>Attendance created</td><td class="num" style="text-align:right">${esc(run.attendances_created)}</td></tr>
              <tr><td>Attendance closed</td><td class="num" style="text-align:right">${esc(run.attendances_closed)}</td></tr>
            </tbody>
          </table>
          ${run.error_message ? banner('Last error', run.error_message, 'bad') : ''}
        ` : empty('No sync has run yet', 'Connect both sides, then press Sync now.')}
        ${auth.canWrite ? `
          <div class="row" style="margin-top:14px">
            <button class="primary" id="syncNow" ${needsSetup ? 'disabled' : ''}>Sync now</button>
            <a class="btn" href="#/activity">View history</a>
          </div>` : ''}
      </div>
    </div>

    ${runs.length ? `
      <div class="card" style="margin-top:14px">
        <h2>Recent runs</h2>
        <div class="scroll">
          <table>
            <thead><tr><th>Started</th><th>Result</th><th class="num">New</th><th class="num">Created</th><th class="num">Closed</th><th>Trigger</th></tr></thead>
            <tbody>
              ${runs.map((r) => `
                <tr>
                  <td>${esc(fmtAgo(r.started_at))}</td>
                  <td>${pill(r.status)}</td>
                  <td class="num">${esc(r.punches_new)}</td>
                  <td class="num">${esc(r.attendances_created)}</td>
                  <td class="num">${esc(r.attendances_closed)}</td>
                  <td>${esc(r.triggered_by)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}
  `;

  const button = $('#syncNow', mount);
  if (button) {
    button.addEventListener('click', () =>
      busy(button, () =>
        guard(async () => {
          // run-inline returns the finished run, so the result is immediate
          // rather than a "queued" message that explains nothing.
          const result = await api.post('/sync/run-inline');
          const summary = `${result.status} — ${result.punches_new} new punch(es), `
            + `${result.attendances_created} created, ${result.attendances_closed} closed`;
          await render(mount);
          return summary;
        }).then((summary) => summary && window.dispatchEvent(
          new CustomEvent('bb:toast', { detail: summary })
        ))
      )
    );
  }
}
