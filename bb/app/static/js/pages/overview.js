/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth } from '../api.js';
import {
  $, banner, busy, empty, esc, fmtAgo, fmtIn, guard, loading, pill, stat, fmtUtc,
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

/* Getting started: the four things between signing up and attendance
 * arriving in Odoo, in order, each with the button that does it. Shown until
 * the first three are done — the fourth (matching badges) keeps coming back as
 * new people punch, and has its own banner for that. */
function setupChecklist({ health, devices, run, unmapped }) {
  const connected = (state) => state && state !== 'missing';
  const steps = [
    {
      done: connected(health.odoo),
      title: 'Connect Odoo',
      body: 'Where attendance is written. You need the server URL, database, login and an API key.',
      action: { href: '#/get-started?step=1', label: 'Connect Odoo' },
    },
    {
      done: connected(health.source),
      title: 'Add a biometric connection',
      body: 'A BioTime server, or a device by its IP address.',
      action: { href: '#/get-started?step=2', label: 'Add connection' },
    },
    {
      done: Boolean(run),
      title: 'Run the first sync',
      body: 'Pulls punches and writes attendance. After this it runs on its own schedule.',
      action: { href: '#/get-started?step=3', label: 'Run first sync' },
    },
    {
      done: Boolean(run) && unmapped === 0,
      title: 'Match any unknown badges',
      body: 'Badges no Odoo employee carries yet are held until they are matched.',
      action: { href: '#/get-started?step=4', label: 'Match badges' },
    },
  ];
  if (steps.slice(0, 3).every((s) => s.done)) return '';
  const next = steps.findIndex((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;
  return `
    <div class="card checklist" style="margin-bottom:14px">
      <div class="card-head">
        <h2>Get set up <span class="hint">${doneCount} of ${steps.length} done</span></h2>
        <div class="row" style="gap:12px;flex-wrap:nowrap">
          <div class="progress" aria-hidden="true"><span style="width:${(doneCount / steps.length) * 100}%"></span></div>
          ${auth.canWrite ? `<a class="btn primary-link sm" href="#/get-started">${doneCount ? 'Continue guided setup' : 'Start guided setup'}</a>` : ''}
        </div>
      </div>
      <ol class="steps-list">
        ${steps.map((s, i) => `
          <li class="${s.done ? 'done' : i === next ? 'next' : ''}">
            <span class="step-mark">${s.done ? '✓' : i + 1}</span>
            <div class="step-text"><strong>${esc(s.title)}</strong><span>${esc(s.body)}</span></div>
            ${!s.done && auth.canWrite ? (s.action.sync
              ? `<button class="${i === next ? 'primary' : ''} sm" data-checklist-sync="1" ${
                  !connected(health.odoo) || !connected(health.source) ? 'disabled' : ''}>${esc(s.action.label)}</button>`
              : `<a class="btn sm${i === next ? ' primary-link' : ''}" href="${esc(s.action.href)}">${esc(s.action.label)}</a>`) : ''}
          </li>`).join('')}
      </ol>
    </div>`;
}

export async function render(mount) {
  mount.innerHTML = loading();
  const [data, runs, devices] = await Promise.all([
    api.get('/dashboard'),
    api.get('/sync/runs?limit=5').catch(() => []),
    api.get('/devices').catch(() => []),
  ]);

  const health = data.connection_health || {};
  const needsSetup = health.odoo === 'missing' || health.source === 'missing';
  const run = data.last_run;
  const checklist = setupChecklist({
    health, devices: devices.length, run, unmapped: data.unmapped_employees,
  });

  // Failures and warnings (stopped account, renewals, failed punches, unmatched
  // badges, connections…) live in one place — the bell in the top bar — rather
  // than being repeated on this page.
  const today = new Date().toISOString().slice(0, 10);
  mount.innerHTML = `
    <div>
    <div class="rail-main">
    ${checklist}
    ${scheduleCard(data.schedule, needsSetup)}

    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Punches today', value: data.punches_today, href: `#/activity?date_from=${today}` })}
      ${stat({
        label: 'Pending', value: data.punches_pending,
        tone: data.punches_pending > 0 ? 'warn' : '',
        note: 'awaiting the next run', href: '#/activity?state=pending',
      })}
      ${stat({
        label: 'Unmatched employee badges', value: data.unmapped_employees,
        tone: data.unmapped_employees > 0 ? 'warn' : '', href: '#/settings/odoo?show=unmapped',
      })}
      ${stat({
        label: 'Errors', value: data.punches_error,
        tone: data.punches_error > 0 ? 'bad' : '', href: '#/activity?state=error',
      })}
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Connections</h2>
        <table>
          <tbody>
            <tr class="conn-row"><td><a class="conn-link" href="#/settings/odoo">Odoo</a></td><td style="text-align:right"><a class="conn-link" href="#/settings/odoo">${pill(health.odoo)}</a></td></tr>
            <tr class="conn-row"><td><a class="conn-link" href="#/settings/biometric">Biometric</a></td><td style="text-align:right"><a class="conn-link" href="#/settings/biometric">${pill(health.source)}</a></td></tr>
          </tbody>
        </table>
        <div class="row" style="margin-top:14px">
          <a class="btn" href="#/settings/biometric">Manage connections</a>
        </div>
      </div>

      <div class="card">
        <h2>Last sync${run ? ` <span class="pill mute" data-tip="${esc(fmtUtc(run.started_at))} UTC">${esc(fmtAgo(run.started_at))}</span>` : ''}</h2>
        ${run ? `
          <table>
            <tbody>
              <tr><td>Result</td><td style="text-align:right">${pill(run.status)}</td></tr>
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
    </div>
    </div>
  `;

  // The checklist's "Sync now" is the top bar's, pressed from here.
  mount.querySelector('[data-checklist-sync]')?.addEventListener('click', () => $('#topSync')?.click());

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
