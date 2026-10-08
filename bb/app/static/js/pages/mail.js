/* Staff console → Email server: where signup confirmations, login details
 * and every other BioBridge email are sent from.
 *
 * Saved settings here take precedence over SMTP_* in .env and apply to the
 * next email on every process — no restart. The password is write-only:
 * the server says whether one is stored, never what it is. "Send test email"
 * goes through whatever is in use right now, so save before testing.
 */

import { api, auth } from '../api.js';
import { $, banner, busy, esc, field as baseField, fmtAgo, guard, loading, pill, readForm, toast } from '../ui.js';

const field = (o) => baseField({ tip: true, ...o });

const SECURITY = [
  { value: 'starttls', label: 'STARTTLS — Port 587 (Recommended)' },
  { value: 'ssl', label: 'SSL/TLS — Port 465' },
  { value: 'none', label: 'None — Port 25 (Not Recommended)' },
];
const PORT_FOR = { starttls: 587, ssl: 465, none: 25 };

const SOURCE_TEXT = {
  database: ['ok', 'Using These Settings'],
  environment: ['warn', 'Using SMTP_* from .env'],
  none: ['bad', 'Not sending — emails are only logged'],
};

export async function render(mount) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not Available', 'This section is for platform staff.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  const cfg = await api.get('/admin/mail');
  const gmail = cfg.presets?.gmail;
  const [tone, sourceText] = SOURCE_TEXT[cfg.active_source] || SOURCE_TEXT.none;

  mount.innerHTML = `
    ${cfg.active_source === 'none' ? banner('No Email Server Yet',
      'New customers never receive their confirmation link or login details until one is set up.', 'warn') : ''}
    ${cfg.active_source === 'environment' ? banner('Sending through .env',
      `Mail currently goes through ${esc(cfg.environment?.host || '')} from SMTP_* in .env. Saving here takes over from it.`) : ''}
    <form id="mailForm" class="card" novalidate>
      <div class="card-head">
        <h2>Email Server <span class="pill ${tone}">${esc(sourceText)}</span></h2>
        <div class="actions">
          ${gmail ? '<button type="button" id="useGmail">Fill In Gmail Settings</button>' : ''}
          <button class="primary" id="mailSave" type="submit">Save</button>
        </div>
      </div>
      ${gmail ? `<div class="hint" style="margin:0 0 14px">${esc(gmail.help)}</div>` : ''}
      <div class="grid cols-2" style="gap:0 16px">
        ${field({ name: 'host', label: 'SMTP Host', value: cfg.host, required: true, placeholder: 'smtp.gmail.com' })}
        ${field({ name: 'port', label: 'Port', type: 'number', value: cfg.port || 587, required: true })}
        ${field({ name: 'security', label: 'Security', value: cfg.security || 'starttls', options: SECURITY, required: true,
                  help: 'Has to match the port: STARTTLS on 587, SSL/TLS on 465.' })}
        ${field({ name: 'enabled', label: 'Use These Settings', boolean: true, required: true,
                  value: String(cfg.enabled !== false),
                  options: [{ value: 'true', label: 'On — Send Through This Server' },
                            { value: 'false', label: 'Off — Fall Back To .env' }] })}
        ${field({ name: 'username', label: 'Username', value: cfg.username, placeholder: 'no-reply@yourcompany.com',
                  help: 'The account to sign in as. Gmail / Workspace: the full address (left blank, the From address is used). Required with a password.' })}
        <div class="field">
          <label for="password">Password ${cfg.has_password ? '<span class="pill ok">saved</span>' : '<span class="opt">optional</span>'}</label>
          <input type="password" name="password" id="password" autocomplete="new-password"
                 placeholder="${cfg.has_password ? 'Leave blank to keep the saved one' : 'App Password for Gmail'}">
          ${cfg.has_password ? '<label class="hint" style="display:inline-flex;gap:8px;align-items:center;margin-top:8px;font-weight:400;cursor:pointer"><input type="checkbox" name="clear_password" style="width:auto;margin:0"> Remove The Saved Password</label>' : ''}
        </div>
        ${field({ name: 'from_email', label: 'From Address', type: 'email', value: cfg.from_email, required: true,
                  placeholder: 'no-reply@yourcompany.com',
                  help: 'For Gmail this must be the signed-in account or one of its verified "Send mail as" aliases.' })}
        ${field({ name: 'from_name', label: 'From Name', value: cfg.from_name || 'BioBridge', placeholder: 'BioBridge' })}
        ${field({ name: 'reply_to', label: 'Reply-To', type: 'email', value: cfg.reply_to, placeholder: 'support@yourcompany.com',
                  help: 'Where customer replies go, if not the From address.' })}
      </div>
      ${cfg.updated_at ? `<div class="hint" style="margin-top:6px">Last saved ${esc(fmtAgo(cfg.updated_at))}${
        cfg.updated_by ? ` by ${esc(cfg.updated_by)}` : ''}.</div>` : ''}
    </form>

    <form id="mailTest" class="card" style="margin-top:16px" novalidate>
      <div class="card-head"><h2>Send A Test Email <span class="hint">Uses The Settings In Use Right Now</span></h2></div>
      <div class="row">
        <input type="email" name="to" required value="${esc(auth.user?.email || '')}" style="max-width:320px" aria-label="Send the test to">
        <button class="primary" id="mailTestGo" type="submit" ${cfg.active_source === 'none' ? 'disabled' : ''}>Send Test Email</button>
      </div>
      <p class="err" id="mailTestError" style="margin-top:10px"></p>
    </form>`;

  const form = $('#mailForm', mount);
  // The browser's password manager sees "username + password" and fills in
  // the console sign-in (e.g. your own login email) — which Gmail then
  // rejects. Read-only until focused stops autofill; the saved value is
  // put back in case it already ran.
  const userInput = form.querySelector('[name=username]');
  userInput.setAttribute('autocomplete', 'off');
  userInput.setAttribute('data-lpignore', 'true');
  userInput.readOnly = true;
  userInput.addEventListener('focus', () => { userInput.readOnly = false; }, { once: true });
  setTimeout(() => { if (userInput.readOnly) userInput.value = cfg.username || ''; }, 300);
  const security = form.querySelector('[name=security]');
  const port = form.querySelector('[name=port]');
  security.addEventListener('change', () => {
    // Follow the security choice only while the port is still a standard one.
    if (Object.values(PORT_FOR).includes(Number(port.value))) port.value = PORT_FOR[security.value];
  });

  $('#useGmail', mount)?.addEventListener('click', () => {
    form.querySelector('[name=host]').value = gmail.host;
    port.value = gmail.port;
    security.value = gmail.security;
    const user = form.querySelector('[name=username]');
    if (!user.value) user.focus();
    toast('Gmail settings filled in — add the account, App Password and From address, then Save.', 'ok');
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(form);
    if (!values.password) delete values.password;
    if (!values.reply_to) values.reply_to = '';
    if (!values.from_email) delete values.from_email;
    values.clear_password = Boolean(values.clear_password);
    busy($('#mailSave', mount), async () => {
      const ok = await guard(() => api.patch('/admin/mail', values), 'Email Settings Saved');
      if (ok) render(mount);
    });
  });

  $('#mailTest', mount).addEventListener('submit', (event) => {
    event.preventDefault();
    const error = $('#mailTestError', mount);
    error.textContent = '';
    const { to } = readForm(event.target);
    if (!to) { error.textContent = 'Enter an address to send the test to.'; return; }
    busy($('#mailTestGo', mount), async () => {
      try {
        const result = await api.post('/admin/mail/test', { to });
        toast(result.message, 'ok');
      } catch (exc) {
        // Shown in place, not only as a toast: the reason is what the person
        // needs to fix the settings above, and it can be long.
        error.textContent = exc.message || 'The test email could not be sent.';
      }
    });
  });
}
