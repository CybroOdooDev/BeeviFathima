/* Sign in and sign up. Rendered into #auth-root, outside the app shell. */

import { api, ApiError, auth, switchSession } from '../api.js';
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
    error.textContent = exc.message || 'Something went wrong';
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
      (payload && typeof payload.detail === 'string') ? payload.detail : 'Sign-in failed',
      response.status, payload,
    );
  }
  return payload;
}

/** One sign-in for every hat the account has.
 *
 * The server still has two doors and still mints two separately-scoped
 * sessions — a workspace one and, for platform staff, a console one — and it
 * still refuses each at the other's routes. What changes is that nobody has
 * to find the second door: the password typed here opens both, and the
 * sidebar switches between them.
 *
 * Returns 'tenant' or 'staff': which session is active now. */
async function signIn(values) {
  let workspace;
  try {
    workspace = await credentials('/auth/login', values);
  } catch (error) {
    // 403 at the customer door is "platform staff with no workspace of their
    // own" (or a disabled account, which the console door refuses too).
    if (error.status !== 403) throw error;
    const console_ = await credentials('/auth/staff/login', values).catch(() => { throw error; });
    auth.persist(console_);
    return 'staff';
  }
  auth.persist(workspace);

  // Only for accounts that hold the flag — asking the console door on behalf
  // of every customer would be pointless and would fill the server log with
  // refused console sign-ins.
  const me = await api.get('/auth/me');
  if (me.is_platform_admin) {
    try {
      auth.stash(await credentials('/auth/staff/login', values));
    } catch { /* the workspace session still works on its own */ }
  }
  return 'tenant';
}

/** "Log in as admin": the console door first, so the console is where this
 * lands. A non-staff account gets the same answer as a wrong password — the
 * server will not say who holds the flag. The person's own workspace, if they
 * have one, is opened alongside as usual. */
async function signInAsAdmin(values) {
  auth.persist(await credentials('/auth/staff/login', values));
  try {
    const me = await api.get('/auth/me');
    if (me.tenant_id) auth.stash(await credentials('/auth/login', values));
  } catch { /* the console session still works on its own */ }
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
    : '<a href="#/staff/login">Log in as admin</a>';
  const root = shell(`
    <h1>${reauth ? 'Confirm it’s you' : admin ? 'Admin sign in' : 'Sign in'}</h1>
    <p class="sub">${reauth
      ? `Your ${target === 'staff' ? 'staff console' : 'workspace'} session has ended. Enter your password to reopen it.`
      : admin
        ? 'For BioBridge platform staff. Opens the staff console — your own workspace too, if you have one.'
        : 'Biometric attendance, synced into Odoo.'}</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true, value: reauth })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${reauth ? 'Continue' : admin ? 'Sign in to console' : 'Sign in'}</button>
    </form>
    ${reauth ? '<p class="auth-alt"><a href="#/">Cancel</a></p>'
      : admin ? ''
      : '<p class="auth-alt">No account yet? <a href="#/signup">Create one</a></p>'}`,
    { staff: admin, foot });

  if (reauth) {
    $('#form input[name=email]', root).readOnly = true;
    $('#form input[name=password]', root).focus();
  }

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    submit($('#go', root), async () => {
      const active = admin ? await signInAsAdmin(values) : await signIn(values);
      // Land where this person works: the console when they asked for it,
      // the hat they last used, or the one they were switching to when they
      // were asked to sign in again.
      const wanted = admin ? 'staff' : target || rememberedHat();
      if (active === 'tenant' && wanted === 'staff' && auth.stashed('staff')) {
        await switchSession('staff');
      }
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
    name: 'start_mode', label: 'Getting started', value: 'trial',
    options: [
      { value: 'trial', label: 'Start a free trial' },
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
            : 'Custom pricing'}</span>
        </div>
        <a href="#/plans">Explore plans</a>
      </div>
      <input type="hidden" name="plan_id" value="${esc(chosenId)}">
    </div>` : '';

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
    <p class="auth-alt">Already have one? <a href="#/login">Sign in</a></p>`);

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
          <a class="link" href="#/signup">&larr; Back to sign up</a>
        </div>
        <div class="pricing-header">
          <h1>Plans built around your headcount and sync speed</h1>
          <p class="sub">Every plan syncs the same attendance features into Odoo — the
            difference is how many employees and how often. Every plan starts with a
            free trial, whichever one you pick below.</p>
        </div>
        ${active.length ? pricingCards({ plans: active, ctaLabel: 'Get started' })
          : empty('No plans available', 'Check back shortly, or contact us directly.')}
      </div>
    </div>`;

  wirePricingCards(root, (planId) => {
    window.location.hash = `#/signup?plan=${encodeURIComponent(planId)}`;
  });
}
