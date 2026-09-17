/* Overview: is it working, what needs attention, and the Sync now button. */

import { api, auth } from '../api.js';
import { $, banner, busy, empty, esc, fmtAgo, guard, loading, pill, stat } from '../ui.js';

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
            <tr><td>Device platform</td><td style="text-align:right">${pill(health.source)}</td></tr>
          </tbody>
        </table>
        <div class="row" style="margin-top:14px">
          <a class="btn" href="#/setup">Manage connections</a>
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
