/* The staff console's landing page: the whole platform at a glance.
 *
 * Counts and states only — GET /admin/overview never returns a punch, an
 * attendance record or an employee, so neither can this page. Each number
 * links to where it can be acted on: the account list, or one account's
 * Configure row (#/platform?open=<id>). */

import { api } from '../api.js';
import { banner, empty, esc, fmtAgo, fmtIn, loading, pill, stat } from '../ui.js';

const money = (cents) => `$${Math.round((cents || 0) / 100).toLocaleString()}`;
const n = (value) => Number(value || 0).toLocaleString();

const STATUS_ORDER = [
  { key: 'active', label: 'Active (paying)', tone: 'ok' },
  { key: 'trialing', label: 'Trialing', tone: 'accent' },
  { key: 'past_due', label: 'Past due', tone: 'warn' },
  { key: 'suspended', label: 'Suspended', tone: 'bad' },
  { key: 'cancelled', label: 'Cancelled', tone: 'mute' },
];

/* Punches per day, every account together: one series, so no legend — the
 * card title names it. Thin bars with a rounded top on a quiet baseline; the
 * exact number is in each bar's hover label (and in the table view below). */
function punchChart(series) {
  const W = 720;
  const H = 300;
  const pad = { l: 40, r: 8, t: 12, b: 26 };
  const max = Math.max(1, ...series.map((d) => d.punches));
  const nice = (() => {
    const p = 10 ** Math.floor(Math.log10(max));
    const m = max / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  })();
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const step = plotW / series.length;
  const bw = Math.min(26, Math.max(6, step * 0.56));
  const y = (v) => pad.t + plotH - (v / nice) * plotH;
  const ticks = [0, nice / 2, nice];
  const label = (iso) => new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

  const bars = series.map((d, i) => {
    const x = pad.l + i * step + (step - bw) / 2;
    const top = y(d.punches);
    const h = pad.t + plotH - top;
    const r = Math.min(4, h, bw / 2);
    const path = h <= 0 ? '' : `M${x},${pad.t + plotH} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${pad.t + plotH} Z`;
    const isToday = i === series.length - 1;
    return `
      <g class="bar-col" data-tip="${esc(`${label(d.day)}${isToday ? ' (today so far)' : ''} · ${n(d.punches)} punch${d.punches === 1 ? '' : 'es'}`)}">
        <rect class="hit" x="${pad.l + i * step}" y="${pad.t}" width="${step}" height="${plotH}"></rect>
        ${path ? `<path class="bar${isToday ? ' today' : ''}" d="${path}"></path>` : ''}
      </g>`;
  }).join('');

  const xLabels = series.map((d, i) => {
    const show = i === 0 || i === series.length - 1 || i === Math.floor(series.length / 2);
    if (!show) return '';
    const x = pad.l + i * step + step / 2;
    const anchor = i === 0 ? 'start' : i === series.length - 1 ? 'end' : 'middle';
    const tx = i === 0 ? pad.l + i * step : i === series.length - 1 ? pad.l + (i + 1) * step : x;
    return `<text class="axis" x="${tx}" y="${H - 6}" text-anchor="${anchor}">${esc(i === series.length - 1 ? 'Today' : label(d.day))}</text>`;
  }).join('');

  return `
    <svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Punches per day across every account, last ${series.length} days">
      ${ticks.map((t) => `
        <line class="gridline" x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}"></line>
        <text class="axis" x="${pad.l - 8}" y="${y(t) + 4}" text-anchor="end">${esc(n(t))}</text>`).join('')}
      ${bars}
      ${xLabels}
    </svg>
    <details class="table-view">
      <summary>Show as a table</summary>
      <table><thead><tr><th>Day</th><th class="num">Punches</th></tr></thead><tbody>
        ${series.map((d) => `<tr><td>${esc(label(d.day))}</td><td class="num">${esc(n(d.punches))}</td></tr>`).join('')}
      </tbody></table>
    </details>`;
}

/* Accounts by status: one bar split by state, each segment's state named in
 * the legend beside its count — never colour alone. */
function statusBar(byStatus, total) {
  const parts = STATUS_ORDER.map((s) => ({ ...s, count: byStatus[s.key] || 0 })).filter((s) => s.count);
  return `
    <div class="seg-bar" role="img" aria-label="${esc(parts.map((p) => `${p.count} ${p.label}`).join(', '))}">
      ${parts.map((p) => `<span class="seg ${p.tone}" style="flex:${p.count}"
          data-tip="${esc(`${p.label}: ${p.count} of ${total}`)}"></span>`).join('')}
    </div>
    <ul class="legend">
      ${STATUS_ORDER.map((s) => `
        <li><span class="swatch ${s.tone}"></span>${esc(s.label)}<b>${esc(n(byStatus[s.key] || 0))}</b></li>`).join('')}
    </ul>`;
}

export async function renderConsoleOverview(mount) {
  mount.innerHTML = loading();
  const o = await api.get('/admin/overview');
  const a = o.accounts;
  const runs = o.activity.runs_24h;
  const trialing = a.by_status.trialing || 0;

  mount.innerHTML = `
    ${o.scheduler.running ? '' : banner(
      'The scheduler is not running',
      'No account is syncing automatically right now'
        + (o.scheduler.last_tick_at ? ` — the last tick was ${fmtAgo(o.scheduler.last_tick_at)}.` : '.')
        + ' Customers can still press Sync now.',
      'bad')}

    <div class="grid cols-4" style="margin-bottom:18px">
      ${stat({ label: 'Accounts', value: n(a.total), note: `${n(a.new_30d)} new in the last 30 days`, href: '#/platform' })}
      ${stat({ label: 'Paying', value: n(o.revenue.paying), note: `${money(o.revenue.mrr_cents)} a month`, href: '#/platform' })}
      ${stat({ label: 'Trialing', value: n(trialing), note: `${n(o.renewals.length)} renew in the next 14 days`, href: '#/platform' })}
      ${stat({
        label: 'Need attention', value: n(o.attention_total),
        tone: o.attention.some((x) => x.severity >= 3) ? 'bad' : o.attention_total ? 'warn' : '',
        note: o.attention_total ? 'see the list below' : 'nothing waiting', href: '#/console#attention',
      })}
    </div>

    <div class="grid cols-4" style="margin-bottom:18px">
      ${stat({ label: 'Punches today', value: n(o.activity.punches_today), note: `${n(o.activity.punches_period)} in ${o.punches_by_day.length} days` })}
      ${stat({
        label: 'Syncs, last 24 h', value: n(runs.success + runs.partial + runs.failed),
        tone: runs.failed ? 'warn' : '',
        note: runs.failed ? `${n(runs.failed)} failed · ${n(runs.success)} succeeded` : `${n(runs.success)} succeeded`,
      })}
      ${stat({ label: 'Punches stuck in error', value: n(o.activity.error_punches), tone: o.activity.error_punches ? 'bad' : '' })}
      ${stat({ label: 'Badges not matched', value: n(o.activity.unmatched_badges), tone: o.activity.unmatched_badges ? 'warn' : '' })}
    </div>

    <div class="grid console-grid" style="margin-bottom:18px">
      <div class="card">
        <h2>Punches per day <span class="hint">every account, last ${o.punches_by_day.length} days (UTC)</span></h2>
        ${punchChart(o.punches_by_day)}
      </div>
      <div class="card">
        <h2>Accounts by status <span class="hint">${n(a.total)} total</span></h2>
        ${statusBar(a.by_status, a.total)}
        <h3 class="sub-h">Set-up</h3>
        <ul class="legend">
          <li><span class="swatch ok"></span>Both sides connected<b>${n(o.setup.ready)}</b></li>
          <li><span class="swatch warn"></span>Half connected<b>${n(o.setup.partial)}</b></li>
          <li><span class="swatch mute"></span>Nothing connected<b>${n(o.setup.none)}</b></li>
        </ul>
        <h3 class="sub-h">Sync health</h3>
        <ul class="legend">
          <li><span class="swatch bad"></span>Last sync failed<b>${n(o.health.failing)}</b></li>
          <li><span class="swatch warn"></span>Backed off<b>${n(o.health.backed_off)}</b></li>
          <li><span class="swatch mute"></span>Automatic sync off<b>${n(o.health.sync_off)}</b></li>
        </ul>
      </div>
    </div>

    <div class="grid cols-2" style="margin-bottom:18px">
      <div class="card" id="attention">
        <h2>Needs attention <span class="hint">${o.attention_total > o.attention.length
          ? `the ${o.attention.length} most urgent of ${n(o.attention_total)}` : n(o.attention_total)}</span></h2>
        ${o.attention.length ? `
          <ul class="item-list">
            ${o.attention.map((x) => `
              <li>
                <div class="item-main">
                  <strong>${esc(x.name)}</strong>
                  <div class="pills">${x.reasons.map((r) => `<span class="pill ${esc(r.tone)}">${esc(r.text)}</span>`).join(' ')}</div>
                </div>
                <a class="btn sm" href="#/platform?open=${esc(x.id)}">Open</a>
              </li>`).join('')}
          </ul>` : empty('Nothing needs attention', 'Every account is connected, syncing, and paid up.')}
      </div>

      <div class="card">
        <h2>Renewing in the next 14 days <span class="hint">${n(o.renewals.length)}</span></h2>
        ${o.renewals.length ? `
          <ul class="item-list">
            ${o.renewals.map((r) => `
              <li>
                <div class="item-main">
                  <strong>${esc(r.name)}</strong>
                  <div class="hint">${pill(r.status, r.status.replace('_', ' '))} ${esc(r.plan_name || 'no plan')} · renews ${esc(fmtIn(r.renews_at))}</div>
                </div>
                <a class="btn sm" href="#/platform?open=${esc(r.id)}">Open</a>
              </li>`).join('')}
          </ul>` : empty('No renewals coming up', 'Nothing renews in the next two weeks.')}
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Plans <span class="hint">${money(o.revenue.mrr_cents)} recurring a month</span></h2>
        <div class="scroll">
          <table class="fit">
            <thead><tr><th>Plan</th><th class="num">Price</th><th class="num">Accounts</th><th class="num">Paying</th><th class="num">Monthly</th></tr></thead>
            <tbody>
              ${o.revenue.by_plan.map((p) => `
                <tr>
                  <td>${esc(p.name)}</td>
                  <td class="num">${p.price_cents != null ? `${money(p.price_cents)}/mo` : '—'}</td>
                  <td class="num">${esc(n(p.accounts))}</td>
                  <td class="num">${esc(n(p.paying))}</td>
                  <td class="num">${p.mrr_cents ? esc(money(p.mrr_cents)) : '—'}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h2>Recent failed syncs <span class="hint">employee names removed</span></h2>
        ${o.failed_runs.length ? `
          <ul class="item-list">
            ${o.failed_runs.map((r) => `
              <li>
                <div class="item-main">
                  <strong>${esc(r.tenant_name)}</strong> <span class="hint">${esc(fmtAgo(r.started_at))} · ${esc(r.triggered_by)}</span>
                  <div class="hint clamp">${esc(r.message || 'No message recorded.')}</div>
                </div>
                <a class="btn sm" href="#/platform?open=${esc(r.tenant_id)}">Open</a>
              </li>`).join('')}
          </ul>` : empty('No failed syncs', 'Nothing has failed recently.')}
      </div>
    </div>`;

  // "Need attention" is on this page; the tile scrolls to it.
  mount.querySelector('a.stat-link[href="#/console#attention"]')?.addEventListener('click', (event) => {
    event.preventDefault();
    mount.querySelector('#attention')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}
