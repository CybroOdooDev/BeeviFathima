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
  if (!response.ok) {
    const detail = payload?.detail;
    // FastAPI's validation errors (422) are a list — show their messages
    // rather than a bare "Something went wrong".
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) && detail.length
        ? detail.map((d) => String(d.msg || '').replace(/^Value error, /, '')).filter(Boolean).join(' ')
        : `Request failed (HTTP ${response.status})`;
    throw new Error(message || 'Something went wrong');
  }
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
    ${current ? field({ name: 'current_password', label: current, type: 'password', required: true }) : ''}
    ${field({ name: 'new_password', label: 'New Password', type: 'password', required: true,
              help: 'At least 10 characters.' })}
    ${field({ name: 'confirm', label: 'Confirm Password', type: 'password', required: true })}`;
}

function check(values) {
  if (values.new_password !== values.confirm) throw new Error('The passwords don’t match.');
  if (values.new_password.length < 10) throw new Error('Use at least 10 characters.');
  const body = { new_password: values.new_password };
  if (values.current_password) body.current_password = values.current_password;
  return body;
}

/** Shown instead of the app after signing in with the emailed password. */
export function renderSetPassword() {
  const root = authShell(`
    <h1>Choose your password</h1>
    <p class="sub">Welcome! Choose your own password to continue — the one we emailed you stops working once you do.</p>
    <form id="pwForm">
      ${passwordFields({ current: null })}
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
      toast('Password set — let\u2019s get you connected.', 'ok');
      // A brand-new account goes straight into the guided setup.
      window.dispatchEvent(new CustomEvent('bb:signed-in', { detail: { next: '#/get-started' } }));
    } catch (exc) {
      error.textContent = exc.message;
    } finally {
      button.disabled = false;
    }
  });
}

/** Settings → General → "Change user password": a two-step modal wizard.
 *
 *   1 Current password   who you are
 *   2 New password       typed twice; Save sends both steps together
 *
 * There is no "check the current password" call on its own, so the server
 * checks it on Save — when it says the current one is wrong, the wizard
 * goes back to step 1 with that message. Lives on <body>, like the
 * add-connection wizard, so a page re-render can't close it mid-way. */
export function openPasswordWizard() {
  document.querySelector('dialog.pw-wizard')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard pw-wizard';
  dialog.setAttribute('aria-labelledby', 'pwWizTitle');
  dialog.style.width = 'min(480px, calc(100vw - 24px))';
  document.body.append(dialog);

  const STEPS = ['Current password', 'New password'];
  const state = { step: 1, current: '', error: '' };

  const close = () => { dialog.close(); dialog.remove(); };

  function render() {
    dialog.innerHTML = `
      <div class="wiz-head">
        <strong id="pwWizTitle">Change user password</strong>
        <button type="button" class="link wiz-x" data-pw="cancel" aria-label="Close">&times;</button>
      </div>
      <ol class="wiz-steps">
        ${STEPS.map((label, i) => {
          const n = i + 1;
          const cls = n === state.step ? 'current' : n < state.step ? 'done' : '';
          return `<li class="${cls}"><b>${n < state.step ? '✓' : n}</b>${esc(label)}</li>`;
        }).join('')}
      </ol>
      <form id="pwWizForm" novalidate style="display:contents">
        <div class="wiz-body">
          ${state.step === 1 ? `
            <p class="hint" style="margin:0 0 12px">Signed in as <b>${esc(auth.user?.email || '')}</b>. Enter your current password to continue.</p>
            ${field({ name: 'current_password', label: 'Current Password', type: 'password', required: true })}
          ` : `
            ${field({ name: 'new_password', label: 'New Password', type: 'password', required: true,
                      help: 'At least 10 characters.' })}
            ${field({ name: 'confirm', label: 'Confirm Password', type: 'password', required: true })}
            <p class="hint" style="margin:4px 0 0">Saving signs you out everywhere else.</p>
          `}
          <p class="err" id="pwWizError">${esc(state.error)}</p>
        </div>
        <div class="wiz-foot">
          ${state.step === 1
            ? '<button type="button" data-pw="cancel">Cancel</button><button class="primary" type="submit">Next</button>'
            : '<button type="button" data-pw="back">Back</button><button class="primary" type="submit" id="pwWizSave">Save new password</button>'}
        </div>
      </form>`;
    dialog.querySelectorAll('[data-pw=cancel]').forEach((b) => b.addEventListener('click', close));
    dialog.querySelector('[data-pw=back]')?.addEventListener('click', () => {
      state.step = 1; state.error = ''; render();
    });
    const form = $('#pwWizForm', dialog);
    form.addEventListener('submit', onSubmit);
    form.querySelector('input')?.focus();
  }

  async function onSubmit(event) {
    event.preventDefault();
    const values = readForm(event.target);
    const error = $('#pwWizError', dialog);
    if (state.step === 1) {
      if (!values.current_password) { error.textContent = 'Enter your current password.'; return; }
      state.current = values.current_password;
      state.step = 2; state.error = '';
      render();
      return;
    }
    let body;
    try {
      body = check({ ...values, current_password: state.current });
    } catch (exc) {
      error.textContent = exc.message;
      return;
    }
    const button = $('#pwWizSave', dialog);
    button.disabled = true;
    error.textContent = '';
    try {
      await api.post('/auth/change-password', body);
      close();
      toast('Password changed', 'ok');
    } catch (exc) {
      const message = exc.message || 'Could not change the password.';
      if (/current password/i.test(message)) {
        // Wrong current password: back to where it was typed.
        state.step = 1; state.current = ''; state.error = message;
        render();
      } else {
        error.textContent = message;
        button.disabled = false;
      }
    }
  }

  dialog.addEventListener('close', () => dialog.remove());
  render();
  dialog.showModal();
}



/** Settings → General → "Delete account": the owner closes the account.
 *
 *   1 Why        a reason is required ("Other" needs a few words), plus an
 *                optional note — it goes to BioBridge, not anywhere public
 *   2 Confirm    what is deleted, the Stripe subscription ending now, the
 *                company name typed back and the password
 *
 * On success the session is gone with the account, so it signs out. */
export async function openDeleteAccountWizard({ companyName, billedByStripe }) {
  document.querySelector('dialog.delete-account')?.remove();
  const reasons = await api.get('/tenant/exit-reasons');
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard delete-account';
  dialog.setAttribute('aria-labelledby', 'delAccTitle');
  dialog.style.width = 'min(540px, calc(100vw - 24px))';
  document.body.append(dialog);
  const state = { step: 1, reason_code: '', reason_text: '', error: '' };
  const close = () => { dialog.close(); dialog.remove(); };

  function render() {
    const steps = ['Why are you leaving?', 'Confirm'];
    dialog.innerHTML = `
      <div class="wiz-head"><strong id="delAccTitle">Delete account</strong>
        <button type="button" class="link wiz-x" data-x aria-label="Close">&times;</button></div>
      <ol class="wiz-steps">${steps.map((label, i) => {
        const n = i + 1;
        const cls = n === state.step ? 'current' : n < state.step ? 'done' : '';
        return `<li class="${cls}"><b>${n < state.step ? '✓' : n}</b>${esc(label)}</li>`;
      }).join('')}</ol>
      <form id="delAccForm" novalidate style="display:contents">
        <div class="wiz-body">
          ${state.step === 1 ? `
            <p class="hint" style="margin:0 0 10px">Please tell us why — it's required, and it helps us improve BioBridge.</p>
            <div class="exit-reasons" role="radiogroup" aria-label="Reason for leaving">
              ${reasons.map((r) => `<label style="display:flex;gap:10px;align-items:center;padding:6px 0;cursor:pointer;font-weight:400">
                <input type="radio" name="reason_code" value="${esc(r.code)}" style="width:auto;margin:0"${state.reason_code === r.code ? ' checked' : ''}>
                <span>${esc(r.label)}</span></label>`).join('')}
            </div>
            <div class="field" style="margin-top:8px"><label for="reason_text">Anything Else? <span class="opt" id="reasonOpt">optional</span></label>
              <textarea id="reason_text" name="reason_text" rows="3" maxlength="2000"
                placeholder="What would have made you stay?">${esc(state.reason_text)}</textarea></div>
          ` : `
            <div class="banner bad"><strong>This permanently deletes ${esc(companyName)}</strong>
              Your Odoo and biometric connections, devices, employee matches, every punch and attendance record BioBridge holds,
              and all users of this account. Attendance already written to Odoo stays in Odoo. This can't be undone.</div>
            ${billedByStripe ? `<div class="banner warn"><strong>Your subscription ends now</strong>
              It is cancelled immediately — nothing more is charged, and the rest of the current period is not refunded.</div>` : ''}
            <div class="field"><label for="confirm_name">Type <strong>${esc(companyName)}</strong> to confirm</label>
              <input id="confirm_name" name="confirm_name" autocomplete="off"></div>
            <div class="field"><label for="del_password">Your Password</label>
              <input id="del_password" name="password" type="password" autocomplete="current-password"></div>
          `}
          <p class="err" id="delAccErr">${esc(state.error)}</p>
        </div>
        <div class="wiz-foot">
          ${state.step === 1
            ? '<button type="button" data-x>Cancel</button><button type="submit" class="primary">Next</button>'
            : '<button type="button" data-back>Back</button><button type="submit" class="danger" id="delAccGo" disabled>Delete account permanently</button>'}
        </div>
      </form>`;
    dialog.querySelectorAll('[data-x]').forEach((b) => b.addEventListener('click', close));
    dialog.querySelector('[data-back]')?.addEventListener('click', () => { state.step = 1; state.error = ''; render(); });
    const form = $('#delAccForm', dialog);
    form.addEventListener('submit', onSubmit);
    if (state.step === 1) {
      const opt = $('#reasonOpt', dialog);
      const sync = () => {
        const other = form.querySelector('[name=reason_code]:checked')?.value === 'other';
        opt.textContent = other ? 'required for "Other"' : 'optional';
      };
      form.querySelectorAll('[name=reason_code]').forEach((r) => r.addEventListener('change', () => {
        $('#delAccErr', dialog).textContent = '';
        sync();
      }));
      sync();
    } else {
      const name = $('#confirm_name', dialog);
      const pw = $('#del_password', dialog);
      const go = $('#delAccGo', dialog);
      const check = () => {
        go.disabled = !(name.value.trim().toLowerCase() === companyName.trim().toLowerCase() && pw.value);
      };
      name.addEventListener('input', check);
      pw.addEventListener('input', check);
      name.focus();
    }
  }

  async function onSubmit(event) {
    event.preventDefault();
    const form = event.target;
    const error = $('#delAccErr', dialog);
    if (state.step === 1) {
      const code = form.querySelector('[name=reason_code]:checked')?.value || '';
      const text = form.querySelector('[name=reason_text]').value.trim();
      if (!code) { error.textContent = 'Choose a reason for leaving.'; return; }
      if (code === 'other' && text.length < 5) { error.textContent = 'Tell us a little about why you’re leaving.'; return; }
      Object.assign(state, { reason_code: code, reason_text: text, step: 2, error: '' });
      render();
      return;
    }
    const go = $('#delAccGo', dialog);
    go.disabled = true;
    error.textContent = '';
    try {
      await api.post('/tenant/delete', {
        reason_code: state.reason_code,
        reason_text: state.reason_text || null,
        confirm_name: form.querySelector('[name=confirm_name]').value,
        password: form.querySelector('[name=password]').value,
      });
      close();
      toast('Your account has been deleted. Thank you for using BioBridge.', 'ok');
      window.dispatchEvent(new CustomEvent('bb:signed-out'));
    } catch (exc) {
      error.textContent = exc.message || 'Your account was not deleted.';
      go.disabled = false;
    }
  }

  dialog.addEventListener('close', () => dialog.remove());
  render();
  dialog.showModal();
}


/** #/forgot-password — ask for a reset link. The reply is the same for any
 * address, so it never says whether an account exists. */
export function renderForgotPassword() {
  const root = authShell(`
    <h1>Forgot your password?</h1>
    <p class="sub">Enter the email you signed up with and we’ll send you a link to choose a new one.</p>
    <form id="fpForm">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      <button class="primary" style="width:100%" id="fpGo">Send reset link</button>
    </form>
    <p class="auth-alt"><a href="#/login">&larr; Back to sign in</a></p>`);
  $('#fpForm', root).addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = $('#authError', root);
    const button = $('#fpGo', root);
    error.textContent = '';
    button.disabled = true;
    try {
      const result = await post('/auth/forgot-password', readForm(event.target));
      root.querySelector('.auth-card').innerHTML = `
        <div class="brand"><span class="brand-mark">B</span><span class="brand-word"><b>Bio</b><span>Bridge</span></span></div>
        <h1>Check your email</h1>
        <p class="sub">${esc(result.message)} The link works once and expires in an hour.</p>
        <a class="btn" style="width:100%;text-align:center" href="#/login">Back to sign in</a>`;
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false;
    }
  });
}

/** #/reset-password?token=… — where the emailed link lands. */
export function renderResetPassword(route = {}) {
  const token = route.query?.token || '';
  const root = authShell(`
    <h1>Choose a new password</h1>
    <p class="sub">Pick a password you don’t use anywhere else. Signing in elsewhere will end once you save.</p>
    <form id="rpForm">
      ${passwordFields({ current: null })}
      <button class="primary" style="width:100%" id="rpGo">Reset password</button>
    </form>
    <p class="auth-alt"><a href="#/login">&larr; Back to sign in</a></p>`);
  $('#rpForm', root).addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = $('#authError', root);
    const button = $('#rpGo', root);
    error.textContent = '';
    button.disabled = true;
    try {
      const body = check(readForm(event.target));
      await post('/auth/reset-password', { token, new_password: body.new_password });
      root.querySelector('.auth-card').innerHTML = `
        <div class="brand"><span class="brand-mark">B</span><span class="brand-word"><b>Bio</b><span>Bridge</span></span></div>
        <h1>Password changed</h1>
        <p class="sub">You’re all set — sign in with your new password. Any other signed-in devices have been signed out.</p>
        <a class="btn primary" style="width:100%;text-align:center" href="#/login">Go to sign in</a>`;
    } catch (e) {
      error.innerHTML = /invalid or has expired/.test(e.message)
        ? `${esc(e.message)} <a href="#/forgot-password">Request a new link</a>` : esc(e.message);
      button.disabled = false;
    }
  });
}
