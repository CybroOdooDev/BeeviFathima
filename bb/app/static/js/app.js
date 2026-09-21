/* Router, app shell, and boot.
 *
 * Hash routing on purpose: it works behind any proxy or CDN with no rewrite
 * rules, which matters because this ships into customer infrastructure we do
 * not control.
 */

import { api, auth, loadSession } from './api.js';
import { $, esc, toast } from './ui.js';
import { renderLogin, renderSignup, renderStaffLogin } from './pages/auth.js';
import { render as renderOverview } from './pages/overview.js';
import { render as renderSetup } from './pages/setup.js';
import { renderActivity, renderAttendance, renderEmployees } from './pages/data.js';
import { render as renderSettings } from './pages/settings.js';
import { render as renderPlatform } from './pages/platform.js';

const PUBLIC = new Set(['/login', '/signup', '/staff/login']);

/** Where an unauthenticated visitor lands, by path.
 *
 * The staff console has its own entry so a console credential is never typed
 * into the customer form. It is unadvertised — nothing links to it from the
 * product — which is not a security measure (the server checks the scope of
 * every token) but does keep the two audiences from drifting onto each
 * other's page. */
const DOORS = {
  '/staff/login': renderStaffLogin,
  '/signup': renderSignup,
};

const NAV = [
  {
    label: 'Monitor',
    // Everything in these two groups is tenant-scoped. A platform user with no
    // customer account of their own has nothing to show there — every one of
    // these screens would answer 403 — so the whole group is hidden and the
    // console is all they see.
    tenantOnly: true,
    items: [
      { path: '/', title: 'Overview' },
      { path: '/attendance', title: 'Attendance' },
      { path: '/activity', title: 'Activity' },
    ],
  },
  {
    label: 'Configure',
    tenantOnly: true,
    items: [
      { path: '/employees', title: 'Employees', badge: 'unmapped' },
      { path: '/setup', title: 'Connections' },
      { path: '/settings', title: 'Settings' },
    ],
  },
  {
    label: 'Platform',
    // Staff only. Hiding it is a courtesy, not the control — every /admin
    // request is checked server-side, so a hand-typed #/platform gets a
    // refusal rather than data.
    staffOnly: true,
    items: [{ path: '/platform', title: 'All accounts' }],
  },
];

const ROUTES = {
  '/': { title: 'Overview', render: renderOverview },
  '/attendance': { title: 'Attendance', render: renderAttendance },
  '/activity': { title: 'Activity', render: renderActivity },
  '/employees': { title: 'Employees', render: renderEmployees },
  '/setup': { title: 'Connections', render: renderSetup },
  '/settings': { title: 'Settings', render: renderSettings },
  '/platform': { title: 'All accounts', render: renderPlatform },
};

const badges = { unmapped: 0 };

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
        <div class="brand"><span class="brand-dot"></span>BioBridge</div>
        <nav id="sidenav"></nav>
        <div class="side-foot">
          <div class="side-user" id="sideUser"></div>
          <div id="consoleSwitch"></div>
          <button class="link" id="signOut" style="padding-left:0">Sign out</button>
        </div>
      </aside>
      <div class="main">
        <div class="topbar">
          <button id="menuToggle" class="sm">Menu</button>
          <h1 id="pageTitle"></h1>
          <div class="spacer"></div>
          <span id="tenantPill"></span>
        </div>
        <div class="content" id="content"></div>
      </div>
    </div>`;

  $('#signOut', root).addEventListener('click', async () => {
    // Failure is ignored: signing out locally is what actually matters.
    try {
      if (auth.refreshToken) {
        await api.post(`/auth/logout?refresh_token=${encodeURIComponent(auth.refreshToken)}`);
      }
    } catch { /* already gone */ }
    window.dispatchEvent(new CustomEvent('bb:signed-out'));
  });

  $('#menuToggle', root).addEventListener('click', () =>
    $('#sidebar', root).classList.toggle('open')
  );

  return root;
}

function renderChrome(path) {
  $('#sidenav').innerHTML = NAV.filter(
    (group) => (!group.staffOnly || auth.isPlatformAdmin)
            && (!group.tenantOnly || auth.tenant)
  ).map((group) => `
    <div class="nav-group"><div class="nav-group-label">${esc(group.label)}</div></div>
    ${group.items.map((item) => {
      const active = item.path === path
        || (item.path !== '/' && path.startsWith(item.path + '/'));
      const count = item.badge ? badges[item.badge] : 0;
      return `<a class="nav ${active ? 'active' : ''}" href="#${esc(item.path)}">
        <span>${esc(item.title)}</span>
        ${count ? `<span class="nav-badge">${esc(count)}</span>` : ''}
      </a>`;
    }).join('')}`).join('');

  const sideUser = $('#sideUser');
  sideUser.textContent = auth.user?.email || '';
  sideUser.title = auth.user?.email || '';   // the truncated address, in full, on hover

  // Staff signed in to their own workspace: the console is a different session,
  // reached through its own door. Offered rather than hidden, because the page
  // is unadvertised and they would otherwise have no way to find it.
  $('#consoleSwitch').innerHTML =
    auth.user?.is_platform_admin === true && !auth.isStaffSession
      ? '<a class="link" href="#/staff/login">Open the staff console &rsaquo;</a>'
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

/** Badges are decorative: never let them break navigation. */
async function refreshBadges() {
  if (!auth.tenant) return;  // staff-only: there is no dashboard to count
  try {
    const data = await api.get('/dashboard');
    badges.unmapped = data.unmapped_employees || 0;
  } catch { /* leave the previous value */ }
}

let running = false;

async function resolve() {
  if (running) return;
  running = true;
  try {
    const route = parseHash();

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
      return (DOORS[route.path] || renderLogin)();
    }

    if (PUBLIC.has(route.path)) {
      // One exception to the bounce: someone who holds the staff flag but is
      // signed in to their own workspace has no other route to the console,
      // and signing in at that door swaps the session rather than adding a
      // second one. Without this they would have to sign out first to find a
      // page nothing links to.
      const switchingHats = route.path === '/staff/login'
        && auth.user?.is_platform_admin === true
        && !auth.isStaffSession;
      if (!switchingHats) {
        window.location.hash = '#/';
        return;
      }
      return renderStaffLogin();
    }

    if (!auth.user) {
      try {
        await loadSession();
      } catch {
        auth.clear();
        return (DOORS[route.path] || renderLogin)();
      }
    }

    // A platform user with no customer account has no Overview to land on —
    // every tenant-scoped screen would 403. Send them to the console instead of
    // showing an error page on the way in.
    if (!auth.tenant && auth.isPlatformAdmin && route.path !== '/platform') {
      window.location.hash = '#/platform';
      return;
    }

    const entry = ROUTES[route.path];
    mountShell();
    renderChrome(route.path);
    $('#pageTitle').textContent = entry ? entry.title : 'Not found';

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
  }
}

window.addEventListener('bb:signed-in', async () => {
  try {
    await loadSession();
  } catch { /* resolve() will retry */ }
  window.location.hash = '#/';
  resolve();
});

window.addEventListener('bb:signed-out', () => {
  auth.clear();
  $('#app-root').classList.add('hidden');
  $('#app-root').innerHTML = '';
  delete $('#app-root').dataset.built;
  // Back to the door this session came in through. Sending a support engineer
  // to the customer login would have them type a console credential into the
  // customer form, which is the one habit the split is meant to break.
  window.location.hash = auth.lastDoor === 'staff' ? '#/staff/login' : '#/login';
  resolve();
});

window.addEventListener('bb:toast', (event) => toast(event.detail, 'ok'));
window.addEventListener('hashchange', resolve);

resolve();
