/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth } from '../api.js';
import { pairingReviewed } from '../setup-state.js';
import {
  $, banner, busy, empty, esc, fmtAgo, fmtIn, guard, loading, pill, stat, fmtUtc, triggerLabel } from '../ui.js';

/* Getting started: the five things between signing up and attendance
 * arriving in Odoo, in order, each done from the guided setup (the one button above). Shown until
 * the first four are done — the fifth (matching badges) keeps coming back as
 * new people punch, and has its own banner for that. */
function setupChecklist({ health, devices, run, unmapped }) {
  const connected = (state) => state && state !== 'missing';
  const steps = [
    {
      done: connected(health.odoo),
      title: 'Connect Odoo',
      body: 'Where attendance is written. You need the server URL, database, login and an API key.',
    },
    {
      done: connected(health.source),
      title: 'Add A Biometric Connection',
      body: 'A BioTime server, or a device by its IP address.',
    },
    {
      // Every account has working defaults; "done" = someone looked, or it was already syncing.
      done: pairingReviewed() || Boolean(run),
      title: 'Pairing Rules',
      body: 'How punches become check-ins and check-outs. The defaults suit most sites.',
    },
    {
      done: Boolean(run),
      title: 'Run The First Sync',
      body: 'Pulls punches and writes attendance. After this it runs on its own schedule.',
    },
    {
      done: Boolean(run) && unmapped === 0,
      title: 'Match Unknown Badges',
      body: 'Badges no Odoo employee carries yet are held until they are matched.',
    },
  ];
  if (steps.slice(0, 4).every((s) => s.done)) return '';
  const next = steps.findIndex((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;
  return `
    <div class="card checklist" style="margin-bottom:14px">
      <div class="card-head">
        <h2>BioBridge Setup</h2>
        <div class="row" style="gap:12px;flex-wrap:nowrap">
          ${doneCount ? `<div class="progress" aria-hidden="true"><span style="width:${(doneCount / steps.length) * 100}%"></span></div>` : ''}
          ${auth.canWrite ? `<div class="setup-start">
            <a class="btn primary-link sm" href="#/get-started">${doneCount ? 'Continue Setup' : 'Initial Setup'}</a>
            <div class="setup-pointer" role="note"><span class="setup-pointer-arrow" aria-hidden="true"></span>${
              doneCount ? 'Continue Here' : 'Start Here'}</div>
          </div>` : ''}
        </div>
      </div>
      <ol class="steps-list">
        ${steps.map((s, i) => `
          <li class="${s.done ? 'done' : i === next ? 'next' : ''}">
            <span class="step-mark">${s.done ? '✓' : i + 1}</span>
            <div class="step-text"><strong>${esc(s.title)}</strong><span>${esc(s.body)}</span></div>
          </li>`).join('')}
      </ol>
    </div>`;
}

/* "Next sync in 15 min" with the scheduler's pulse. Only shown once setup is
 * finished — before that, the checklist is what the page is for. */
function scheduleCard(schedule) {
  if (!schedule) return '';
  const every = schedule.effective_interval_minutes;
  let title; let detail; let tag;
  if (!schedule.running) {
    title = 'Scheduler Not Running';
    detail = 'Automatic syncs are paused until the scheduler is back. You can still press Sync Now.';
    tag = '<span class="pill bad">scheduler down</span>';
  } else if (!schedule.next_run_at) {
    title = 'Automatic Sync Is Off';
    detail = 'Syncing is switched off or stopped for this account. Press Sync Now to run one by hand.';
    tag = '<span class="pill mute">scheduler idle</span>';
  } else {
    title = `Next sync ${fmtIn(schedule.next_run_at)}`;
    detail = `Every ${every} minutes${schedule.interval_widened ? ' (slowed to fit your plan)' : ''}.`
      + (schedule.seconds_since_tick != null ? ` Last checked ${fmtAgo(new Date(Date.now() - schedule.seconds_since_tick * 1000).toISOString())}.` : '');
    tag = '<span class="pill ok">scheduler live</span>';
  }
  return `
    <div class="card" style="margin-bottom:14px">
      <div class="row" style="justify-content:space-between;align-items:flex-start">
        <div>
          <h2 style="margin:0 0 4px;font-size:22px">${esc(title)}</h2>
          <div style="color:var(--muted);font-size:13.5px">${esc(detail)}</div>
        </div>
        ${tag}
      </div>
    </div>`;
}

/* The one word for all biometric connections: the worst state among them. */
function worstOf(sources, fallback) {
  for (const st of ['failed', 'degraded', 'unverified', 'connected']) {
    if ((sources || []).some((x) => x.status === st)) return st;
  }
  return fallback;
}

/* "failed" alone says little once there are several biometric connections:
 * say how many are down, and which. Counted here from the connection list, so
 * it does not depend on the dashboard payload carrying it. */
function sourceDetail(sources) {
  if (!sources || sources.length < 2) return '';
  const failed = sources.filter((x) => x.status === 'failed');
  if (!failed.length) return '';   // only a failure is worth a line
  const names = failed.map((x) => esc(x.name || x.provider)).join(', ');
  return `<tr><td colspan="2" class="hint">${failed.length} of ${sources.length} biometric connections failed. Needs attention: ${names}.</td></tr>`;
}

export async function render(mount) {
  mount.innerHTML = loading();
  const [data, runs, devices, sources] = await Promise.all([
    api.get('/dashboard'),
    api.get('/sync/runs?limit=5').catch(() => []),
    api.get('/devices').catch(() => []),
    api.get('/sources').catch(() => []),
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
    ${checklist || scheduleCard(data.schedule)}

    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat({ label: 'Punches today', value: data.punches_today, href: `#/activity?date_from=${today}` })}
      ${stat({
        label: 'Pending', value: data.punches_pending,
        tone: data.punches_pending > 0 ? 'warn' : '',
        note: 'awaiting the next run', href: '#/activity?state=pending',
      })}
      ${stat({
        label: 'Unmatched Employee Badges', value: data.unmapped_employees,
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
            <tr class="conn-row"><td><a class="conn-link" href="#/settings/biometric">Biometric</a></td><td style="text-align:right"><a class="conn-link" href="#/settings/biometric">${pill(worstOf(sources, health.source))}</a></td></tr>
            ${sourceDetail(sources)}
          </tbody>
        </table>
        <div class="row" style="margin-top:14px">
          <a class="btn" href="#/settings/biometric">Manage Connections</a>
        </div>
      </div>

      <div class="card">
        <h2 class="h2-split">Latest Sync${run ? ` <span class="pill mute" data-tip="${esc(fmtUtc(run.started_at))} UTC">${esc(fmtAgo(run.started_at))}</span>` : ''}</h2>
        ${run ? `
          <table>
            <tbody>
              <tr><td>Result</td><td style="text-align:right">${pill(run.status)}</td></tr>
              <tr><td>New punches</td><td class="num" style="text-align:right">${esc(run.punches_new)}</td></tr>
              <tr><td>Attendance created</td><td class="num" style="text-align:right">${esc(run.attendances_created)}</td></tr>
              <tr><td>Attendance closed</td><td class="num" style="text-align:right">${esc(run.attendances_closed)}</td></tr>
            </tbody>
          </table>
          ${run.error_message ? banner('Last Error', run.error_message, 'bad') : ''}
        ` : empty('No Sync Has Run Yet', 'Connect both sides, then press Sync now.')}
        ${auth.canWrite ? `
          <div class="row" style="margin-top:14px">
            <button class="primary" id="syncNow" ${needsSetup ? 'disabled' : ''}>Sync Now</button>
            <a class="btn" href="#/activity">View History</a>
          </div>` : ''}
      </div>
    </div>

    ${runs.length ? `
      <div class="card" style="margin-top:14px">
        <h2>Recent Runs</h2>
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
                  <td>${esc(triggerLabel(r.triggered_by))}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}
    </div>
    </div>
  `;

  // The checklist's "Sync now" is the top bar's, pressed from here.

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
