/* Settings → Billing: the subscription after the first purchase.
 *
 * Renewals are charged automatically by Stripe to the saved card every
 * month. This page shows when and how much, the card it goes to, every
 * invoice (with its PDF), and the fixes a customer needs on their own:
 * pay an overdue invoice, change the card, cancel at period end or undo
 * that. Paying and changing the card happen on Stripe's own pages — no card
 * number ever touches BioBridge.
 */

import { api, auth } from '../api.js';
import { $, $$, banner, busy, empty, esc, guard, loading, pill, toast } from '../ui.js';

const money = (cents, currency) => {
  if (cents == null) return '—';
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: (currency || 'usd').toUpperCase() })
      .format(cents / 100);
  } catch {
    return `${(cents / 100).toFixed(2)} ${(currency || '').toUpperCase()}`;
  }
};
const day = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—');

const INVOICE_TONE = { paid: 'active', open: 'pending', uncollectible: 'failed', void: 'skipped' };
const SUB_LABEL = {
  active: 'Active', trialing: 'Active', past_due: 'Payment overdue', unpaid: 'Unpaid',
  canceled: 'Cancelled', incomplete: 'Awaiting payment', incomplete_expired: 'Expired',
};
const SUB_TONE = { active: 'active', trialing: 'active', past_due: 'failed', unpaid: 'failed', canceled: 'skipped' };

/** Send the browser to a Stripe-hosted page returned by ``path``. */
const goTo = (button, path) => busy(button, () => guard(async () => {
  const { url } = await api.post(path);
  window.location.href = url;
}));

export async function render(mount, route) {
  mount.innerHTML = loading();
  if (route?.query?.card === 'updated') {
    history.replaceState(null, '', '#/settings/billing');
    toast('Payment method updated — future renewals are charged to it.', 'ok');
  }
  const data = await api.get('/billing/overview');
  const readonly = !auth.canWrite;

  if (!data.enabled) {
    mount.innerHTML = `<div class="card">${empty('Online Billing Is Not Set Up',
      'Your subscription is handled by BioBridge support. Contact them for invoices or renewals.')}</div>`;
    return;
  }
  if (!data.has_customer) {
    mount.innerHTML = `<div class="card">
      ${empty('No Subscription Yet', 'Choose a plan and pay on Stripe — it then renews automatically every month.')}
      ${readonly ? '' : '<div class="row" style="justify-content:center;margin-top:12px"><a class="btn primary-link" href="#/settings/billing/choose">Choose A Plan</a></div>'}
    </div>`;
    return;
  }

  const sub = data.subscription;
  const open = data.open_invoice;
  const card = data.payment_method;
  const overdue = sub && ['past_due', 'unpaid'].includes(sub.status);

  mount.innerHTML = `
    ${data.error ? banner('Stripe Could Not Be Reached', data.error, 'bad') : ''}
    ${open ? `<div class="banner bad"><strong>${overdue ? 'Your Renewal Payment Failed' : 'An invoice is waiting for payment'}</strong>
        ${esc(money(open.amount_remaining ?? open.amount_due, open.currency))} is due${
          open.next_payment_attempt ? ` — Stripe will retry the saved card on ${esc(day(open.next_payment_attempt))}` : ''}.
        Pay now with any card to keep syncing.
        ${readonly ? '' : `<button type="button" class="primary sm" data-pay="${esc(open.id)}" style="margin-left:8px">Pay Now</button>`}</div>` : ''}

    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Subscription ${sub ? pill(SUB_TONE[sub.status] || sub.status, SUB_LABEL[sub.status] || sub.status) : pill('skipped', 'Ended')}</h2></div>
        ${sub ? `
          <table><tbody>
            <tr><td>Plan</td><td style="text-align:right"><strong>${esc(data.plan_name || '—')}</strong></td></tr>
            <tr><td>Price</td><td style="text-align:right">${esc(money((sub.unit_amount ?? 0) * (sub.quantity || 1), sub.currency))} / ${esc(sub.interval)}</td></tr>
            <tr><td>${sub.cancel_at_period_end ? 'Ends On' : 'Next renewal'}</td>
                <td style="text-align:right">${esc(day(sub.cancel_at || sub.current_period_end))}</td></tr>
            <tr><td>Renewal</td><td style="text-align:right">${sub.cancel_at_period_end
              ? '<span class="pill warn">Won’t renew</span>'
              : `Charged automatically${card?.last4 ? ` to ${esc(cardName(card))}` : ''}`}</td></tr>
          </tbody></table>
          ${sub.cancel_at_period_end ? `<div class="hint" style="margin-top:10px">Cancelled — the account keeps working until ${esc(day(sub.cancel_at || sub.current_period_end))}, then syncing stops.</div>` : ''}
          ${readonly ? '' : `<div class="row" style="margin-top:14px">
            <a class="btn" href="#/settings/billing/choose">Change Plan</a>
            ${sub.cancel_at_period_end
              ? '<button type="button" class="primary" id="resumeSub">Keep My Subscription</button>'
              : '<button type="button" id="cancelSub">Cancel Subscription</button>'}
          </div>
          <div id="cancelConfirm" hidden class="banner warn" style="margin-top:12px">
            <strong>Cancel At The End Of This Period?</strong>
            Nothing more is charged. BioBridge keeps syncing until ${esc(day(sub.current_period_end))}, then stops. You can undo this until then.
            <div class="row" style="margin-top:10px">
              <button type="button" class="primary sm" id="cancelYes">Yes, Cancel At Period End</button>
              <button type="button" class="sm danger" id="cancelNowBtn">End It Now Instead</button>
              <button type="button" class="sm" id="cancelNo">Keep It</button>
            </div>
            <div class="hint" style="margin-top:8px">Ending it now stops syncing straight away and the rest of the paid period is not refunded.
              Your account, connections and history are kept either way; delete the account from Settings → General if you want it gone.
            </div>
          </div>`}`
        : `<p class="hint">This account has no running subscription.</p>
           ${readonly ? '' : '<div class="row" style="margin-top:12px"><a class="btn primary-link" href="#/settings/billing/choose">Resubscribe</a></div>'}`}
      </div>

      <div class="card">
        <div class="card-head"><h2>Payment Method</h2></div>
        ${card ? `<p style="margin:0 0 6px"><strong>${esc(cardName(card))}</strong></p>
          ${card.exp_month ? `<p class="hint" style="margin:0">Expires ${esc(String(card.exp_month).padStart(2, '0'))}/${esc(card.exp_year)}</p>` : ''}`
          : '<p class="hint">No card saved — renewals can’t be charged automatically.</p>'}
        ${readonly ? '' : `<div class="row" style="margin-top:14px">
          <button type="button" id="updateCard">${card ? 'Update Card' : 'Add A Card'}</button>
          <span class="hint">Opens Stripe’s secure page.</span></div>`}
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Invoices <span class="hint">Last 12</span></h2></div>
      ${data.invoices.length ? `<div class="scroll"><table>
        <thead><tr><th>Date</th><th>Invoice</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead>
        <tbody>${data.invoices.map((inv) => `
          <tr>
            <td>${esc(day(inv.created))}</td>
            <td class="mono">${esc(inv.number || inv.id)}</td>
            <td class="num">${esc(money(inv.status === 'paid' ? inv.amount_paid : inv.amount_due, inv.currency))}</td>
            <td>${pill(INVOICE_TONE[inv.status] || inv.status, inv.status === 'open' ? 'due' : inv.status)}</td>
            <td style="text-align:right;white-space:nowrap">
              ${inv.payable && !readonly ? `<button type="button" class="primary sm" data-pay="${esc(inv.id)}">Pay</button>` : ''}
              ${inv.hosted_invoice_url ? `<a class="btn sm" href="${esc(inv.hosted_invoice_url)}" target="_blank" rel="noopener">View</a>` : ''}
              ${inv.invoice_pdf ? `<a class="btn sm" href="${esc(inv.invoice_pdf)}" target="_blank" rel="noopener">PDF</a>` : ''}
            </td>
          </tr>`).join('')}</tbody></table></div>`
        : empty('No Invoices Yet', 'The first one appears after your first payment.')}
    </div>`;

  $$('[data-pay]', mount).forEach((b) =>
    b.addEventListener('click', () => goTo(b, `/billing/invoices/${encodeURIComponent(b.dataset.pay)}/pay`)));
  $('#updateCard', mount)?.addEventListener('click', (e) => goTo(e.currentTarget, '/billing/payment-method'));

  const confirm = $('#cancelConfirm', mount);
  $('#cancelSub', mount)?.addEventListener('click', () => { confirm.hidden = false; });
  $('#cancelNo', mount)?.addEventListener('click', () => { confirm.hidden = true; });
  $('#cancelYes', mount)?.addEventListener('click', (e) => busy(e.currentTarget, async () => {
    if (await guard(() => api.post('/billing/cancel'), 'Subscription cancelled — it won’t renew.')) render(mount);
  }));
  $('#cancelNowBtn', mount)?.addEventListener('click', (e) => busy(e.currentTarget, async () => {
    if (await guard(() => api.post('/billing/cancel', { when: 'now' }),
      'Subscription ended — syncing has stopped. Your account is kept.')) render(mount);
  }));
  $('#resumeSub', mount)?.addEventListener('click', (e) => busy(e.currentTarget, async () => {
    if (await guard(() => api.post('/billing/resume'), 'Subscription resumed — it renews as normal.')) render(mount);
  }));
}

function cardName(card) {
  const brand = card.brand ? card.brand.charAt(0).toUpperCase() + card.brand.slice(1) : 'Card';
  return card.last4 ? `${brand} •••• ${card.last4}` : brand;
}
