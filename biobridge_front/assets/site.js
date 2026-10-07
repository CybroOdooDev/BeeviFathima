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

  let tz = '';
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch { /* old browser */ }
  form.querySelector('[name="timezone"]').value = tz;

  // Country code defaults to the visitor's country, guessed from the browser
  // (language region first, then timezone). They can change it.
  const codeSelect = form.querySelector('[name="phone_code"]');
  if (codeSelect) {
    const byTz = { 'Asia/Kolkata': 'IN', 'Asia/Calcutta': 'IN', 'Asia/Dubai': 'AE', 'Asia/Riyadh': 'SA', 'Asia/Qatar': 'QA',
      'Asia/Kuwait': 'KW', 'Asia/Muscat': 'OM', 'Asia/Bahrain': 'BH', 'Asia/Karachi': 'PK', 'Asia/Dhaka': 'BD',
      'Asia/Colombo': 'LK', 'Asia/Kathmandu': 'NP', 'Asia/Singapore': 'SG', 'Asia/Kuala_Lumpur': 'MY', 'Asia/Jakarta': 'ID',
      'Asia/Bangkok': 'TH', 'Asia/Manila': 'PH', 'Asia/Shanghai': 'CN', 'Asia/Hong_Kong': 'HK', 'Asia/Tokyo': 'JP',
      'Asia/Seoul': 'KR', 'Europe/London': 'GB', 'Europe/Dublin': 'IE', 'Europe/Berlin': 'DE', 'Europe/Paris': 'FR',
      'Europe/Madrid': 'ES', 'Europe/Rome': 'IT', 'Europe/Amsterdam': 'NL', 'Africa/Cairo': 'EG', 'Africa/Lagos': 'NG',
      'Africa/Nairobi': 'KE', 'Africa/Johannesburg': 'ZA', 'Australia/Sydney': 'AU', 'Pacific/Auckland': 'NZ',
      'America/New_York': 'US', 'America/Chicago': 'US', 'America/Denver': 'US', 'America/Los_Angeles': 'US',
      'America/Toronto': 'CA', 'America/Mexico_City': 'MX', 'America/Sao_Paulo': 'BR' };
    const regionOf = (tag) => { try { return new Intl.Locale(tag).maximize().region || ''; } catch { return ''; } };
    // The timezone says where the visitor physically is; the language only says
    // what they read, so it is the fallback.
    const fromLang = [...(navigator.languages || [navigator.language || ''])]
      .map((t) => (/-[A-Za-z]{2}\b/.test(t) ? regionOf(t) : '')).find(Boolean);
    const guess = byTz[tz] || fromLang || '';
    const hit = [...codeSelect.options].find((o) => o.dataset.iso === guess);
    if (hit) codeSelect.value = hit.value;
    // The closed control shows just "flag +code"; the list keeps full names.
    // The <select> stays as the stored value (and the no-JS fallback); what the
    // visitor sees is a searchable list on top of it: type "91", "ind" or "uk".
    const view = form.querySelector('.phone-code-view');
    const wrap = view && view.parentElement;
    const flagOf = (iso) => String.fromCodePoint(...[...iso].map((c) => 0x1F1E6 + c.charCodeAt(0) - 65));
    const paint = () => {
      const o = codeSelect.selectedOptions[0];
      if (view && o) view.textContent = `${flagOf(o.dataset.iso)} ${o.value}`;
    };
    codeSelect.addEventListener('change', paint);
    paint();

    if (view && wrap) {
      codeSelect.tabIndex = -1;
      codeSelect.setAttribute('aria-hidden', 'true');
      wrap.classList.add('is-searchable');
      view.tabIndex = 0;
      view.setAttribute('role', 'combobox');
      view.setAttribute('aria-haspopup', 'listbox');
      view.setAttribute('aria-expanded', 'false');
      view.setAttribute('aria-label', 'Country code');
      view.removeAttribute('aria-hidden');

      const all = [...codeSelect.options].map((o, i) => ({
        i, iso: o.dataset.iso, code: o.value,
        name: o.textContent.replace(/^\S+\s/, '').replace(/\s*\(\+\d+\)\s*$/, ''),
      }));
      const pop = document.createElement('div');
      pop.className = 'phone-code-pop';
      pop.hidden = true;
      pop.innerHTML = '<input type="text" class="phone-code-search" placeholder="Search country or code" '
        + 'autocomplete="off" aria-label="Search country or code"><ul role="listbox"></ul>';
      wrap.appendChild(pop);
      const search = pop.querySelector('input');
      const list = pop.querySelector('ul');
      let shown = []; let active = 0;

      const render = () => {
        const q = search.value.trim().toLowerCase().replace(/^\+/, '');
        shown = all.filter((c) => !q || c.name.toLowerCase().includes(q)
          || c.code.slice(1).startsWith(q) || c.iso.toLowerCase() === q);
        // Names that start with the query first, then the rest.
        shown.sort((a, b) => (b.name.toLowerCase().startsWith(q) - a.name.toLowerCase().startsWith(q)) || (a.i - b.i));
        active = Math.min(active, Math.max(shown.length - 1, 0));
        list.innerHTML = shown.length ? '' : '<li class="none">No match</li>';
        shown.forEach((c, n) => {
          const li = document.createElement('li');
          li.setAttribute('role', 'option');
          li.dataset.index = String(c.i);
          if (n === active) li.classList.add('active');
          if (c.i === codeSelect.selectedIndex) li.setAttribute('aria-selected', 'true');
          li.textContent = `${flagOf(c.iso)} ${c.name} (${c.code})`;
          list.appendChild(li);
        });
        const cur = list.querySelector('.active');
        if (cur) cur.scrollIntoView({ block: 'nearest' });
      };
      const open = (seed = '') => {
        pop.hidden = false;
        view.setAttribute('aria-expanded', 'true');
        search.value = seed;
        active = 0;
        render();
        search.focus();
      };
      const close = (refocus) => {
        pop.hidden = true;
        view.setAttribute('aria-expanded', 'false');
        if (refocus) view.focus();
      };
      const choose = (index) => {
        codeSelect.selectedIndex = index;
        codeSelect.dispatchEvent(new Event('change', { bubbles: true }));
        close(true);
      };

      view.addEventListener('click', () => (pop.hidden ? open() : close(false)));
      view.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') { e.preventDefault(); open(); }
        else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); open(e.key); }
      });
      search.addEventListener('input', () => { active = 0; render(); });
      search.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, shown.length - 1); render(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
        else if (e.key === 'Enter') { e.preventDefault(); if (shown[active]) choose(shown[active].i); }
        else if (e.key === 'Escape') { e.preventDefault(); close(true); }
        else if (e.key === 'Tab') close(false);
      });
      list.addEventListener('mousedown', (e) => {
        const li = e.target.closest('li[data-index]');
        if (li) { e.preventDefault(); choose(Number(li.dataset.index)); }
      });
      document.addEventListener('mousedown', (e) => { if (!pop.hidden && !wrap.contains(e.target)) close(false); });
    }
  }

  const mailFallback = (data) => {
    const labelOf = (name) => {
      const el = form.querySelector(`[name="${name}"]`);
      const text = el && el.closest('label.field') ? el.closest('label.field').firstChild.textContent.trim() : '';
      return text || name;
    };
    const body = Object.entries(data).map(([k, v]) => `${labelOf(k)}: ${v}`).join('\n');
    window.location.href = `mailto:${SALES_EMAIL}?subject=${encodeURIComponent(
      `BioBridge — ${data.topic || 'enquiry'} from ${data.company || data.name}`
    )}&body=${encodeURIComponent(body)}`;
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const data = {};
    for (const [k, v] of new FormData(form)) { if (String(v).trim() !== '') data[k] = String(v).trim(); }
    // One phone value: "+91 98765 43210"; a lone country code is no number.
    const code = data.phone_code; const num = data.phone_number;
    delete data.phone_code; delete data.phone_number;
    if (num) data.phone = `${code || ''} ${num}`.trim();

    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    say('Sending…', '');
    try {
      if (FORM_ENDPOINT) {
        const response = await fetch(FORM_ENDPOINT, {
          method: 'POST',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        });
        if (!response.ok) throw new Error(String(response.status));
        form.reset();
        say('Thanks — we have your request and will be in touch.', 'ok');
      } else {
        const result = await callApi('/public/contact', data);
        form.reset();
        status.className = 'form-status ok';
        status.textContent = (result && result.message) || 'Thanks — we have your request and will be in touch.';
        if (result && result.booking_url) {
          status.append(' ');
          const link = document.createElement('a');
          link.href = result.booking_url; link.textContent = 'Pick a time now';
          link.target = '_blank'; link.rel = 'noopener';
          status.append(link);
        }
      }
    } catch (error) {
      if (!FORM_ENDPOINT && error.status === 0) {
        say('We couldn\u2019t reach our server, opening your email app instead\u2026', 'bad');
        mailFallback(data);
      } else if (!FORM_ENDPOINT && error.message) {
        say(error.message, 'bad');
      } else {
        say(`That didn't go through. Please email us at ${SALES_EMAIL}.`, 'bad');
      }
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

// Yearly billing: what paying once a year saves against twelve monthly payments.
const savingPct = (p) => (p && p.yearly_price_cents != null && p.monthly_price_cents
  ? Math.round((1 - p.yearly_price_cents / (12 * p.monthly_price_cents)) * 100) : 0);
const saveLabel = (list) => {
  const best = Math.max(0, ...list.map(savingPct));
  return best > 0 ? `Save ${best}%` : '';
};
const startInterval = new URLSearchParams(window.location.search).get('billing') === 'year' ? 'year' : 'month';

// Plans come from the app when it's reachable; the prices written into the
// HTML are the fallback, so a page never shows nothing.
let plansPromise = null;
function livePlans() {
  if (!plansPromise) plansPromise = callApi('/public/plans').catch(() => null);
  return plansPromise;
}

// Pricing and home: fill prices and limits in, and hide "buy now" for any
// plan that can't be bought online yet. The Monthly / Yearly switch repaints
// the prices and the sign-up links; the prices written into the page are the
// fallback when the app can't be reached.
if (document.querySelector('[data-plan], .mini-plan')) {
  let interval = startInterval;
  let byName = null;   // live plans, once the app has answered

  const planFor = (card) => {
    const live = byName && byName[card.dataset.plan];
    if (live) return live;
    // Fallback: what the HTML itself says.
    return {
      monthly_price_cents: Number(card.dataset.monthCents) || null,
      yearly_price_cents: Number(card.dataset.yearCents) || null,
      can_buy_online: true, can_buy_yearly: true, fallback: true,
    };
  };

  const paintPlans = () => {
    const all = [...document.querySelectorAll('[data-plan]')].map(planFor);
    document.querySelectorAll('[data-save]').forEach((el) => {
      el.textContent = byName ? saveLabel(all) : '2 months free';
    });
    document.querySelectorAll('[data-interval]').forEach((btn) => {
      const on = btn.dataset.interval === interval;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    });
    document.querySelectorAll('[data-plan]').forEach((card) => {
      const p = planFor(card);
      // A plan with no yearly price stays monthly even on the yearly view.
      const yearly = interval === 'year' && p.yearly_price_cents != null;
      const price = card.querySelector('.price');
      const note = card.querySelector('[data-note]');
      if (price && (!p.fallback || p.monthly_price_cents)) {
        price.innerHTML = yearly
          ? `${money(p.yearly_price_cents)}<small> / year</small>`
          : `${money(p.monthly_price_cents)}<small> / month</small>`;
      }
      if (note) {
        const pct = savingPct(p);
        note.hidden = interval !== 'year';
        note.textContent = !yearly ? 'Monthly billing only'
          : `$${(p.yearly_price_cents / 1200).toFixed(2)} / month, billed yearly${pct > 0 ? ` · save ${pct}%` : ''}`;
      }
      const billing = yearly ? 'year' : 'month';
      card.querySelectorAll('a[href^="signup.html"]').forEach((a) => {
        const url = new URL(a.getAttribute('href'), window.location.href);
        url.searchParams.set('billing', billing);
        a.setAttribute('href', `signup.html${url.search}`);
      });
      const buy = card.querySelector('[data-buy]');
      if (buy) buy.hidden = !(yearly ? p.can_buy_yearly : p.can_buy_online);
      if (byName && byName[card.dataset.plan]) {
        const items = card.querySelectorAll('.ticks li span');
        const [emp, dev, sync] = limitsOf(p);
        if (items[0]) items[0].textContent = emp;
        if (items[1]) items[1].textContent = sync;
        if (items[2]) items[2].textContent = dev;
      }
    });
  };

  document.querySelectorAll('[data-interval]').forEach((btn) => btn.addEventListener('click', () => {
    interval = btn.dataset.interval;
    paintPlans();
  }));
  paintPlans();

  livePlans().then((plans) => {
    if (!plans) return;
    byName = Object.fromEntries(plans.map((p) => [p.name, p]));
    paintPlans();
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
    i === 0 && here !== 'UTC' ? `${z} (default)` : z}</option>`).join('');

  const mode = () => signupForm.querySelector('[name=mode]:checked').value;
  const billingSelect = signupForm.querySelector('#billingSelect');
  const billing = () => billingSelect.value;
  const yearlyOption = billingSelect.querySelector('[value=year]');
  const billingNote = signupForm.querySelector('#billingNote');
  const current = () => plans && plans.find((p) => p.name === planSelect.value);
  // What the visitor asked for, kept apart from what the chosen plan allows:
  // flicking through a monthly-only plan must not lose their yearly choice.
  let wantYear = params.get('billing') === 'year';
  billingSelect.addEventListener('change', () => { wantYear = billing() === 'year'; });

  const paint = () => {
    const p = current();
    const buying = mode() === 'buy';
    // Yearly needs a yearly price on the plan — and, to pay for it now, a
    // yearly Stripe price too. Otherwise the choice is withheld, not failed.
    const yearlyOk = !p || (p.yearly_price_cents != null && (!buying || p.can_buy_yearly));
    yearlyOption.disabled = !yearlyOk;
    billingSelect.value = wantYear && yearlyOk ? 'year' : 'month';
    const yearly = billing() === 'year';
    yearlyOption.textContent = p && p.yearly_price_cents != null && savingPct(p) > 0
      ? `Pay yearly — save ${savingPct(p)}%` : 'Pay yearly';
    billingNote.textContent = '';
    go.textContent = buying ? 'Continue to payment' : 'Start 10-day free trial';
    note.textContent = '';
    if (p && !yearlyOk && p.yearly_price_cents == null) {
      billingNote.textContent = `${p.name} is monthly only.`;
    } else if (p && !yearlyOk) {
      billingNote.textContent = `${p.name} can't be paid for yearly online yet — pay monthly, or start a free trial.`;
    }
    const canBuy = yearly ? p?.can_buy_yearly : p?.can_buy_online;
    if (buying && p && !canBuy) {
      note.textContent = `${p.name} can't be bought online yet — start a free trial, or contact us.`;
    }
    go.disabled = Boolean(buying && p && !canBuy);
    if (p && summary) {
      summary.hidden = false;
      const price = yearly && yearlyOk
        ? `${money(p.yearly_price_cents)}<small> / year${buying ? '' : ' after the trial'}</small>`
        : `${money(p.monthly_price_cents)}<small> / month${buying ? '' : ' after the trial'}</small>`;
      const sub = yearly && yearlyOk
        ? `<span style="font-size:14px;color:var(--muted)">$${(p.yearly_price_cents / 1200).toFixed(2)} / month, billed yearly</span>` : '';
      summary.innerHTML = `<span class="eyebrow">${buying ? 'You are buying' : 'Your trial plan'}</span>
        <span class="name">${p.name}</span>
        <span class="price">${price}</span>${sub}
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
