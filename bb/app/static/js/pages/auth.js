/* Sign in and sign up. Rendered into #auth-root, outside the app shell. */

import { api, auth } from '../api.js';
import { $, $$, esc, field, planCards, readForm, timezoneNames } from '../ui.js';

function shell(inner, { staff = false, wide = false } = {}) {
  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card${staff ? ' staff' : ''}${wide ? ' wide' : ''}">
        <div class="brand">
          <span class="brand-mark">B</span>
          <span class="brand-word"><b>Bio</b><span>Bridge</span></span>
          ${staff ? '<span class="pill warn">staff console</span>' : ''}
        </div>
        ${inner}
        <p class="err" id="authError"></p>
      </div>
    </div>`;
  return root;
}

async function submit(button, fn) {
  const error = $('#authError');
  error.textContent = '';
  button.disabled = true;
  try {
    await fn();
  } catch (exc) {
    error.textContent = exc.message || 'Something went wrong';
  } finally {
    button.disabled = false;
  }
}

function afterAuth(tokens) {
  auth.persist(tokens);
  window.dispatchEvent(new CustomEvent('bb:signed-in'));
}

/** Staff are also often customers with a workspace of their own. Signing in
 * at the console door opens that workspace session too — with the password
 * already typed, so there is no second sign-in to switch to it later.
 *
 * Only in this direction. A console session is the one worth stealing, so it
 * is never opened as a side effect of an ordinary customer sign-in; that one
 * asks for the password when the person actually goes to the console. */
async function alsoOpenWorkspace(values) {
  if (auth.stashed('tenant')) return;
  try {
    const me = await api.get('/auth/me');
    if (!me.tenant_id) return;
    // A bare call rather than api.post: it must not touch the console
    // session that is active right now, only sit beside it.
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    if (response.ok) auth.stash(await response.json());
  } catch { /* best effort — the console sign-in itself already worked */ }
}

export function renderLogin() {
  // A staff member in the console whose workspace session has expired comes
  // back through here; their address is known, so only the password is asked.
  const returning = auth.isAuthenticated && auth.isStaffSession && auth.user?.tenant_id
    ? auth.user.email : '';
  const root = shell(`
    <h1>${returning ? 'Back to your workspace' : 'Sign in'}</h1>
    <p class="sub">${returning
      ? 'Your workspace session has ended. Confirm your password to reopen it — the console stays open alongside.'
      : 'Biometric attendance, synced into Odoo.'}</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true, value: returning })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${returning ? 'Open my workspace' : 'Sign in'}</button>
    </form>
    <p class="auth-alt">${returning
      ? '<a href="#/platform">Cancel — stay in the console</a>'
      : 'No account yet? <a href="#/signup">Create one</a>'}</p>`);

  if (returning) {
    $('#form input[name=email]', root).readOnly = true;
    $('#form input[name=password]', root).focus();
  }

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    submit($('#go', root), async () => {
      afterAuth(await api.post('/auth/login', values));
    });
  });
}

export function renderStaffLogin() {
  /* A second door, not a second check.
   *
   * The console refuses a customer-scoped token whatever page produced it, so
   * this screen is not what keeps anyone out. What it buys is that the
   * session this door mints is its own — short-lived, and never the same
   * token as the customer one. There is no sign-up here on purpose — the
   * staff flag is only ever set by tools/grant_admin.py on the server.
   *
   * Reached two ways: cold, by staff with no session; or from the "Staff
   * console" switch in a workspace, when there is no console session open
   * yet (or it has expired). The second is a step-up: the address is known,
   * so only the password is asked, and the workspace stays open beside it. */
  const stepUp = auth.isAuthenticated && !auth.isStaffSession
    && auth.user?.is_platform_admin === true;
  const root = shell(`
    <h1>${stepUp ? 'Open the staff console' : 'Platform console'}</h1>
    <p class="sub">${stepUp
      ? 'Confirm your password to open the console. Your workspace stays open — switch between the two from the sidebar.'
      : 'For BioBridge staff. Customer accounts sign in at the main login. Console sessions are short and expire on their own.'}</p>
    <form id="form">
      ${field({ name: 'email', label: 'Staff email', type: 'email', required: true, value: stepUp ? auth.user.email : '' })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${stepUp ? 'Open console' : 'Sign in to console'}</button>
    </form>
    <p class="auth-alt">${stepUp
      ? '<a href="#/">Cancel — back to my workspace</a>'
      : 'Not staff? <a href="#/login">Customer sign in</a>'}</p>`,
    { staff: true });

  if (stepUp) {
    $('#form input[name=email]', root).readOnly = true;
    $('#form input[name=password]', root).focus();
  }

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    submit($('#go', root), async () => {
      auth.persist(await api.post('/auth/staff/login', values));
      await alsoOpenWorkspace(values);
      window.dispatchEvent(new CustomEvent('bb:signed-in'));
    });
  });
}

export async function renderSignup() {
  // Pre-fill the browser's zone: it is right often enough to save a step, and
  // wrong in a way the user can see and correct.
  const guess = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  // Public on purpose — GET /auth/plans needs no token — and best-effort: a
  // failed fetch just means no picker, not a broken signup screen. With no
  // active plans there is nothing to actually choose, so both the mode and
  // plan fields are left out entirely rather than offered with nothing in
  // them — every signup is then just a trial, same as before either existed.
  const plans = await api.get('/auth/plans').catch(() => []);
  const defaultPlanId = (plans.find((p) => p.is_default) || plans[0] || {}).id || '';

  const OPTIONAL_HELP = 'Optional during a trial — leave it on the '
    + 'recommended plan, or pick one to trial its specific limits.';
  const REQUIRED_HELP = 'Required to skip the trial — this is the plan '
    + 'you start on immediately, with no trial period.';

  // Two different questions, not one dropdown with a default: whether to
  // trial at all, and — only if not — which plan to skip straight to.
  // Picking a plan *during* a trial (the field below) never skips it; only
  // this selector does.
  const modeField = plans.length ? field({
    name: 'start_mode', label: 'Getting started', value: 'trial',
    options: [
      { value: 'trial', label: 'Start a free trial' },
      { value: 'plan', label: 'Choose a plan now — no trial' },
    ],
  }) : '';

  // A card per plan, not a dropdown of names — the whole point of surfacing
  // this here is so a visitor can actually compare tiers (price, what each
  // one includes) before picking one, not just recognise a name they were
  // already told elsewhere.
  const planField = plans.length ? planCards({
    id: 'planField', name: 'plan_id', label: 'Plan', value: defaultPlanId,
    plans, help: OPTIONAL_HELP,
  }) : '';

  const root = shell(`
    <h1>Create your account</h1>
    <p class="sub">The first user becomes the owner of the workspace.</p>
    <form id="form">
      ${field({ name: 'company_name', label: 'Company', required: true })}
      ${field({ name: 'full_name', label: 'Your name' })}
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      ${field({
        name: 'password', label: 'Password', type: 'password', required: true,
        help: 'At least 10 characters.',
      })}
      ${field({
        name: 'timezone', label: 'Your timezone', value: guess, required: true,
        help: 'Used to display attendance. The BioTime server has its own setting.',
        datalist: timezoneNames(),
      })}
      ${modeField}
      ${planField}
      <button class="primary" style="width:100%" id="go">Create account</button>
    </form>
    <p class="auth-alt">Already have one? <a href="#/login">Sign in</a></p>`,
    { wide: plans.length > 0 });

  const modeSelect = $('#form select[name=start_mode]', root);
  const planWrap = $('#planField', root);
  const planRadios = $$('#planField input[name=plan_id]', root);
  if (modeSelect && planWrap && planRadios.length) {
    const optBadge = planWrap.querySelector('.opt');
    const help = planWrap.querySelector('.help');
    const sync = () => {
      const skipping = modeSelect.value === 'plan';
      planRadios.forEach((r) => { r.required = skipping; });
      if (optBadge) optBadge.style.display = skipping ? 'none' : '';
      if (help) help.textContent = skipping ? REQUIRED_HELP : OPTIONAL_HELP;
    };
    modeSelect.addEventListener('change', sync);
    sync();

    // Cards carry the selection visually — readForm() only cares which
    // radio is checked, but a click needs to repaint which card looks
    // picked.
    planWrap.addEventListener('change', (event) => {
      if (event.target.name !== 'plan_id') return;
      planRadios.forEach((r) => r.closest('.plan-card').classList.toggle('selected', r.checked));
    });
  }

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    if (!values.full_name) delete values.full_name;
    if (!values.plan_id) delete values.plan_id;
    if (values.start_mode === 'plan') values.skip_trial = true;
    delete values.start_mode;
    submit($('#go', root), async () => {
      afterAuth(await api.post('/auth/signup', values));
    });
  });
}
