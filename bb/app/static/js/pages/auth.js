/* Sign in and sign up. Rendered into #auth-root, outside the app shell. */

import { api, auth } from '../api.js';
import { $, esc, field, readForm } from '../ui.js';

function shell(inner) {
  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="brand"><span class="brand-dot"></span>BioBridge</div>
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

export function renderSignup() {
  // Pre-fill the browser's zone: it is right often enough to save a step, and
  // wrong in a way the user can see and correct.
  const guess = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

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
      <button class="primary" style="width:100%" id="go">Create account</button>
    </form>
    <p class="auth-alt">Already have one? <a href="#/login">Sign in</a></p>`);

  $('#form', root).addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(event.target);
    if (!values.full_name) delete values.full_name;
    submit($('#go', root), async () => {
      afterAuth(await api.post('/auth/signup', values));
    });
  });
}
