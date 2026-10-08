/* Sign in and sign up. Rendered into #auth-root, outside the app shell. */

import { api, ApiError, auth } from '../api.js';
import { $, empty, esc, field, pricingCards, readForm, timezoneNames, wirePricingCards } from '../ui.js';

/** The sign-in card, plus an optional line under it — outside the card, the
 * usual place for a secondary door like "Log in as admin". */
function shell(inner, { staff = false, wide = false, foot = '' } = {}) {
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
      ${foot ? `<p class="auth-foot">${foot}</p>` : ''}
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
    error.textContent = exc.message || 'Something Went Wrong';
  } finally {
    button.disabled = false;
  }
}

function afterAuth(tokens) {
  auth.persist(tokens);
  window.dispatchEvent(new CustomEvent('bb:signed-in'));
}

/** A credential call that never touches the session in hand.
 *
 * Deliberately not api.post: that attaches whatever session is active, and
 * reads a 401 on an authenticated request as "your session expired" — which
 * a wrong password during a re-sign-in is not. */
async function credentials(path, values) {
  const response = await fetch(`/api/v1${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(values),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      (payload && typeof payload.detail === 'string') ? payload.detail : 'Sign-In Failed',
      response.status, payload,
    );
  }
  return payload;
}

/** The customer door. A platform staff account is refused here — staff is a
 * backend role and signs in at the console door only ("Log in as admin").
 * Returns 'tenant'. */
async function signIn(values) {
  auth.persist(await credentials('/auth/login', values));
  return 'tenant';
}

/** "Log in as admin": the console door only. A non-staff account gets the same
 * answer as a wrong password — the server will not say who holds the flag.
 * Returns 'staff'. */
async function signInAsAdmin(values) {
  auth.persist(await credentials('/auth/staff/login', values));
  return 'staff';
}

export function renderLogin(route = {}) {
  // Asked again for the password while already signed in: a switch to a
  // session that had expired (the console's lives a day at most). The
  // address is known, so only the password is asked.
  const reauth = auth.isAuthenticated && auth.user?.email ? auth.user.email : '';
  const target = route.query?.reauth || '';
  // "Log in as admin": the same form, pointed at the console first. Reached
  // from the link under the card, or bookmarked as #/staff/login.
  const admin = route.path === '/staff/login' && !reauth;
  const foot = reauth ? '' : admin
    ? '<a href="#/login">&larr; Back to customer sign-in</a>'
    : '<a href="#/staff/login">Log In As Admin</a>';
  const root = shell(`
    <h1>${reauth ? 'Confirm It’s You' : admin ? 'Admin Sign In' : 'Sign In'}</h1>
    <p class="sub">${reauth
      ? `Your ${target === 'staff' ? 'staff console' : 'workspace'} session has ended. Enter your password to reopen it.`
      : admin
        ? 'For BioBridge platform staff only.'
        : 'Biometric attendance, synced into Odoo.'}</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true, value: reauth })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      ${reauth ? '' : '<p class="auth-alt" style="text-align:right;margin:-4px 0 12px"><a href="#/forgot-password">Forgot Password?</a></p>'}
      <button class="primary" style="width:100%" id="go">${reauth ? 'Continue' : admin ? 'Sign In To Console' : 'Sign In'}</button>
    </form>
    ${reauth ? '<p class="auth-alt"><a href="#/">Cancel</a></p>'
      : admin ? ''
      : '<p class="auth-alt">No account yet? <a href="#/signup">Create One</a></p>'}`,
    { staff: admin, foot });

  if (reauth) {
    $('#form input[name=email]', root).readOnly = true;
    $('#form input[name=password]', root).focus();
  }

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    submit($('#go', root), async () => {
      if (admin) await signInAsAdmin(values); else await signIn(values);
      window.dispatchEvent(new CustomEvent('bb:signed-in'));
    });
  });
}

/** The hat last used on this browser — a convenience, not a permission. */
export function rememberedHat() {
  try {
    return localStorage.getItem('bb.hat') || 'tenant';
  } catch {
    return 'tenant';
  }
}

export async function renderSignup(route = {}) {
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
  // #/plans hands the pick back as ?plan=<id> — see renderPlans below. Falls
  // back to the default the way it always has when nobody has chosen yet, or
  // when the id no longer matches an active plan.
  const chosenId = (route.query?.plan && plans.some((p) => p.id === route.query.plan))
    ? route.query.plan : defaultPlanId;
  const chosenPlan = plans.find((p) => p.id === chosenId);

  // Two different questions, not one dropdown with a default: whether to
  // trial at all, and — only if not — which plan to skip straight to.
  // Picking a plan *during* a trial (the summary below) never skips it; only
  // this selector does.
  const modeField = plans.length ? field({
    name: 'start_mode', label: 'Getting Started', value: 'trial',
    options: [
      { value: 'trial', label: 'Start A Free Trial' },
      { value: 'plan', label: 'Choose a plan now — no trial' },
    ],
    help: 'Either way, the plan below is what you start on — a trial just '
      + 'delays when it starts being billed.',
  }) : '';

  // One line naming the plan and its price, plus a way to the full
  // comparison — the richer pricingCards() grid this used to inline lives at
  // #/plans now, so this screen stays a short form. The hidden input is what
  // readForm() actually reads; it always carries a valid id, so there is
  // nothing here to validate.
  const planField = plans.length ? `
    <div class="field" id="planField">
      <label>Plan</label>
      <div class="plan-picked">
        <div>
          <strong>${esc(chosenPlan?.name || 'Plan')}</strong>
          <span class="hint">${chosenPlan?.monthly_price_cents != null
            ? `$${(chosenPlan.monthly_price_cents / 100).toFixed(0)}/mo`
            : 'Custom Pricing'}</span>
        </div>
        <a href="#/plans">Explore Plans</a>
      </div>
      <input type="hidden" name="plan_id" value="${esc(chosenId)}">
    </div>` : '';

  const root = shell(`
    <h1>Create Your Account</h1>
    <p class="sub">The first user becomes the owner of the workspace.</p>
    <form id="form">
      ${field({ name: 'company_name', label: 'Company', required: true })}
      ${field({ name: 'full_name', label: 'Your Name' })}
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      ${field({
        name: 'password', label: 'Password', type: 'password', required: true,
        help: 'At least 10 characters.',
      })}
      ${field({
        name: 'timezone', label: 'Your Timezone', value: guess, required: true,
        help: 'Used to display attendance. The BioTime server has its own setting.',
        datalist: timezoneNames(),
      })}
      ${modeField}
      ${planField}
      <button class="primary" style="width:100%" id="go">Create Account</button>
    </form>
    <p class="auth-alt">Already have one? <a href="#/login">Sign In</a></p>`);

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

/** The full pricing comparison — reached from "Explore plans" on signup, and
 * from "Choose a plan"/"Change plan" in Settings for a signed-out visitor
 * following a bookmarked or shared link. Public: GET /auth/plans needs no
 * token, and there is nothing here that acts on an account — a click just
 * carries the pick back to #/signup, where creating the account is what
 * actually chooses it. */
export async function renderPlans() {
  const plans = await api.get('/auth/plans').catch(() => []);
  const active = plans.filter((p) => p.is_active);

  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="pricing-page">
        <div class="pricing-page-head">
          <div class="brand">
            <span class="brand-mark">B</span>
            <span class="brand-word"><b>Bio</b><span>Bridge</span></span>
          </div>
          <a class="link" href="#/signup">&larr; Back To Sign Up</a>
        </div>
        <div class="pricing-header">
          <h1>Plans Built Around Your Headcount And Sync Speed</h1>
          <p class="sub">Every plan syncs the same attendance features into Odoo — the
            difference is how many employees and how often. Every plan starts with a
            free trial, whichever one you pick below.</p>
        </div>
        ${active.length ? pricingCards({ plans: active, ctaLabel: 'Get Started' })
          : empty('No Plans Available', 'Check back shortly, or contact us directly.')}
      </div>
    </div>`;

  wirePricingCards(root, (planId) => {
    window.location.hash = `#/signup?plan=${encodeURIComponent(planId)}`;
  });
}
