/* Staff console → Payments (Stripe): the keys online billing runs on.
 *
 * Saved keys take precedence over STRIPE_* in .env while switched on, and
 * apply from the next request — no restart. Both secrets are write-only:
 * only a hint (sk_test_…4242) comes back. Prices live on each plan (Plans
 * page); "Test connection" checks the key and every active plan's price.
 */

import { api, auth } from '../api.js';
import { $, banner, busy, esc, field as baseField, fmtAgo, guard, loading, readForm, toast } from '../ui.js';

const field = (o) => baseField({ tip: true, ...o });

const SOURCE = {
  database: ['ok', 'Using These Keys'],
  environment: ['warn', 'Using STRIPE_* from .env'],
  none: ['bad', 'Online Billing Is Off'],
};

const money = (cents) => (cents == null ? 'Custom' : `$${(cents / 100).toFixed(cents % 100 ? 2 : 0)}`);

export async function render(mount) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not Available', 'This section is for platform staff.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  const cfg = await api.get('/admin/stripe');
  const [tone, sourceText] = SOURCE[cfg.active_source] || SOURCE.none;
  const missingPrices = cfg.plans.filter((p) => p.is_active && !p.stripe_price_id);

  mount.innerHTML = `
    ${cfg.active_source === 'none' ? banner('Online Billing Is Off',
      'Website "Buy now", in-app checkout and automatic renewals all need a Stripe secret key.', 'warn') : ''}
    ${cfg.active_mode === 'test' ? banner('Test Mode',
      'Payments use Stripe test cards (4242 4242 4242 4242) and no money moves. Switch to live keys and live prices before launch.', '') : ''}
    ${cfg.active_source !== 'none' && !cfg.active_has_webhook_secret ? banner('Webhook Signing Secret Missing',
      'Without it every Stripe webhook is refused, so paid checkouts never create or activate accounts and renewals are never recorded.', 'bad') : ''}

    <form id="stripeForm" class="card" novalidate>
      <div class="card-head">
        <h2>Payments (Stripe) <span class="pill ${tone}">${esc(sourceText)}</span>${
          cfg.active_mode ? ` <span class="pill ${cfg.active_mode === 'live' ? 'ok' : 'warn'}">${esc(cfg.active_mode)} mode</span>` : ''}</h2>
        <div class="actions"><button class="primary" id="stripeSave" type="submit">Save</button></div>
      </div>
      <div class="grid cols-2" style="gap:0 16px">
        ${secretField('secret_key', 'Secret Key', cfg.secret_key_hint, 'sk_test_… or sk_live_…',
          'Stripe Dashboard → Developers → API keys. A restricted key (rk_…) works too if it can read prices and write customers, Checkout sessions, subscriptions and portal sessions.')}
        ${secretField('webhook_secret', 'Webhook Signing Secret', cfg.has_webhook_secret ? 'saved' : null, 'whsec_…',
          'Shown on the webhook endpoint in Stripe (Developers → Webhooks → your endpoint → Signing secret).')}
        ${field({ name: 'enabled', label: 'Use These Keys', boolean: true, required: true, value: String(cfg.enabled !== false),
          options: [{ value: 'true', label: 'On — Bill Through These Keys' }, { value: 'false', label: 'Off — Fall Back To .env' }] })}
      </div>
      ${cfg.environment_key_hint && cfg.active_source !== 'environment' ? `<div class="hint">.env also has a key (${esc(cfg.environment_key_hint)}); it's used only while these are off or empty.</div>` : ''}
      ${cfg.updated_at ? `<div class="hint" style="margin-top:6px">Last saved ${esc(fmtAgo(cfg.updated_at))}${cfg.updated_by ? ` by ${esc(cfg.updated_by)}` : ''}.</div>` : ''}
    </form>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Webhook <span class="hint">Stripe → BioBridge</span></h2></div>
      <p class="hint" style="margin:0 0 8px">In Stripe, add an endpoint at this address and subscribe it to the events below, then paste its signing secret above.</p>
      <div class="row"><code id="whUrl" style="padding:8px 10px;background:var(--bg);border-radius:8px;word-break:break-all">${esc(cfg.webhook_url)}</code>
        <button type="button" class="sm" id="copyWh">Copy</button></div>
      <p class="hint" style="margin:10px 0 0">Events: ${cfg.webhook_events.map((e) => `<code>${esc(e)}</code>`).join(', ')}</p>
      <p class="hint" style="margin:8px 0 0">Last event received: <strong>${esc(cfg.last_event_at ? fmtAgo(cfg.last_event_at) : 'never')}</strong></p>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Plans And Prices</h2>
        <div class="actions"><a class="btn sm" href="#/platform/plans">Edit Plans</a>
          <button type="button" class="primary sm" id="stripeTest" ${cfg.active_source === 'none' ? 'disabled' : ''}>Test Connection</button></div></div>
      ${missingPrices.length ? banner(`${missingPrices.length} active plan${missingPrices.length === 1 ? '' : 's'} can't be bought online`,
        'Add each one’s Stripe price id (price_…, a monthly recurring price) on the Plans page.', 'warn') : ''}
      <table><thead><tr><th>Plan</th><th class="num">Price / Mo</th><th>Stripe Price</th><th>Check</th></tr></thead>
        <tbody>${cfg.plans.map((p) => `<tr data-plan="${esc(p.name)}">
          <td>${esc(p.name)}${p.is_active ? '' : ' <span class="pill mute">retired</span>'}</td>
          <td class="num">${esc(money(p.monthly_price_cents))}</td>
          <td class="mono">${p.stripe_price_id ? esc(p.stripe_price_id) : '<span class="hint">None</span>'}</td>
          <td class="check hint">—</td></tr>`).join('')}</tbody></table>
      <p class="err" id="stripeTestError" style="margin-top:10px"></p>
    </div>`;

  const form = $('#stripeForm', mount);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = readForm(form);
    for (const key of ['secret_key', 'webhook_secret']) {
      if (!values[key]) delete values[key];
      else values[key] = values[key].trim();
    }
    values.clear_secret_key = Boolean(values.clear_secret_key);
    values.clear_webhook_secret = Boolean(values.clear_webhook_secret);
    busy($('#stripeSave', mount), async () => {
      if (await guard(() => api.patch('/admin/stripe', values), 'Stripe Settings Saved')) render(mount);
    });
  });

  $('#copyWh', mount).addEventListener('click', () => {
    navigator.clipboard?.writeText(cfg.webhook_url).then(() => toast('Webhook URL Copied', 'ok'));
  });

  $('#stripeTest', mount).addEventListener('click', (event) => busy(event.currentTarget, async () => {
    const error = $('#stripeTestError', mount);
    error.textContent = '';
    try {
      const result = await api.post('/admin/stripe/test');
      toast(result.message, result.ok ? 'ok' : 'bad');
      if (!result.webhook_secret_set) error.textContent = result.message;
      for (const row of result.plans) {
        const cell = mount.querySelector(`tr[data-plan="${CSS.escape(row.plan)}"] .check`);
        if (cell) cell.innerHTML = `<span class="pill ${row.ok ? 'ok' : 'bad'}">${row.ok ? 'ok' : 'problem'}</span> ${esc(row.message)}`;
      }
    } catch (exc) {
      error.textContent = exc.message || 'Stripe could not be reached.';
    }
  }));
}

function secretField(name, label, hint, placeholder, help) {
  return `
    <div class="field">
      <label for="${name}">${esc(label)} ${hint ? `<span class="pill ok">${esc(hint)}</span>` : '<span class="opt">not set</span>'}
        <span class="field-tip" tabindex="0" role="note" aria-label="${esc(help)}" data-tip="${esc(help)}">ⓘ</span></label>
      <input type="password" name="${name}" id="${name}" autocomplete="new-password" spellcheck="false"
             placeholder="${hint ? 'Leave blank to keep the saved one' : esc(placeholder)}">
      ${hint ? `<label class="hint" style="display:inline-flex;gap:8px;align-items:center;margin-top:8px;font-weight:400;cursor:pointer">
        <input type="checkbox" name="clear_${name}" style="width:auto;margin:0"> Remove The Saved One</label>` : ''}
    </div>`;
}
