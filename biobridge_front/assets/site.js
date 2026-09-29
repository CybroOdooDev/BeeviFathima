/* BioBridge marketing site — the only script. Three small jobs:
 *   1. point every sign-in / sign-up link at the app (one setting, below),
 *   2. the mobile menu,
 *   3. the contact form: posts to FORM_ENDPOINT, or opens an email if unset.
 */

// ---- settings: change these two lines when you deploy -------------------
const APP_URL = 'http://127.0.0.1:8000';          // where BioBridge itself runs
const FORM_ENDPOINT = '';                            // e.g. a Formspree / Basin URL; blank = email fallback
const SALES_EMAIL = 'sales@example.com';             // used by the email fallback
// --------------------------------------------------------------------------

const APP_ROUTES = {
  signup: '/app/#/signup',
  signin: '/app/#/login',
  plans: '/app/#/plans',
};

document.querySelectorAll('[data-app]').forEach((link) => {
  const route = APP_ROUTES[link.dataset.app];
  if (route) link.href = APP_URL.replace(/\/$/, '') + route;
});

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
