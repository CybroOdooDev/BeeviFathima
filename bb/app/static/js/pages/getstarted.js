/* #/get-started — the guided first setup, one step at a time.
 *
 *   1 Connect Odoo            where attendance is written
 *   2 Add a biometric source  where punches come from
 *   3 Review pairing          how punches become check-ins and check-outs
 *   4 Run the first sync      pulls punches, writes attendance
 *   5 Match unknown badges    anyone Odoo doesn't recognise yet (skippable)
 *   ✓ Done
 *
 * Each step shows the same form the Settings pages use (rendered into this
 * page, so there is one implementation of each), with a plain-language
 * explanation above it and Back / Skip / Continue below. Progress is read
 * from the server every few seconds, so "Continue" lights up the moment a
 * step is actually done — the customer never has to work out where to go
 * next. Leaving and coming back resumes at the first unfinished step.
 */

import { api, auth } from '../api.js';
import { $, banner, busy, esc, guard, loading, toast } from '../ui.js';
import { needsMatch, unmappedCard, wireUnmapped } from './data.js';
import { render as renderSettings } from './settings.js';
import { markPairingReviewed, pairingReviewed } from '../setup-state.js';

const STEPS = [
  {
    key: 'odoo', title: 'Connect Odoo', short: 'Odoo',
    intro: 'BioBridge writes attendance into your Odoo. Enter your Odoo address, database, '
      + 'the login of a user who can manage Attendances, and an API key for that user. '
      + 'Click <strong>Test connection</strong>, then <strong>Connect Odoo</strong>.',
    help: 'Create the API key in Odoo: click your avatar → <em>My Profile</em> (or <em>Preferences</em>) → '
      + '<em>Account Security</em> → <em>New API Key</em>. Copy it straight away — Odoo shows it only once.',
    done: (s) => s.odoo,
  },
  {
    key: 'biometric', title: 'Connect your biometric system', short: 'Biometric',
    intro: 'Tell BioBridge where punches come from: a server such as ZKTeco BioTime, HikCentral or '
      + 'BioStar 2, or a terminal directly by its IP address or serial number. '
      + 'Click <strong>+ Add connection</strong> and follow the three short steps.',
    help: 'Not sure which to pick? If your staff punch on terminals managed by software on a PC or '
      + 'server, choose that software. If the terminal works on its own, choose the device.',
    done: (s) => s.source,
  },
  {
    key: 'pairing', title: 'Pairing Rules', short: 'Pairing',
    intro: 'Pairing decides how raw punches become shifts: which punch is a check-in and which a '
      + 'check-out, how double-taps are handled and the longest shift to accept. The defaults suit most '
      + 'sites — look them over, change anything that doesn’t fit, then continue.',
    help: 'Alternating (in, out, in, out) suits most terminals, which have no IN/OUT keys. '
      + 'You can change all of this later under <em>Settings → Pairing</em>.',
    // Every account has working defaults, so "done" means someone has looked —
    // or the account was already syncing before this step existed.
    done: (s) => s.pairing || s.run,
  },
  {
    key: 'sync', title: 'Run the first sync', short: 'First sync',
    intro: 'Pull the punches recorded so far and write them to Odoo as attendance. '
      + 'After this, BioBridge syncs on its own every few minutes.',
    help: 'The first sync reads up to the last few weeks of punches, so it can take a minute.',
    done: (s) => s.run,
  },
  {
    key: 'badges', title: 'Match unknown badges', short: 'Badges', skippable: true,
    intro: 'Punches from badges that no Odoo employee carries are held here until you say who they belong to. '
      + 'Search for the employee and click <strong>Match</strong> — or <strong>Ignore</strong> a badge you don’t need.',
    help: 'Tip: put each person’s device user ID in their <em>Badge ID</em> field in Odoo '
      + '(Employee → HR Settings) and matching becomes automatic for everyone.',
    done: (s) => s.run && s.unmapped === 0,
  },
];

const SKIP_KEY = 'bb:setup-skipped';

function skippedSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem(`${SKIP_KEY}:${auth.tenant?.id}`) || '[]'));
  } catch {
    return new Set();
  }
}
function saveSkipped(set) {
  try { localStorage.setItem(`${SKIP_KEY}:${auth.tenant?.id}`, JSON.stringify([...set])); } catch { /* fine */ }
}

/** Where setup stands, from the server. */
export async function setupStatus() {
  const [dash, devices] = await Promise.all([
    api.get('/dashboard'),
    api.get('/devices').catch(() => []),
  ]);
  const health = dash.connection_health || {};
  const ok = (state) => Boolean(state) && state !== 'missing';
  return {
    odoo: ok(health.odoo),
    source: ok(health.source),
    devices: devices.length,
    run: Boolean(dash.last_run),
    pairing: pairingReviewed(),
    unmapped: dash.unmapped_employees || 0,
  };
}

/** True while the required steps (everything but badge matching) aren't all done (or skipped). */
export function setupIncomplete(status) {
  const skipped = skippedSet();
  return STEPS.filter((step) => !step.skippable).some((step) => !step.done(status) && !skipped.has(step.key));
}

function firstOpen(status) {
  const skipped = skippedSet();
  const i = STEPS.findIndex((step) => !step.done(status) && !skipped.has(step.key));
  return i === -1 ? STEPS.length : i;
}

/** Stops the previous visit's status poll. The router can render this page
 * again while an earlier render is still alive (a hashchange back to it, a
 * re-render after a save), and both share the same mount — so an old
 * instance, still holding its own step, would keep repainting the stepper
 * and footer every few seconds and the page would flip between two steps. */
let stopPrevious = () => {};

export async function render(mount, route) {
  stopPrevious();
  const run = Symbol('setup');
  mount.setupRun = run;
  if (!auth.canWrite) {
    mount.innerHTML = banner('Setup needs an admin',
      'Ask the account owner or an admin to connect Odoo and your biometric system.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  let status = await setupStatus();
  if (mount.setupRun !== run) return; // a newer render took over meanwhile
  const asked = Number(route?.query?.step);
  let index = Number.isInteger(asked) && asked >= 1 && asked <= STEPS.length + 1 ? asked - 1 : firstOpen(status);
  let poll = null;

  const stop = () => { if (poll) { clearInterval(poll); poll = null; } };
  stopPrevious = () => { stop(); if (mount.setupRun === run) mount.setupRun = null; };
  // The page's mount stays in the document across routes, so "still here"
  // means the address still points at this page *and* this is the newest
  // render of it — never an older one painting over it.
  const here = () => mount.setupRun === run && window.location.hash.startsWith('#/get-started');
  const go = (i) => {
    index = Math.max(0, Math.min(STEPS.length, i));
    history.replaceState(null, '', `#/get-started?step=${index + 1}`);
    paint();
  };

  function stepper() {
    const skipped = skippedSet();
    return `
      <ol class="wiz-steps setup-steps" aria-label="Setup progress">
        ${STEPS.map((step, i) => {
          const done = step.done(status);
          const cls = i === index ? 'current' : done ? 'done' : '';
          const mark = done ? '✓' : skipped.has(step.key) && i !== index ? '–' : i + 1;
          return `<li class="${cls}"><button type="button" class="link" data-go="${i}" ${
            i > firstOpen(status) && !done ? 'disabled' : ''}><b>${mark}</b>${esc(step.short)}</button></li>`;
        }).join('')}
      </ol>`;
  }

  function footer() {
    const step = STEPS[index];
    const done = step.done(status);
    const last = index === STEPS.length - 1;
    return `
      <div class="setup-foot">
        <div>${index > 0 ? '<button type="button" id="setupBack">← Back</button>' : ''}</div>
        <div class="setup-foot-status">${done
          ? `<span class="pill ok">Done</span> ${esc(step.short)} is set up.`
          : '<span class="hint">Finish this step to continue.</span>'}</div>
        <div class="row">
          ${step.skippable && !done ? '<button type="button" class="link" id="setupSkip">Skip this step</button>' : ''}
          <button type="button" class="primary" id="setupNext" ${done ? '' : 'disabled'}>${last ? 'Finish' : 'Continue →'}</button>
        </div>
      </div>`;
  }

  function paintChrome() {
    $('#setupStepper', mount).innerHTML = stepper();
    $('#setupFoot', mount).innerHTML = index < STEPS.length ? footer() : '';
    mount.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => go(Number(b.dataset.go))));
    $('#setupBack', mount)?.addEventListener('click', () => go(index - 1));
    $('#setupNext', mount)?.addEventListener('click', () => go(index + 1));
    $('#setupSkip', mount)?.addEventListener('click', () => {
      const skipped = skippedSet();
      skipped.add(STEPS[index].key);
      saveSkipped(skipped);
      go(index + 1);
    });
  }

  async function refreshStatus() {
    if (!here()) { stop(); return; }
    const before = STEPS[index]?.done(status);
    try { status = await setupStatus(); } catch { return; }
    if (!here()) { stop(); return; }
    paintChrome();
    if (index < STEPS.length && !before && STEPS[index].done(status)) {
      toast(`${STEPS[index].short} done — click Continue for the next step.`, 'ok');
      $('#setupNext', mount)?.focus();
    }
  }

  async function paintBody(body) {
    const step = STEPS[index];
    if (step.key === 'odoo') {
      await renderSettings(body, { path: '/settings/odoo', query: {} });
    } else if (step.key === 'biometric') {
      await renderSettings(body, { path: '/settings/biometric', query: {} });
      // No connection yet: open the add-connection wizard straight away.
      if (step.key === 'biometric' && !status.source) $('#addConnection', body)?.click();
    } else if (step.key === 'pairing') {
      body.innerHTML = '<div id="pairForm"></div><div id="pairConfirm"></div>';
      const formHost = $('#pairForm', body);
      // Saving the form counts as having reviewed it. Delegated, because the
      // form re-renders itself after every save.
      formHost.addEventListener('submit', () => {
        markPairingReviewed();
        setTimeout(refreshStatus, 900);
      });
      await renderSettings(formHost, { path: '/settings/pairing', query: {} });
      $('#pairConfirm', body).innerHTML = `
        <div class="card" style="margin-top:14px">
          <div class="row" style="justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
            <span class="hint">Happy with the defaults? You don’t have to change anything.</span>
            <button type="button" id="pairKeep">${status.pairing || status.run ? 'Keep these settings' : 'Looks right — keep these settings'}</button>
          </div>
        </div>`;
      $('#pairKeep', body).addEventListener('click', async () => {
        markPairingReviewed();
        await refreshStatus();
      });
    } else if (step.key === 'sync') {
      body.innerHTML = `
        <div class="card">
          <div class="card-head"><h2>First sync</h2></div>
          ${status.run ? '<p>A sync has already run. Run another any time from the top bar.</p>' : ''}
          <div class="row"><button type="button" class="primary" id="setupSync">${status.run ? 'Run again' : 'Run the first sync'}</button></div>
          <div id="setupSyncResult" style="margin-top:12px"></div>
        </div>`;
      const button = $('#setupSync', body);
      button.addEventListener('click', () => busy(button, () => guard(async () => {
        const run = await api.post('/sync/run-inline');
        $('#setupSyncResult', body).innerHTML = banner(
          run.status === 'success' ? 'Sync finished' : `Sync ${run.status}`,
          `${run.punches_new} new punch(es) read, ${run.attendances_created} attendance record(s) created, `
            + `${run.attendances_closed} closed.${run.error_message ? ` ${run.error_message}` : ''}`,
          run.status === 'success' ? '' : 'warn');
        await refreshStatus();
      })));
    } else if (step.key === 'badges') {
      const mappings = await api.get('/mappings?limit=500').catch(() => []);
      body.innerHTML = unmappedCard(mappings.filter(needsMatch));
      wireUnmapped(body, async () => { await paintBody(body); await refreshStatus(); });
    }
  }

  async function paint() {
    stop();
    if (index >= STEPS.length) {
      mount.innerHTML = `
        <div class="card setup-done">
          <div class="notice-icon" aria-hidden="true">✓</div>
          <h2>You're all set</h2>
          <p class="hint">Attendance now flows from your biometric system into Odoo every few minutes.
            New badges that Odoo doesn't recognise will show up on the Overview for you to match.</p>
          <div class="row" style="justify-content:center;margin-top:8px">
            <a class="btn primary-link" href="#/">Go to Overview</a>
            <a class="btn" href="#/attendance">See attendance</a>
          </div>
        </div>`;
      return;
    }
    const step = STEPS[index];
    mount.innerHTML = `
      <div class="setup-wizard">
        <div class="card setup-head">
          <div class="setup-head-top">
            <div><span class="hint">Step ${index + 1} of ${STEPS.length}</span><h2>${esc(step.title)}</h2></div>
            <a class="btn sm" href="#/" title="You can come back to this any time from the Overview">Exit setup</a>
          </div>
          <div id="setupStepper"></div>
          <p class="setup-intro">${step.intro}</p>
          <p class="hint setup-help">${step.help}</p>
        </div>
        <div id="setupBody" class="setup-body">${loading()}</div>
        <div id="setupFoot" class="card"></div>
      </div>`;
    paintChrome();
    await paintBody($('#setupBody', mount));
    if (!here()) return;
    stop();
    poll = setInterval(refreshStatus, 4000);
  }

  await paint();
}
