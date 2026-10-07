/* Staff console → Plans: the tiers accounts are sold under.
 *
 * Create a plan, change its price and limits, link it to a Stripe Price,
 * make it the default for new accounts, or retire it. Plans are never
 * deleted: an account already on a retired plan keeps it and keeps working,
 * and the row is the only record of what that plan promised.
 *
 * A limit left empty is unlimited. Editing a plan's limits applies to every
 * account on it straight away; one account's exception belongs on that
 * account (All accounts → Configure → Limits), not on a copy of the plan.
 */

import { api, auth } from '../api.js';
import {
  $, $$, banner, busy, empty, esc, field as baseField, guard, loading, pill, readForm, toast,
} from '../ui.js';

const field = (o) => baseField({ tip: true, ...o });

const money = (cents) => (cents == null ? 'Custom' : `$${(cents / 100).toFixed(cents % 100 ? 2 : 0)}`);
const cap = (n, unit) => (n == null ? '<span class="hint">unlimited</span>' : `${esc(n)} ${unit}`);

export async function render(mount) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not available', 'This section is for platform staff.', 'warn');
    return;
  }
  mount.innerHTML = loading();
  const plans = await api.get('/admin/plans');

  mount.innerHTML = `
    <div class="page-bar">
      <span class="hint">${plans.filter((p) => p.is_active).length} active · ${
        plans.filter((p) => !p.is_active).length} retired</span>
      <button class="primary sm" id="newPlan">New plan</button>
    </div>
    <div class="card">
      ${plans.length ? `
        <div class="scroll">
          <table class="tight-table plans-table">
            <thead><tr>
              <th>Plan</th><th class="num">Price / mo</th><th class="num">Employees</th>
              <th class="num">Devices</th><th class="num">Fastest sync</th><th>Stripe</th>
              <th class="num">Accounts</th><th>Status</th><th></th>
            </tr></thead>
            <tbody>${plans.map(rowFor).join('')}</tbody>
          </table>
        </div>
        <div class="hint" style="margin-top:12px">Changing a plan's limits applies to every account
          on it from the next sync. For one customer's exception, use
          <a href="#/platform">All accounts</a> → Configure → Limits.</div>`
        : empty('No plans yet', 'Create the first one — every account without a plan has no limits.')}
    </div>`;

  const refresh = () => render(mount);
  $('#newPlan', mount).addEventListener('click', () => openPlanDialog(null, refresh));
  $$('tr[data-plan]', mount).forEach((tr) => {
    $('.edit', tr).addEventListener('click', () =>
      openPlanDialog(plans.find((p) => p.id === tr.dataset.plan), refresh));
    // Two clicks, no confirm(): the first turns the button into the
    // confirmation, which resets itself after a few seconds.
    const del = $('.del', tr);
    del?.addEventListener('click', () => {
      if (!del.classList.contains('armed')) {
        del.classList.add('armed');
        del.textContent = 'Click again to delete';
        del.classList.replace('danger-outline', 'danger');
        setTimeout(() => {
          if (!del.isConnected) return;
          del.classList.remove('armed');
          del.classList.replace('danger', 'danger-outline');
          del.textContent = 'Delete';
        }, 4000);
        return;
      }
      busy(del, async () => {
        const result = await guard(() => api.del(`/admin/plans/${tr.dataset.plan}`));
        if (result) { toast(result.message, 'ok'); refresh(); }
      });
    });
  });
}

function rowFor(p) {
  return `
    <tr data-plan="${esc(p.id)}">
      <td><strong>${esc(p.name)}</strong>${p.is_default ? ' <span class="pill ok">default</span>' : ''}
        ${p.description ? `<div class="hint">${esc(p.description)}</div>` : ''}</td>
      <td class="num">${money(p.monthly_price_cents)}${p.yearly_price_cents != null
        ? `<div class="hint">${money(p.yearly_price_cents)} / yr</div>` : ''}</td>
      <td class="num">${cap(p.max_employees, '')}</td>
      <td class="num">${cap(p.max_devices, '')}</td>
      <td class="num">${p.min_sync_interval_minutes ? `${esc(p.min_sync_interval_minutes)} min` : '<span class="hint">any</span>'}</td>
      <td>${p.stripe_price_id ? `<span class="mono hint">${esc(p.stripe_price_id)}</span>` : '<span class="hint">not sold online</span>'}</td>
      <td class="num">${esc(p.tenants)}</td>
      <td>${pill(p.is_active ? 'active' : 'skipped', p.is_active ? 'active' : 'retired')}</td>
      <td class="actions-cell"><div class="row-actions">
        <button class="sm edit">Edit</button>
        ${p.tenants ? '' : '<button type="button" class="sm danger-outline del" title="Delete this plan — only possible while no account is on it">Delete</button>'}
      </div></td>
    </tr>`;
}

function openPlanDialog(plan, onSaved) {
  document.querySelector('dialog.plan-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.className = 'wizard config-dialog plan-dialog';
  document.body.append(dialog);
  const p = plan || { is_active: true, is_default: false };
  const close = () => { dialog.close(); dialog.remove(); };

  dialog.innerHTML = `
    <div class="wiz-head">
      <strong>${plan ? `Edit ${esc(plan.name)}` : 'New plan'}</strong>
      <button type="button" class="link wiz-x" data-close aria-label="Close">&times;</button>
    </div>
    <div class="wiz-body">
      <form id="planForm">
        <div class="grid cols-2">
          ${field({ name: 'name', label: 'Name', value: p.name || '', required: true, placeholder: 'Growth' })}
          ${field({ name: 'price', label: 'Price Per Month (USD)', type: 'number',
                    value: p.monthly_price_cents != null ? p.monthly_price_cents / 100 : '',
                    help: 'What pricing pages show. Leave empty for "Custom". What a customer is '
                        + 'actually charged is the Stripe Price below — keep the two in step.' })}
        </div>
        ${field({ name: 'yearly_price', label: 'Price Per Year (USD)', type: 'number',
                  value: p.yearly_price_cents != null ? p.yearly_price_cents / 100 : '',
                  help: 'Shown when a visitor switches the pricing page to yearly. Empty = no yearly option. '
                      + 'Charged through the yearly Stripe Price below.' })}
        ${field({ name: 'description', label: 'Description', value: p.description || '',
                  placeholder: 'Growing teams on several devices.' })}
        <h3 style="margin:6px 0 10px;font-size:13px">Limits <span class="hint">empty = unlimited</span></h3>
        <div class="grid cols-3">
          ${field({ name: 'max_employees', label: 'Employees', type: 'number', value: p.max_employees ?? '',
                    help: 'Badges matched to an Odoo employee. Lowering it never unmaps anyone — only new matches wait.' })}
          ${field({ name: 'max_devices', label: 'Devices', type: 'number', value: p.max_devices ?? '',
                    help: 'Terminals the account has added; the oldest fill the allowance. Punches from '
                        + 'terminals beyond it are held, and released when the limit covers them.' })}
          ${field({ name: 'min_sync_interval_minutes', label: 'Fastest Sync (Min)', type: 'number',
                    value: p.min_sync_interval_minutes ?? '',
                    help: 'The fastest interval a customer on this plan can choose. Assigning the plan '
                        + 'raises a faster interval to this.' })}
        </div>
        <h3 style="margin:6px 0 10px;font-size:13px">Selling</h3>
        <div class="grid cols-2">
          ${field({ name: 'stripe_yearly_price_id', label: 'Stripe Yearly Price', value: p.stripe_yearly_price_id || '',
                    placeholder: 'price_…',
                    help: 'A recurring yearly Price. Empty = yearly payment cannot be bought online.' })}
        </div>
        <div class="grid cols-3">
          ${field({ name: 'stripe_price_id', label: 'Stripe Price', value: p.stripe_price_id || '',
                    placeholder: 'price_…',
                    help: 'The monthly Price customers are charged. Empty = cannot be bought online; '
                        + 'staff assign it. Changing it affects new checkouts and switches only.' })}
          ${field({ name: 'is_active', label: 'Status', value: String(p.is_active), boolean: true, required: true,
                    options: [{ value: 'true', label: 'Active — can be chosen' },
                              { value: 'false', label: 'Retired — kept for accounts on it' }] })}
          ${field({ name: 'is_default', label: 'Default For New Accounts', value: String(p.is_default),
                    boolean: true, required: true,
                    options: [{ value: 'false', label: 'No' }, { value: 'true', label: 'Yes' }],
                    help: 'Given to a signup or a staff-created account that picks nothing. Only one plan can be default.' })}
        </div>
        ${plan && plan.tenants ? `<div class="hint" style="margin-top:4px">${esc(plan.tenants)} account${
          plan.tenants === 1 ? ' is' : 's are'} on this plan — new limits apply to them from the next sync.</div>` : ''}
      </form>
    </div>
    <div class="wiz-foot">
      <button type="button" class="link" data-close>Cancel</button>
      <div class="actions"><button type="submit" form="planForm" class="primary save">${plan ? 'Save plan' : 'Create plan'}</button></div>
    </div>`;

  $$('[data-close]', dialog).forEach((b) => b.addEventListener('click', close));
  dialog.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  dialog.addEventListener('click', (e) => { if (e.target === dialog) close(); });

  $('#planForm', dialog).addEventListener('submit', (event) => {
    event.preventDefault();
    const v = readForm(event.target);
    const body = {
      name: v.name.trim(),
      description: v.description.trim() || null,
      monthly_price_cents: v.price == null ? null : Math.round(v.price * 100),
      max_employees: v.max_employees || null,
      max_devices: v.max_devices || null,
      min_sync_interval_minutes: v.min_sync_interval_minutes || null,
      stripe_price_id: v.stripe_price_id.trim() || null,
      yearly_price_cents: v.yearly_price == null ? null : Math.round(v.yearly_price * 100),
      stripe_yearly_price_id: (v.stripe_yearly_price_id || '').trim() || null,
      is_active: v.is_active,
      is_default: v.is_default,
    };
    busy($('.save', dialog), async () => {
      const saved = await guard(() => (plan
        ? api.patch(`/admin/plans/${plan.id}`, body)
        : api.post('/admin/plans', body)));
      if (!saved) return;
      close();
      toast(`${saved.name} ${plan ? 'saved' : 'created'}`, 'ok');
      onSaved();
    });
  });

  dialog.showModal();
  $('#name', dialog).focus();
}
