/* Router, app shell, and boot.
 *
 * Hash routing on purpose: it works behind any proxy or CDN with no rewrite
 * rules, which matters because this ships into customer infrastructure we do
 * not control.
 */

import { api, auth, loadSession, switchSession } from './api.js';
import { $, esc, fmtAgo, toast, wireSearchSelects, wireTips } from './ui.js';
import { renderLogin, renderPlans, renderSignup } from './pages/auth.js';
import { render as renderOverview } from './pages/overview.js';
import { renderActivity, renderAttendance, renderEmployees } from './pages/data.js';
import { renderTerminals } from './pages/terminals.js';
import { render as renderSettings } from './pages/settings.js';
import { render as renderPlatform } from './pages/platform.js';
import { render as renderPlanAdmin } from './pages/plans.js';
import { render as renderGetStarted } from './pages/getstarted.js';
import { render as renderMailAdmin } from './pages/mail.js';
import { render as renderPaymentsAdmin } from './pages/payments.js';
import { render as renderClosures } from './pages/closures.js';
import { render as renderLeads } from './pages/leads.js';
import { renderSetPassword, renderVerifyEmail, renderForgotPassword, renderResetPassword } from './pages/account.js';
import { renderConsoleOverview } from './pages/console.js';

const PUBLIC = new Set(['/login', '/signup', '/staff/login', '/plans', '/forgot-password']);
// Doors that render the same whether or not someone is signed in.
const ANYONE = new Set(['/verify-email', '/reset-password']);

/** Where an unauthenticated visitor lands, by path.
 *
 * There is one sign-in for everyone. Platform staff type their password in
 * the same form as customers; it opens a console session as well (see signIn
 * in pages/auth.js), and the sidebar switches between the two. */
const DOORS = {
  '/signup': renderSignup,
  // "Log in as admin" — the same page, aimed at the console. Linked from
  // under the sign-in card; see renderLogin.
  '/staff/login': renderLogin,
  // The full pricing comparison — reached from "Explore plans" on signup, and
  // from "Choose a plan"/"Change plan" in Settings for a signed-out visitor
  // who followed a bookmarked or shared link. Public: no session is needed to
  // compare plans, only to act on one.
  '/plans': renderPlans,
  // Where the confirmation email's link lands when there is no marketing
  // site to send it to (SITE_URL unset). Works signed in or out.
  '/verify-email': renderVerifyEmail,
  // Forgot password: ask for a link, then choose a new password from it.
  '/forgot-password': renderForgotPassword,
  '/reset-password': renderResetPassword,
};

/* Small line icons for the sidebar, inline so nothing is fetched. */
const ICON = {
  overview: '<path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 4v4h6V4z"/>',
  attendance: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4M8.5 14.5l2 2 4-4"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  employees: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.8-3.2 3-5 5.5-5s4.7 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M16 14c2.4 0 4 1.5 4.6 4"/>',
  connections: '<path d="M9 7H6a4 4 0 0 0 0 8h3M15 7h3a4 4 0 0 1 0 8h-3M8 11h8"/>',
  terminals: '<rect x="6" y="2.5" width="12" height="19" rx="2"/><rect x="8.5" y="5.5" width="7" height="5" rx="1"/><circle cx="12" cy="15.5" r="2.2"/>',
  platform: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  // A price tag.
  plans: '<path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3a1.4 1.4 0 0 1 0 2l-6.7 6.7a1.4 1.4 0 0 1-2 0z"/><circle cx="8" cy="8" r="1.5"/>',
  // An archive box.
  closed: '<rect x="3" y="4" width="18" height="5" rx="1.2"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
  // A card.
  card: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M3 10h18M7 15h4"/>',
  // An envelope.
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6.5 8.5-6.5"/>',
  // A cog: toothed wheel with a hub.
  settings: '<path d="M10.3 3.2h3.4l.5 2.4 1.9.8 2-1.4 2.4 2.4-1.4 2 .8 1.9 2.4.5v3.4l-2.4.5-.8 1.9 1.4 2-2.4 2.4-2-1.4-1.9.8-.5 2.4h-3.4l-.5-2.4-1.9-.8-2 1.4-2.4-2.4 1.4-2-.8-1.9-2.4-.5v-3.4l2.4-.5.8-1.9-1.4-2 2.4-2.4 2 1.4 1.9-.8z"/><circle cx="12" cy="12" r="3.2"/>',
};
const icon = (name) => `<svg class="nav-icon" viewBox="0 0 24 24" width="17" height="17" fill="none"
  stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

const NAV = [
  {
    label: 'Monitor',
    // Everything here is tenant-scoped. A platform user with no customer
    // account of their own has nothing to show — every one of these screens
    // would answer 403 — so the group is hidden and the console is all they see.
    tenantOnly: true,
    items: [
      { path: '/', title: 'Overview', icon: 'overview' },
      { path: '/attendance', title: 'Attendance', icon: 'attendance' },
      { path: '/employees', title: 'Employees', badge: 'unmapped', icon: 'employees' },
      { path: '/terminals', title: 'Terminals', icon: 'terminals' },
      { path: '/activity', title: 'Activity', icon: 'activity' },
    ],
  },
  {
    // A single main item, Settings, with every submenu nested under it and
    // always shown — the account's own rules, then the two connections
    // (Odoo: where attendance is written; Biometric: what it's written
    // from) — so the whole configuration menu is visible in the sidebar
    // itself, with nothing hidden behind a second click or an in-page tab
    // bar.
    tenantOnly: true,
    items: [
      {
        path: '/settings/general', title: 'Settings', icon: 'settings', prefix: '/settings',
        children: [
          { path: '/settings/general', title: 'General' },
          { path: '/settings/pairing', title: 'Pairing' },
          { path: '/settings/hours', title: 'Working hours' },
          { path: '/settings/plan', title: 'Plan' },
          { path: '/settings/billing', title: 'Billing' },
          { path: '/settings/odoo', title: 'Odoo connection' },
          { path: '/settings/biometric', title: 'Biometric connections' },
        ],
      },
    ],
  },
  {
    label: 'Platform',
    // Staff only. Hiding it is a courtesy, not the control — every /admin
    // request is checked server-side, so a hand-typed #/platform gets a
    // refusal rather than data.
    staffOnly: true,
    items: [
      { path: '/console', title: 'Overview', icon: 'overview' },
      { path: '/platform', title: 'All accounts', icon: 'platform', activeFor: ['/platform'] },
      { path: '/platform/plans', title: 'Plans', icon: 'plans' },
      { path: '/platform/leads', title: 'Leads', icon: 'mail' },
      { path: '/platform/closed', title: 'Closed accounts', icon: 'closed' },
      { path: '/platform/email', title: 'Email server', icon: 'mail' },
      { path: '/platform/payments', title: 'Payments', icon: 'card' },
    ],
  },
];

/** A path that has moved: what to swap the hash to instead of rendering.
 * Handled as a redirect *before* any route renders (see resolve()) — doing
 * it from inside a page's own render() raced the router's `running` guard
 * against a hashchange fired mid-render, which silently dropped it. */
const REDIRECTS = {
  // Connections used to be its own page at /setup. It is now the Odoo and
  // Biometric submenus under Settings — this keeps an old bookmark working
  // instead of landing on "page not found".
  '/setup': '/settings/odoo',
  // A bare /settings has no section of its own — land on the first one.
  '/settings': '/settings/general',
  // Changing your password moved into Settings → General.
  '/settings/password': '/settings/general',
};

/* Each screen's title, and one line under it saying what the screen is for. */
const ROUTES = {
  '/': { title: 'Overview', sub: 'Is attendance flowing, and what needs you', render: renderOverview },
  '/attendance': { title: 'Attendance', sub: 'Shifts written to Odoo, in your timezone', render: renderAttendance },
  '/activity': { title: 'Activity', sub: 'Every punch pulled, and every sync run', render: renderActivity },
  '/employees': { title: 'Employees', sub: 'Badges matched to Odoo employees', render: renderEmployees },
  '/terminals': { title: 'Terminals', sub: 'Every device your biometric connections bring in', render: renderTerminals },
  '/get-started': { title: 'Get set up', sub: 'Connect Odoo and your biometric system, one step at a time', render: renderGetStarted },
  '/settings/general': { title: 'General', sub: 'Company, timezone and sync schedule', render: renderSettings },
  '/settings/pairing': { title: 'Pairing', sub: 'How raw punches become shifts', render: renderSettings },
  '/settings/hours': { title: 'Working hours', sub: 'Working hours for late arrivals', render: renderSettings },
  '/settings/plan': { title: 'Plan', sub: 'Your subscription plan', render: renderSettings },
  '/settings/billing': { title: 'Billing', sub: 'Renewals, payment method and invoices', render: renderSettings },
  '/settings/plan/choose': { title: 'Choose a plan', sub: 'Compare plans and switch', render: renderSettings },
  '/settings/odoo': { title: 'Odoo connection', sub: 'Odoo connection, and badges waiting for a match', render: renderSettings },
  '/settings/biometric': { title: 'Biometric connections', sub: 'Biometric connections — where punches come from', render: renderSettings },
  '/console': { title: 'Platform overview', sub: 'Every account at a glance — health, growth and what needs a person', render: renderConsoleOverview },
  '/platform': { title: 'All accounts', sub: 'Every customer account on this platform', render: renderPlatform },
  '/platform/leads': { title: 'Leads', sub: 'Contact and demo requests from the website', render: renderLeads },
  '/platform/closed': { title: 'Closed accounts', sub: 'Deleted accounts — who closed them, and why', render: renderClosures },
  '/platform/payments': { title: 'Payments (Stripe)', sub: 'The Stripe keys, webhook and prices online billing runs on', render: renderPaymentsAdmin },
  '/platform/email': { title: 'Email server', sub: 'Where signup confirmations and login details are sent from', render: renderMailAdmin },
  '/platform/plans': { title: 'Plans', sub: 'The tiers accounts are sold under, and the limits each one enforces', render: renderPlanAdmin },
};

const badges = { unmapped: 0 };
/** What the top bar's sync button needs, from the last /dashboard read. */
const syncState = { lastRun: null, needsSetup: true };
// What is wrong right now (GET /alerts) — drives the bell in the top bar.
const alertState = { count: 0, worst: null, alerts: [] };

function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/';
  const [path, queryString] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(queryString || ''));
  const clean = path.length > 1 ? path.replace(/\/+$/, '') : path;
  return { path: clean || '/', query };
}

function mountShell() {
  $('#auth-root').classList.add('hidden');
  const root = $('#app-root');
  root.classList.remove('hidden');
  if (root.dataset.built) return root;
  root.dataset.built = '1';

  root.innerHTML = `
    <div class="shell">
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <span class="brand-mark">B</span>
          <span class="brand-word"><b>Bio</b><span>Bridge</span></span>
        </div>
        <nav id="sidenav"></nav>
        <div class="side-foot">
          <div class="side-user" id="sideUser"></div>
          <div id="consoleSwitch"></div>
          <div class="theme-toggle" id="themeSwitch" role="group" aria-label="Theme"></div>
          <button class="link" id="signOut" style="padding-left:0">Sign out</button>
        </div>
      </aside>
      <div class="main">
        <div class="topbar">
          <button id="menuToggle" class="sm">Menu</button>
          <div class="title-block">
            <h1 id="pageTitle"></h1>
            <div class="page-sub" id="pageSub"></div>
          </div>
          <div class="spacer"></div>
          <div class="top-actions" id="topActions"></div>
          <span id="tenantPill"></span>
        </div>
        <div class="content" id="content"></div>
      </div>
    </div>`;

  $('#signOut', root).addEventListener('click', async () => {
    // Both hats, if both are open: "Sign out" that left a console session
    // alive in the tab would be the surprising kind of convenience.
    // Failure is ignored: signing out locally is what actually matters.
    for (const token of auth.allRefreshTokens()) {
      try {
        await api.post(`/auth/logout?refresh_token=${encodeURIComponent(token)}`);
      } catch { /* already gone */ }
    }
    window.dispatchEvent(new CustomEvent('bb:signed-out'));
  });

  // One click between a dual-role person's workspace and the console. If the
  // session for the other side is still open it is just made active; if not
  // (the console session lives a day at most) the sign-in asks for the
  // password again, email filled in, and reopens both.
  $('#consoleSwitch', root).addEventListener('click', async (event) => {
    const button = event.target.closest('[data-switch]');
    if (!button) return;
    const target = button.dataset.switch;
    button.disabled = true;
    let switched = false;
    try {
      switched = await switchSession(target);
    } catch { /* fall through to the password prompt */ }
    button.disabled = false;
    if (switched) rememberHat(target);
    const destination = switched
      ? (target === 'staff' ? '#/console' : '#/')
      : `#/login?reauth=${target}`;
    if (window.location.hash === destination) resolve();
    else window.location.hash = destination;
  });

  watchAlerts();
  paintThemeSwitch();
  $('#themeSwitch', root).addEventListener('click', (event) => {
    const button = event.target.closest('[data-theme-choice]');
    if (button) setTheme(button.dataset.themeChoice);
  });

  // Sync now, from any screen — it used to live only on the Overview card.
  $('#topActions', root).addEventListener('click', async (event) => {
    const button = event.target.closest('#topSync');
    if (!button) return;
    button.disabled = true;
    button.classList.add('spinning');
    try {
      const result = await api.post('/sync/run-inline');
      toast(`Sync ${result.status} — ${result.punches_new} new punch${result.punches_new === 1 ? '' : 'es'}, `
        + `${result.attendances_created} created, ${result.attendances_closed} closed`,
      result.status === 'failed' ? 'bad' : 'ok');
    } catch (error) {
      if (error.status !== 401) toast(error.message || 'Sync failed', 'bad');
    }
    button.disabled = false;
    button.classList.remove('spinning');
    resolve();   // every screen shows something the sync may have changed
  });

  $('#menuToggle', root).addEventListener('click', () =>
    $('#sidebar', root).classList.toggle('open')
  );

  return root;
}

/* Light, dark, or whatever the computer is set to. js/theme.js applies the
 * stored choice before first paint; this is the control that changes it. */
const THEMES = [
  { value: 'system', label: 'Auto' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
];

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') || 'system';
}

function setTheme(value) {
  if (value === 'light' || value === 'dark') {
    document.documentElement.setAttribute('data-theme', value);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  try {
    if (value === 'light' || value === 'dark') localStorage.setItem('bb.theme', value);
    else localStorage.removeItem('bb.theme');
  } catch { /* storage blocked: the choice lasts until the tab closes */ }
  paintThemeSwitch();
}

function paintThemeSwitch() {
  const holder = $('#themeSwitch');
  if (!holder) return;
  const active = currentTheme();
  holder.innerHTML = THEMES.map((t) => `
    <button type="button" data-theme-choice="${t.value}" aria-pressed="${t.value === active}"
            title="${t.value === 'system' ? 'Follow this computer’s setting' : `Always ${t.label.toLowerCase()}`}">
      ${t.label}</button>`).join('');
}

/** Which hat to land in at the next sign-in on this browser. */
function rememberHat(scope) {
  try {
    localStorage.setItem('bb.hat', scope);
  } catch { /* a convenience only */ }
}

function renderChrome(path) {
  $('#sidenav').innerHTML = NAV.filter(
    (group) => (!group.staffOnly || auth.isPlatformAdmin)
            && (!group.tenantOnly || auth.tenant)
  ).map((group) => `
    <div class="nav-group"><div class="nav-group-label">${esc(group.label)}</div></div>
    ${group.items.map((item) => {
      if (item.children) {
        // A parent whose submenus only appear once you're in its section:
        // clicking the parent opens its first submenu, and the rest unfold
        // under it. Anywhere else in the app they stay folded away. The
        // parent picks up the subtle "current section" treatment; the pill
        // highlight belongs to whichever child is open.
        const inSection = item.prefix && path.startsWith(item.prefix);
        const childrenHtml = !inSection ? '' : item.children.map((child) => {
          const childActive = child.path === path;
          return `<a class="nav nav-child ${childActive ? 'active' : ''}" href="#${esc(child.path)}"${childActive ? ' aria-current="page"' : ''}>
            <span class="nav-label">${esc(child.title)}</span>
          </a>`;
        }).join('');
        return `<a class="nav nav-parent ${inSection ? 'in-section' : ''}" href="#${esc(item.path)}">
          <span class="nav-label">${item.icon ? icon(item.icon) : ''}${esc(item.title)}</span>
        </a>${childrenHtml}`;
      }
      const active = item.activeFor
        ? item.activeFor.includes(path)
        : item.path === path || (item.path !== '/' && path.startsWith(item.path + '/'));
      const count = item.badge ? badges[item.badge] : 0;
      return `<a class="nav ${active ? 'active' : ''}" href="#${esc(item.path)}"${active ? ' aria-current="page"' : ''}>
        <span class="nav-label">${item.icon ? icon(item.icon) : ''}${esc(item.title)}</span>
        ${count ? `<span class="nav-badge">${esc(count)}</span>` : ''}
      </a>`;
    }).join('')}`).join('');

  // The top bar's sync button: a customer session, a role that can write,
  // and both sides connected — otherwise it would only fail.
  const run = syncState.lastRun;
  $('#topActions').innerHTML = auth.tenant && !auth.isStaffSession ? `
    ${bellHtml()}
    <span class="last-sync" title="${esc(run ? `Last sync ${run.status}` : 'No sync has run yet')}">
      ${run ? `<span class="dot ${run.status === 'success' ? 'ok' : run.status === 'failed' ? 'bad' : 'warn'}"></span>
        Synced ${esc(fmtAgo(run.started_at))}` : 'Never synced'}
    </span>
    ${auth.canWrite ? `<button class="sm" id="topSync" aria-label="Sync now" ${syncState.needsSetup ? 'disabled title="Connect Odoo and a biometric source first"' : ''}>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>
      <span class="sync-label">Sync now</span></button>` : ''}` : '';

  const sideUser = $('#sideUser');
  sideUser.textContent = auth.user?.email || '';
  sideUser.title = auth.user?.email || '';   // the truncated address, in full, on hover

  // Someone who is both staff and a customer wears one hat at a time — each
  // is its own session — and switches here. Offered rather than hidden,
  // because the console is unadvertised and there is no other way to find it.
  $('#consoleSwitch').innerHTML =
    auth.user?.is_platform_admin === true && !auth.isStaffSession
      ? '<button type="button" class="sm hat-switch" data-switch="staff">Staff console &rsaquo;</button>'
      : auth.isStaffSession && auth.user?.tenant_id
        ? '<button type="button" class="sm hat-switch" data-switch="tenant">&lsaquo; My workspace</button>'
        : '';
  // Three states, and the order matters. A stopped account used to show the
  // green pill — the pill was keyed on sync_enabled alone, so an account the
  // platform had suspended looked perfectly healthy while nothing synced. The
  // platform's decision outranks the customer's own switch here, because it is
  // the one they cannot do anything about from this screen.
  $('#tenantPill').innerHTML = auth.tenant
    ? (() => {
        const t = auth.tenant;
        const [tone, note] = t.syncable === false
          ? ['bad', ' · sync stopped']
          : t.sync_enabled ? ['ok', ''] : ['warn', ' · sync off'];
        return `<span class="pill ${tone}">${esc(t.name)}${note}</span>`;
      })()
    : auth.isPlatformAdmin
      ? '<span class="pill">platform staff</span>'
      : '';
  $('#sidebar').classList.remove('open');
}

const BELL = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>';

function bellHtml() {
  const n = alertState.count;
  const label = n ? `${n} alert${n === 1 ? '' : 's'} need attention` : 'No alerts';
  return `<button class="sm bell ${n ? alertState.worst : ''}" id="alertBell" aria-haspopup="true"
      aria-label="${esc(label)}" title="${esc(label)}">${BELL}${n ? `<span class="bell-count">${n > 9 ? '9+' : n}</span>` : ''}</button>`;
}

function closeAlertPanel() {
  const panel = $('#alertPanel');
  if (panel) panel.remove();
}

function openAlertPanel() {
  closeAlertPanel();
  const bell = $('#alertBell');
  if (!bell) return;
  const items = alertState.alerts.map((a) => `
    <li class="alert-item ${esc(a.severity)}">
      <strong>${esc(a.title)}</strong>
      <p>${esc(a.detail)}</p>
      ${a.href ? `<a href="${esc(a.href)}">${esc(a.action || 'Open')} &rsaquo;</a>` : ''}
    </li>`).join('');
  const panel = document.createElement('div');
  panel.id = 'alertPanel';
  panel.className = 'alert-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Alerts');
  panel.innerHTML = `<div class="alert-head">Alerts</div>${items
    ? `<ul>${items}</ul>`
    : '<div class="alert-none"><span class="dot ok"></span> Everything is running normally.</div>'}`;
  document.body.appendChild(panel);
  const r = bell.getBoundingClientRect();
  panel.style.top = `${r.bottom + 8}px`;
  panel.style.right = `${Math.max(12, window.innerWidth - r.right)}px`;
}

/** Re-read the alerts and repaint just the bell. Never breaks the page. */
async function refreshAlerts() {
  if (!auth.tenant || auth.isStaffSession) return;
  try {
    const data = await api.get('/alerts');
    alertState.count = data.count;
    alertState.worst = data.worst;
    alertState.alerts = data.alerts;
  } catch { return; }
  const bell = $('#alertBell');
  if (bell) bell.outerHTML = bellHtml();
  if ($('#alertPanel')) openAlertPanel();
}

let alertTimer = null;
function watchAlerts() {
  if (alertTimer) return;
  alertTimer = setInterval(() => { if (!document.hidden) refreshAlerts(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAlerts(); });
  document.addEventListener('click', (event) => {
    if (event.target.closest('#alertBell')) {
      if ($('#alertPanel')) closeAlertPanel(); else openAlertPanel();
    } else if (!event.target.closest('#alertPanel') || event.target.closest('#alertPanel a')) {
      closeAlertPanel();
    }
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeAlertPanel(); });
}

/** Badges are decorative: never let them break navigation. */
async function refreshBadges() {
  if (!auth.tenant) return;  // staff-only: there is no dashboard to count
  try {
    const data = await api.get('/dashboard');
    badges.unmapped = data.unmapped_employees || 0;
    await refreshAlerts();
    syncState.lastRun = data.last_run || null;
    const health = data.connection_health || {};
    syncState.needsSetup = health.odoo === 'missing' || health.source === 'missing';
  } catch { /* leave the previous value */ }
}

let running = false;
// A navigation asked for while a page was still rendering — a hashchange, or
// a session switch after an expiry — is run once that render finishes rather
// than dropped.
let rerun = false;

async function resolve() {
  if (running) {
    rerun = true;
    return;
  }
  running = true;
  try {
    const route = parseHash();
    if (ANYONE.has(route.path)) return DOORS[route.path](route);

    // Rehydrate from a refresh token surviving a page reload.
    if (!auth.isAuthenticated && auth.restore()) {
      try {
        const tokens = await (await fetch(
          `/api/v1/auth/refresh?refresh_token=${encodeURIComponent(auth.refreshToken)}`,
          { method: 'POST' }
        )).json();
        if (tokens.access_token) {
          auth.persist(tokens);
          await loadSession();
        }
      } catch {
        auth.clear();
      }
    }

    if (!auth.isAuthenticated) {
      return (DOORS[route.path] || renderLogin)(route);
    }

    if (PUBLIC.has(route.path)) {
      // Signed in already. The one reason to show the sign-in anyway: a
      // switch to the other session found it expired, so it asks for the
      // password again — see the #consoleSwitch handler.
      if (route.path === '/login' && route.query.reauth) {
        if (!auth.user) await loadSession();
        return renderLogin(route);
      }
      window.location.hash = '#/';
      return;
    }

    if (!auth.user) {
      try {
        await loadSession();
      } catch {
        auth.clear();
        return (DOORS[route.path] || renderLogin)(route);
      }
    }

    // Signed in with the emailed password: nothing else until they set their own.
    if (auth.user?.must_change_password) return renderSetPassword();

    // A platform user with no customer account has no Overview to land on —
    // every tenant-scoped screen would 403. Send them to the console instead of
    // showing an error page on the way in.
    // The console's landing page is its overview.
    if (!auth.tenant && auth.isPlatformAdmin && !['/platform', '/platform/plans', '/platform/email', '/platform/payments', '/platform/closed', '/platform/leads', '/console'].includes(route.path)) {
      window.location.hash = '#/console';
      return;
    }

    if (REDIRECTS[route.path]) {
      window.location.hash = `#${REDIRECTS[route.path]}`;
      return;
    }

    const entry = ROUTES[route.path];
    mountShell();
    renderChrome(route.path);
    $('#pageTitle').textContent = entry ? entry.title : 'Not found';
    $('#pageSub').textContent = entry?.sub || '';

    const content = $('#content');
    if (!entry) {
      content.innerHTML = '<div class="empty"><strong>Page not found</strong>'
        + '<a href="#/">Back to the overview</a></div>';
      return;
    }

    try {
      await entry.render(content, route);
    } catch (error) {
      if (error.status === 401) return; // api.js already signalled sign-out
      content.innerHTML = `<div class="banner bad"><strong>Could not load this page</strong>${
        esc(error.message || 'Unknown error')}</div>`;
    }

    await refreshBadges();
    renderChrome(route.path);
  } finally {
    running = false;
    if (rerun) {
      rerun = false;
      resolve();
    }
  }
}

/** Go to ``hash`` and render it exactly once. Setting a different hash fires
 * hashchange, which already calls resolve(); calling resolve() as well
 * rendered the page twice — and two live copies of a polling page (Get set
 * up) then fought over the same screen. */
function navigate(hash) {
  if (window.location.hash !== hash) window.location.hash = hash;
  else resolve();
}

window.addEventListener('bb:signed-in', async (event) => {
  try {
    await loadSession();
  } catch { /* resolve() will retry */ }
  if (auth.user?.is_platform_admin) rememberHat(auth.scope);
  navigate(event.detail?.next || '#/');
});

window.addEventListener('bb:signed-out', async (event) => {
  // One session expired, not a sign-out: if the other hat is still open, carry
  // on in it rather than dropping the person at a login page.
  if (event.detail?.expired) {
    const other = auth.lastDoor === 'staff' ? 'tenant' : 'staff';
    let switched = false;
    try {
      switched = await switchSession(other);
    } catch { /* sign out below */ }
    if (switched) {
      toast(other === 'tenant'
        ? 'Your console session expired — you are back in your workspace.'
        : 'Your workspace session expired — you are in the staff console.', 'ok');
      navigate(other === 'staff' ? '#/console' : '#/');
      return;
    }
  }
  auth.clear();
  $('#app-root').classList.add('hidden');
  $('#app-root').innerHTML = '';
  delete $('#app-root').dataset.built;
  navigate('#/login');
});

window.addEventListener('bb:toast', (event) => toast(event.detail, 'ok'));
window.addEventListener('hashchange', resolve);
wireTips();
wireSearchSelects();

resolve();
