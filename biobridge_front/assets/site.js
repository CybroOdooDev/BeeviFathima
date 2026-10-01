/* BioBridge marketing site — the only script:
 *   1. point every sign-in link at the app (one setting, below),
 *   2. the mobile menu,
 *   3. the contact form: posts to FORM_ENDPOINT, or opens an email if unset,
 *   4. live plans and prices from the app (pricing, home, signup),
 *   5. registration: free trial, or buy through Stripe Checkout,
 *   6. the "check your inbox" and "email confirmed" pages.
 *
 * The app must list this site's origin in CORS_ORIGINS, and have SITE_URL
 * set to it, so emails and Stripe send people back here.
 */

// ---- settings: change these two lines when you deploy -------------------
const APP_URL = 'http://localhost:8000';          // where BioBridge runs — just the address, e.g. http://127.0.0.1:8000
const FORM_ENDPOINT = '';                            // e.g. a Formspree / Basin URL; blank = email fallback
const SALES_EMAIL = 'sales@example.com';             // used by the email fallback
// --------------------------------------------------------------------------

// Which app page each kind of link opens. Registration happens on this site
// (signup.html); only signing in goes to the app.
const APP_ROUTES = {
  signin: '/app/#/login',    // "Sign in"
};

// Only the scheme + host + port of APP_URL is used. Pasting the app's full
// address (".../app/#/login") would otherwise glue the route onto the end of
// it, and the app, not recognising that, shows its login page for everything.
let appOrigin;
try {
  appOrigin = new URL(APP_URL).origin;
} catch {
  appOrigin = APP_URL.replace(/\/+$/, '');
}

document.querySelectorAll('[data-app]').forEach((link) => {
  const route = APP_ROUTES[link.dataset.app];
  if (route) link.href = appOrigin + route;
});

const API = `${appOrigin}/api/v1`;

document.querySelectorAll('[data-year]').forEach((el) => { el.textContent = new Date().getFullYear(); });

// ---- mobile menu ----------------------------------------------------------
const header = document.querySelector('.site-header');
const menuBtn = document.querySelector('.menu-btn');
if (header && menuBtn) {
  menuBtn.addEventListener('click', () => {
    const open = header.classList.toggle('open');
    menuBtn.setAttribute('aria-expanded', String(open));
    // The buttons sit directly under the links, however tall the links end up.
    const nav = header.querySelector('.nav');
    if (open && nav) header.style.setProperty('--menu-cta-top', `${nav.offsetTop + nav.offsetHeight}px`);
  });
  header.querySelectorAll('.nav a').forEach((a) => a.addEventListener('click', () => {
    header.classList.remove('open');
    menuBtn.setAttribute('aria-expanded', 'false');
  }));
}

// ---- setup guide: highlight the section in view ----------------------------
const toc = document.querySelectorAll('.toc a[href^="#"]');
if (toc.length && 'IntersectionObserver' in window) {
  const byId = new Map([...toc].map((a) => [a.getAttribute('href').slice(1), a]));
  const seen = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      toc.forEach((a) => a.classList.remove('active'));
      byId.get(entry.target.id)?.classList.add('active');
    });
  }, { rootMargin: '-30% 0px -60% 0px' });
  byId.forEach((_a, id) => { const el = document.getElementById(id); if (el) seen.observe(el); });
}

// ---- contact form -----------------------------------------------------------
const form = document.querySelector('#contact-form');
if (form) {
  const status = form.querySelector('.form-status');
  const say = (text, tone) => { status.textContent = text; status.className = `form-status ${tone}`; };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form));

    if (!FORM_ENDPOINT) {
      // No form service configured: hand the details to the visitor's mail app.
      const body = Object.entries(data).map(([k, v]) => `${k}: ${v}`).join('\n');
      window.location.href = `mailto:${SALES_EMAIL}?subject=${encodeURIComponent(
        `BioBridge — ${data.topic || 'enquiry'} from ${data.company || data.name}`
      )}&body=${encodeURIComponent(body)}`;
      return;
    }

    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    say('Sending…', '');
    try {
      const response = await fetch(FORM_ENDPOINT, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!response.ok) throw new Error(String(response.status));
      form.reset();
      say('Thanks — we have your request and will be in touch.', 'ok');
    } catch {
      say(`That didn't go through. Please email us at ${SALES_EMAIL}.`, 'bad');
    } finally {
      button.disabled = false;
    }
  });
}


// ---- the app's API ------------------------------------------------------------
async function callApi(path, body) {
  let response;
  try {
    response = await fetch(`${API}${path}`, body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (exc) {
    // The browser never got an answer: the app is down or unreachable at
    // APP_URL, or it answered without allowing this site (CORS_ORIGINS).
    // Visitors get a plain message; the cause goes to the console.
    console.error(`BioBridge: could not reach ${API}${path} from ${window.location.origin}. `
      + 'Check APP_URL in assets/site.js, that the app is running, and that the app\'s CORS_ORIGINS '
      + `includes ${window.location.origin}.`, exc);
    const error = new Error('We couldn\u2019t reach BioBridge just now. Please try again in a minute.');
    error.status = 0;
    throw error;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload && payload.detail;
    const text = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? 'Please check the highlighted fields.' : 'Something went wrong. Please try again.';
    const error = new Error(text);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const money = (cents) => (cents == null ? 'Custom' : `$${(cents / 100).toFixed(cents % 100 ? 2 : 0)}`);
const limitsOf = (p) => [
  p.max_employees != null ? `Up to ${p.max_employees} employees` : 'No employee cap',
  p.max_devices != null ? `Up to ${p.max_devices} device${p.max_devices === 1 ? '' : 's'}` : 'Unlimited devices',
  p.min_sync_interval_minutes ? `Syncs as often as every ${p.min_sync_interval_minutes} min` : 'Syncs as often as you like',
];

// Plans come from the app when it's reachable; the prices written into the
// HTML are the fallback, so a page never shows nothing.
let plansPromise = null;
function livePlans() {
  if (!plansPromise) plansPromise = callApi('/public/plans').catch(() => null);
  return plansPromise;
}

// Pricing and home: fill prices and limits in, and hide "buy now" for any
// plan that can't be bought online yet.
if (document.querySelector('[data-plan], .mini-plan')) {
  livePlans().then((plans) => {
    if (!plans) return;
    const byName = Object.fromEntries(plans.map((p) => [p.name, p]));
    document.querySelectorAll('[data-plan]').forEach((card) => {
      const p = byName[card.dataset.plan];
      if (!p) return;
      const price = card.querySelector('.price');
      if (price) price.innerHTML = `${money(p.monthly_price_cents)}<small> / month</small>`;
      const items = card.querySelectorAll('.ticks li span');
      const [emp, dev, sync] = limitsOf(p);
      if (items[0]) items[0].textContent = emp;
      if (items[1]) items[1].textContent = sync;
      if (items[2]) items[2].textContent = dev;
      const buy = card.querySelector('[data-buy]');
      if (buy) buy.hidden = !p.can_buy_online;
    });
    document.querySelectorAll('.mini-plan').forEach((card) => {
      const p = byName[card.querySelector('.name')?.textContent.trim()];
      if (!p) return;
      card.querySelector('.price').innerHTML = `${money(p.monthly_price_cents)}<small>/mo</small>`;
      const [emp, dev] = limitsOf(p);
      card.querySelector('.cap').textContent = `${emp.replace('Up to ', '')} · ${dev.replace('Up to ', '')}`;
    });
  });
}

// ---- registration (signup.html) -------------------------------------------------
const signupForm = document.querySelector('#signup-form');
if (signupForm) {
  const params = new URLSearchParams(window.location.search);
  const planSelect = signupForm.querySelector('#planSelect');
  const tzSelect = signupForm.querySelector('#tzSelect');
  const go = signupForm.querySelector('#signupGo');
  const status = signupForm.querySelector('.form-status');
  const note = signupForm.querySelector('#planNote');
  const notice = signupForm.querySelector('#signupNotice');
  const summary = document.querySelector('#planSummary');
  const say = (text, tone = '') => { status.textContent = text; status.className = `form-status ${tone}`; };
  let plans = null;

  // Timezones: every IANA zone the browser knows, with the visitor's own —
  // read from the browser — selected and listed first. Older browsers report
  // a few zones under their pre-2016 names; those are mapped to the current
  // name so the right entry is picked. Anything unreadable falls back to UTC.
  const LEGACY = {
    'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Saigon': 'Asia/Ho_Chi_Minh',
    'Asia/Rangoon': 'Asia/Yangon', 'Europe/Kiev': 'Europe/Kyiv', 'America/Buenos_Aires': 'America/Argentina/Buenos_Aires',
    'Etc/UTC': 'UTC', 'Etc/GMT': 'UTC', 'GMT': 'UTC', 'Etc/Unknown': 'UTC',
  };
  let here = 'UTC';
  try { here = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { /* keep UTC */ }
  here = LEGACY[here] || here;
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone').map((z) => LEGACY[z] || z); } catch { zones = []; }
  zones = [here, ...['UTC', ...zones].filter((z) => z !== here)].filter((z, i, all) => all.indexOf(z) === i);
  tzSelect.innerHTML = zones.map((z, i) => `<option value="${z}"${i === 0 ? ' selected' : ''}>${
    i === 0 && here !== 'UTC' ? `${z} (your timezone)` : z}</option>`).join('');

  const mode = () => signupForm.querySelector('[name=mode]:checked').value;
  const current = () => plans && plans.find((p) => p.name === planSelect.value);

  const paint = () => {
    const p = current();
    const buying = mode() === 'buy';
    go.textContent = buying ? 'Continue to payment' : 'Start 10-day free trial';
    note.textContent = '';
    if (buying && p && !p.can_buy_online) {
      note.textContent = `${p.name} can't be bought online yet — start a free trial, or contact us.`;
    }
    go.disabled = Boolean(buying && p && !p.can_buy_online);
    if (p && summary) {
      summary.hidden = false;
      summary.innerHTML = `<span class="eyebrow">${buying ? 'You are buying' : 'Your trial plan'}</span>
        <span class="name">${p.name}</span>
        <span class="price">${money(p.monthly_price_cents)}<small> / month${buying ? '' : ' after the trial'}</small></span>
        <ul>${limitsOf(p).map((l) => `<li>${l}</li>`).join('')}</ul>`;
    }
  };

  const wanted = params.get('plan');
  if (wanted) {
    const match = [...planSelect.options].find((o) => o.value.toLowerCase() === wanted.toLowerCase());
    if (match) planSelect.value = match.value;
  }
  if (params.get('mode') === 'buy') signupForm.querySelector('[name=mode][value=buy]').checked = true;
  if (params.get('cancelled')) {
    notice.hidden = false;
    notice.className = 'notice warn';
    notice.textContent = 'Payment was cancelled — nothing was charged. You can try again, or start a free trial instead.';
  }

  livePlans().then((list) => {
    if (!list || !list.length) return;
    plans = list;
    const keep = planSelect.value;
    planSelect.innerHTML = list.map((p) =>
      `<option value="${p.name}">${p.name} — ${money(p.monthly_price_cents)}/mo</option>`).join('');
    planSelect.value = list.some((p) => p.name === keep) ? keep
      : (list.find((p) => p.is_default) || list[0]).name;
    paint();
  });
  paint();
  signupForm.addEventListener('change', paint);

  signupForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!signupForm.reportValidity()) return;
    const data = Object.fromEntries(new FormData(signupForm));
    go.disabled = true;
    say(data.mode === 'buy' ? 'Opening the payment page…' : 'Creating your account…');
    try {
      const result = await callApi('/public/register', data);
      try { sessionStorage.setItem('bb-email', data.email); } catch { /* private mode */ }
      if (result.next === 'checkout' && result.checkout_url) {
        window.location.href = result.checkout_url;
        return;
      }
      window.location.href = `check-email.html?email=${encodeURIComponent(data.email)}`;
    } catch (error) {
      say(error.message, 'bad');
      go.disabled = false;
    }
  });
}

// ---- check-email.html ------------------------------------------------------------
const resendBtn = document.querySelector('#resendBtn');
if (resendBtn) {
  const params = new URLSearchParams(window.location.search);
  let email = params.get('email') || '';
  try { email = email || sessionStorage.getItem('bb-email') || ''; } catch { /* private mode */ }
  if (email) document.querySelector('#checkEmail').textContent = email;
  if (params.get('paid')) document.querySelector('#checkPaid').hidden = false;
  const status = document.querySelector('#resendStatus');
  resendBtn.hidden = !email;
  resendBtn.addEventListener('click', async () => {
    resendBtn.disabled = true;
    status.className = 'form-status';
    status.textContent = 'Sending…';
    try {
      const result = await callApi('/public/resend', { email });
      status.className = 'form-status ok';
      status.textContent = result.message;
    } catch (error) {
      status.className = 'form-status bad';
      status.textContent = error.message;
    } finally {
      setTimeout(() => { resendBtn.disabled = false; }, 30000);
    }
  });
}

// ---- verified.html -----------------------------------------------------------------
const verifyCard = document.querySelector('#verifyCard');
if (verifyCard) {
  const token = new URLSearchParams(window.location.search).get('token') || '';
  const title = document.querySelector('#verifyTitle');
  const lead = document.querySelector('#verifyLead');
  const resendForm = document.querySelector('#verifyResend');
  const fail = (text) => {
    title.textContent = 'That link didn’t work';
    lead.textContent = `${text} Links work once and last a limited time — enter your email to get a new one.`;
    verifyCard.querySelector('.notice-icon').classList.add('bad');
    resendForm.hidden = false;
  };
  if (!token) {
    fail('This page needs the link from your confirmation email.');
  } else {
    callApi('/auth/verify-email', { token }).then((result) => {
      title.textContent = 'Email confirmed';
      const text = result.message.replace(/^Email confirmed\.\s*/, '');
      lead.textContent = /login details/.test(text) && !/couldn't/.test(text)
        ? `${text} Sign in with them — you'll choose your own password the first time.`
        : text || 'You can sign in now.';
      document.querySelector('#verifyActions').hidden = false;
      resendForm.hidden = false;
      // Tidy the address bar: the token has done its job.
      history.replaceState(null, '', window.location.pathname);
    }).catch((error) => fail(error.message));
  }
  resendForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = resendForm.querySelector('.form-status');
    try {
      const result = await callApi('/public/resend', { email: resendForm.email.value });
      status.className = 'form-status ok';
      status.textContent = result.message;
    } catch (error) {
      status.className = 'form-status bad';
      status.textContent = error.message;
    }
  });
}
