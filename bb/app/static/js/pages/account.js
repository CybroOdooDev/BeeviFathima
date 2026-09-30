/* The person's own sign-in: confirming their email, the forced first
 * password change after signing in with an emailed password, and changing
 * the password any time from Settings → Password. */

import { api, auth } from '../api.js';
import { $, busy, esc, field, guard, readForm, toast } from '../ui.js';

function authShell(inner) {
  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="brand">
          <span class="brand-mark">B</span>
          <span class="brand-word"><b>Bio</b><span>Bridge</span></span>
        </div>
        ${inner}
        <p class="err" id="authError"></p>
      </div>
    </div>`;
  return root;
}

async function post(path, body, token) {
  const response = await fetch(`/api/v1${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.detail && typeof payload.detail === 'string' ? payload.detail : 'Something went wrong');
  return payload;
}

/** #/verify-email?token=… — where the confirmation link lands when the
 * deployment has no marketing site (SITE_URL unset). */
export async function renderVerifyEmail(route = {}) {
  const root = authShell('<h1>Confirming your email…</h1><p class="sub">One moment.</p>');
  try {
    const result = await post('/auth/verify-email', { token: route.query?.token || '' });
    root.querySelector('.auth-card').innerHTML = `
      <div class="brand"><span class="brand-mark">B</span><span class="brand-word"><b>Bio</b><span>Bridge</span></span></div>
      <h1>Email confirmed</h1>
      <p class="sub">${esc(result.message)}</p>
      <a class="btn primary" style="width:100%;text-align:center" href="#/login">Go to sign in</a>`;
  } catch (error) {
    root.querySelector('.auth-card').innerHTML = `
      <div class="brand"><span class="brand-mark">B</span><span class="brand-word"><b>Bio</b><span>Bridge</span></span></div>
      <h1>That link didn’t work</h1>
      <p class="sub">${esc(error.message)} Links last a limited time and work once — ask for a new one from the page you registered on.</p>
      <a class="btn" style="width:100%;text-align:center" href="#/login">Go to sign in</a>`;
  }
}

function passwordFields({ current = 'Current password' } = {}) {
  return `
    ${field({ name: 'current_password', label: current, type: 'password', required: true })}
    ${field({ name: 'new_password', label: 'New password', type: 'password', required: true,
              help: 'At least 10 characters.' })}
    ${field({ name: 'confirm', label: 'Repeat new password', type: 'password', required: true })}`;
}

function check(values) {
  if (values.new_password !== values.confirm) throw new Error('The two new passwords don’t match.');
  if (values.new_password.length < 10) throw new Error('Use at least 10 characters.');
  return { current_password: values.current_password, new_password: values.new_password };
}

/** Shown instead of the app after signing in with the emailed password. */
export function renderSetPassword() {
  const root = authShell(`
    <h1>Choose your password</h1>
    <p class="sub">You signed in with the password we emailed you. Set your own to continue — the emailed one stops working.</p>
    <form id="pwForm">
      ${passwordFields({ current: 'Password from the email' })}
      <button class="primary" style="width:100%" id="pwGo">Set password and continue</button>
    </form>
    <p class="auth-alt"><a href="#" id="pwOut">Sign out</a></p>`);

  $('#pwOut', root).addEventListener('click', (event) => {
    event.preventDefault();
    auth.clear();
    window.location.hash = '#/login';
    window.location.reload();
  });

  $('#pwForm', root).addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = $('#authError', root);
    const button = $('#pwGo', root);
    error.textContent = '';
    button.disabled = true;
    try {
      await post('/auth/change-password', check(readForm(event.target)), auth.accessToken);
      auth.user = null; // reload /auth/me, now without the flag
      toast('Password set', 'ok');
      window.dispatchEvent(new CustomEvent('bb:signed-in'));
    } catch (exc) {
      error.textContent = exc.message;
    } finally {
      button.disabled = false;
    }
  });
}

/** Settings → Password. */
export async function renderPasswordSettings(mount) {
  mount.innerHTML = `
    <div class="card" style="max-width:560px">
      <div class="card-head" style="position:static"><h2>Password <span class="hint">${esc(auth.user?.email || '')}</span></h2></div>
      <form id="pwForm">
        ${passwordFields()}
        <div class="row" style="margin-top:6px">
          <button class="primary" id="pwSave">Change password</button>
          <span class="hint">Signs you out everywhere else.</span>
        </div>
      </form>
    </div>`;
  $('#pwForm', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    let body;
    try {
      body = check(readForm(event.target));
    } catch (exc) {
      toast(exc.message, 'bad');
      return;
    }
    busy($('#pwSave', mount), async () => {
      const ok = await guard(() => api.post('/auth/change-password', body));
      if (ok) {
        event.target.reset();
        toast('Password changed', 'ok');
      }
    });
  });
}

