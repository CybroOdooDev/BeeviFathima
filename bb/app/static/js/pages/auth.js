/* Sign in and sign up. Rendered into #auth-root, outside the app shell. */

import { api, auth } from '../api.js';
import { $, $$, esc, field, planCards, readForm } from '../ui.js';

function shell(inner, { staff = false, wide = false } = {}) {
  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card${staff ? ' staff' : ''}${wide ? ' wide' : ''}">
        <div class="brand">
          <span class="brand-dot"></span>BioBridge
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

export function renderLogin() {
  const root = shell(`
    <h1>Sign in</h1>
    <p class="sub">Biometric attendance, synced into Odoo.</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">Sign in</button>
    </form>
    <p class="auth-alt">No account yet? <a href="#/signup">Create one</a></p>`);

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
   * this screen is not what keeps anyone out. What it buys is that a staff
   * credential is never typed into the customer-facing form: the two surfaces
   * no longer share one page to phish, and the session this door mints is a
   * short one. There is no sign-up here on purpose — the staff flag is only
   * ever set by tools/grant_admin.py on the server. */
  const root = shell(`
    <h1>Platform console</h1>
    <p class="sub">For BioBridge staff. Customer accounts sign in at the main
      login. Console sessions are short and expire on their own.</p>
    <form id="form">
      ${field({ name: 'email', label: 'Staff email', type: 'email', required: true })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">Sign in to console</button>
    </form>
    <p class="auth-alt">Not staff? <a href="#/login">Customer sign in</a></p>`,
    { staff: true });

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    submit($('#go', root), async () => {
      afterAuth(await api.post('/auth/staff/login', values));
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
