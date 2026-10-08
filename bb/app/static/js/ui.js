/* Rendering helpers. Everything that reaches innerHTML goes through esc(). */

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/* --- formatting ----------------------------------------------------------- */

/** Datetimes from this API are naive UTC; render them as such, labelled. */
export function fmtUtc(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 19);
}

/** The *_local columns are already the tenant's wall clock — never reconvert. */
export function fmtLocal(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 16);
}

/** What started a sync run, for display: Scheduler (the timer or the
 * maintenance pass) or Manual (someone pressed a button). */
export const triggerLabel = (value) =>
  (value === 'schedule' || value === 'maintenance') ? 'Scheduler' : 'Manual';

const plural = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`;

export function fmtAgo(value) {
  if (!value) return 'never';
  const then = new Date(/Z|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z');
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (Number.isNaN(seconds)) return '—';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${plural(Math.floor(seconds / 60), 'minute')} ago`;
  if (seconds < 86400) return `${plural(Math.floor(seconds / 3600), 'hour')} ago`;
  return `${plural(Math.floor(seconds / 86400), 'day')} ago`;
}

/** The mirror of fmtAgo, for a time in the future: "in 7 min". */
export function fmtIn(value) {
  if (!value) return '—';
  const then = new Date(/Z|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z');
  const seconds = Math.round((then.getTime() - Date.now()) / 1000);
  if (Number.isNaN(seconds)) return '—';
  // Already due, but the tick has not come round yet. "in -20 s" reads as a bug;
  // the schedule being a tick behind is normal.
  if (seconds <= 30) return 'any moment';
  if (seconds < 90) return 'in about a minute';
  if (seconds < 3600) return `in ${plural(Math.round(seconds / 60), 'minute')}`;
  if (seconds < 86400) return `in ${plural(Math.round(seconds / 3600), 'hour')}`;
  return `in ${plural(Math.round(seconds / 86400), 'day')}`;
}

export function fmtHours(value) {
  if (value === null || value === undefined) return '—';
  return `${Number(value).toFixed(2)} h`;
}

export function todayISO(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

/* --- state vocabulary ----------------------------------------------------- */
const TONES = {
  connected: 'ok', success: 'ok', synced: 'ok', mapped: 'ok', active: 'ok',
  partial: 'warn', degraded: 'warn', pending: 'warn', unmapped: 'warn',
  ambiguous: 'warn', unverified: 'warn', trialing: 'warn', running: 'warn',
  failed: 'bad', error: 'bad', missing: 'bad',
  skipped: 'mute', ignored: 'mute',
};

export const tone = (state) => TONES[String(state || '').toLowerCase()] || 'mute';
export const pill = (state, label) =>
  `<span class="pill ${tone(state)}">${esc(label ?? state ?? '—')}</span>`;

/* --- components ----------------------------------------------------------- */
/** A number tile. With ``href`` the whole tile is a link to the list behind
 * the number — "2 errors" should take you to the two errors. */
export const stat = ({ label, value, note, tone: t, href }) => {
  const inner = `
    <div class="k">${esc(label)}</div>
    <div class="v ${t || ''}">${esc(value)}</div>
    ${note ? `<div class="note">${esc(note)}</div>` : ''}`;
  return href
    ? `<a class="stat stat-link" href="${esc(href)}">${inner}<span class="stat-go" aria-hidden="true">&rsaquo;</span></a>`
    : `<div class="stat">${inner}</div>`;
};

export const empty = (title, body) => `
  <div class="empty"><strong>${esc(title)}</strong>${body ? esc(body) : ''}</div>`;

/** A notice. ``action`` ({ href, label }) adds the one link that fixes it,
 * so a banner never tells you about a problem without saying where to go. */
export const banner = (title, body, kind = '', action = null) => `
  <div class="banner ${kind}"><strong>${esc(title)}</strong>${body ? esc(body) : ''}${
    action ? `<a class="banner-action" href="${esc(action.href)}">${esc(action.label)} &rarr;</a>` : ''}</div>`;

export const loading = () => '<div class="skeleton">Loading…</div>';

const INFO_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
  + '<circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" stroke-width="1.4"/>'
  + '<path d="M8 7.2v3.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
  + '<circle cx="8" cy="5" r=".85" fill="currentColor"/></svg>';

/* ``tip`` moves ``help`` out of the flow and into a small (i) beside the label,
 * shown by the app-wide hover / focus label (wireTips). For forms whose
 * fields are self-explanatory most of the time, where a paragraph under
 * every box is more noise than guidance. */
/** "Server timezone" → "Server Timezone": the first letter of every word in a
 * form label is capitalised, whatever wrote the label (a page, or a provider's
 * config_fields). Only first letters change, so "API", "URL" and "BioStar"
 * stay as they are. */
export const titleCase = (text) => String(text ?? '').replace(/(^|[\s/(])([a-z])/g, (_m, lead, ch) => lead + ch.toUpperCase());

export function field({ name, label, type = 'text', value = '', help, required, placeholder, options, strongHelp, boolean, datalist, tip, items, emptyNote, noRequiredAttr }) {
  // A select always yields a string, so a "false" option would PATCH the string
  // "false" — truthy everywhere on the server. data-bool tells readForm to
  // convert it. Explicit rather than sniffing the value, so a genuinely
  // string-valued "true" option never gets silently rewritten.
  const control = options
    ? `<select name="${esc(name)}" id="${esc(name)}"${boolean ? ' data-bool="1"' : ''}>${options
        .map((o) => {
          const v = typeof o === 'string' ? o : o.value;
          const l = typeof o === 'string' ? o : o.label;
          return `<option value="${esc(v)}"${v === value ? ' selected' : ''}>${esc(l)}</option>`;
        })
        .join('')}</select>`
    : items && items.length
    // A searchable dropdown over {value, label, hint} items — the visible box
    // shows the label, the paired hidden input carries the value.
    ? `<div class="picker search-select" data-search-select data-items="${esc(JSON.stringify(items))}"
            ${emptyNote ? `data-empty="${esc(emptyNote)}"` : ''}>
         <input type="text" id="${esc(name)}" class="ss-input" autocomplete="off" spellcheck="false"
                role="combobox" aria-expanded="false" aria-autocomplete="list"
                value="${esc(items.find((i) => i.value === value)?.label || '')}"
                ${placeholder ? `placeholder="${esc(placeholder)}"` : ''}>
         <span class="ss-caret" aria-hidden="true"></span>
         <input type="hidden" name="${esc(name)}" value="${esc(value)}">
         <ul class="picker-list hidden" role="listbox"></ul>
       </div>`
    : datalist && datalist.length
    // A searchable dropdown, not a plain <input list>/<datalist> (browsers
    // render that inconsistently, and it never really reads as "a dropdown").
    // The visible box is filter-as-you-type; the real value for the form
    // lives in the paired hidden input — see wireSearchSelects.
    ? `<div class="picker search-select" data-search-select>
         <input type="text" id="${esc(name)}" class="ss-input" autocomplete="off" spellcheck="false"
                role="combobox" aria-expanded="false" aria-autocomplete="list"
                value="${esc(value)}" ${placeholder ? `placeholder="${esc(placeholder)}"` : ''}>
         <span class="ss-caret" aria-hidden="true"></span>
         <input type="hidden" name="${esc(name)}" value="${esc(value)}">
         <ul class="picker-list hidden" role="listbox"></ul>
       </div>`
    : `<input type="${esc(type)}" name="${esc(name)}" id="${esc(name)}"
         value="${esc(value)}" ${required && !noRequiredAttr ? 'required' : ''}
         ${placeholder ? `placeholder="${esc(placeholder)}"` : ''}>`;
  return `
    <div class="field">
      <label for="${esc(name)}">${esc(titleCase(label))}${required ? '' : ' <span class="opt">optional</span>'}${
        help && tip ? `<span class="field-tip${strongHelp ? ' strong' : ''}" tabindex="0" role="note"
          aria-label="${esc(help)}" data-tip="${esc(help)}">${INFO_ICON}</span>` : ''}</label>
      ${control}
      ${help && !tip ? `<div class="help ${strongHelp ? 'strong' : ''}">${esc(help)}</div>` : ''}
    </div>`;
}

// A browser without Intl.supportedValuesOf (older Safari) just gets an empty
// list — the search-select then shows "No matching timezone" for everything,
// rather than erroring. What actually rejects an unrecognized zone name is
// the server: every schema with a timezone field validates it against the
// same IANA database this list comes from (see app/schemas.py's
// _validate_timezone).
let _tzNamesCache = null;
export function timezoneNames() {
  if (_tzNamesCache) return _tzNamesCache;
  try {
    const names = Intl.supportedValuesOf('timeZone');
    // "UTC" itself is a valid IANA zone and every server-side default here
    // is "UTC" — but it is missing from supportedValuesOf's own list (it
    // only returns region/city-style names, e.g. "Etc/UTC"). Put the name
    // people actually expect to type back in, first, rather than making the
    // by-far-most-common choice the one this list fails to suggest.
    _tzNamesCache = names.includes('UTC') ? names : ['UTC', ...names];
  } catch {
    _tzNamesCache = [];
  }
  return _tzNamesCache;
}

/** Wires every field() search-select (currently: the timezone fields) — one
 * delegated listener set for the whole document, so it works for any of them
 * any page ever renders, present or future, with nothing per-page to call.
 *
 * Each is a text box that filters a dropdown list as you type, backed by a
 * hidden input that holds the real value read by readForm(). Click, Enter,
 * or arrow-then-Enter picks an option; Escape or a blur that lands on
 * anything other than a real option reverts to the last value actually
 * picked — this is a dropdown you can search, not a free-text field with
 * suggestions, so it never leaves a half-typed filter sitting in the form. */
export function wireSearchSelects() {
  const isInput = (event) => event.target.classList?.contains('ss-input');
  const boxOf = (event) => event.target.closest?.('[data-search-select]');
  const listOf = (box) => box.querySelector('.picker-list');
  const hiddenOf = (box) => box.querySelector('input[type=hidden]');
  // Timezones by default; a box with data-items searches its own list instead.
  const itemsOf = (box) => {
    if (box.dataset.items) {
      if (!box._items) {
        try { box._items = JSON.parse(box.dataset.items); } catch { box._items = []; }
      }
      return box._items;
    }
    return timezoneNames().map((z) => ({ value: z, label: z }));
  };
  const labelFor = (box, value) => itemsOf(box).find((i) => i.value === value)?.label ?? value;

  const open = (box, query) => {
    const q = query.trim().toLowerCase();
    const all = itemsOf(box);
    const matches = (q ? all.filter((i) => `${i.label} ${i.hint || ''} ${i.value}`.toLowerCase().includes(q)) : all).slice(0, 200);
    const list = listOf(box);
    list.innerHTML = matches.length
      ? matches.map((i) => `<li role="option" data-value="${esc(i.value)}">${esc(i.label)}${
          i.hint ? `<span class="picker-hint">${esc(i.hint)}</span>` : ''}</li>`).join('')
      : `<li class="picker-note">${esc(box.dataset.empty || 'No Matching Timezone')}</li>`;
    list.classList.remove('hidden');
    box.querySelector('.ss-input').setAttribute('aria-expanded', 'true');
  };
  const close = (box) => {
    const list = listOf(box);
    list.classList.add('hidden');
    list.innerHTML = '';
    box.querySelector('.ss-input').setAttribute('aria-expanded', 'false');
  };
  const move = (list, delta) => {
    const items = [...list.querySelectorAll('li[data-value]')];
    if (!items.length) return;
    const from = items.findIndex((li) => li.classList.contains('active'));
    items.forEach((li) => li.classList.remove('active'));
    const next = items[(from + delta + items.length) % items.length];
    next.classList.add('active');
    next.scrollIntoView({ block: 'nearest' });
  };
  const commit = (box, value) => {
    const input = box.querySelector('.ss-input');
    const hidden = hiddenOf(box);
    input.value = labelFor(box, value);
    if (hidden.value !== value) {
      hidden.value = value;
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }
    close(box);
  };

  document.addEventListener('input', (event) => {
    if (!isInput(event)) return;
    open(boxOf(event), event.target.value);
  });
  document.addEventListener('focusin', (event) => {
    if (!isInput(event)) return;
    // The box usually already holds a real value (the tenant's current
    // timezone, a sensible default) — filtering by that on focus would
    // collapse the list to just the one match already sitting there.
    // Show everything to browse, and select it so typing replaces rather
    // than appends.
    open(boxOf(event), '');
    event.target.select();
  });
  document.addEventListener('focusout', (event) => {
    if (!isInput(event)) return;
    const box = boxOf(event);
    // A mousedown on a list item fires before this box loses focus to it —
    // give that a beat to land before deciding nothing was picked.
    setTimeout(() => {
      if (box.contains(document.activeElement)) return;
      const input = box.querySelector('.ss-input');
      const hidden = hiddenOf(box);
      const typed = input.value.trim().toLowerCase();
      const exact = itemsOf(box).find((i) => i.label.toLowerCase() === typed || i.value.toLowerCase() === typed);
      if (exact) commit(box, exact.value);
      else { input.value = hidden.value ? labelFor(box, hidden.value) : ''; close(box); }
    }, 150);
  });
  document.addEventListener('keydown', (event) => {
    if (!isInput(event)) return;
    const box = boxOf(event);
    const list = listOf(box);
    if (event.key === 'Escape') {
      // Closing the list is all Esc should do here — not also a dialog behind it.
      if (!list.classList.contains('hidden')) { event.preventDefault(); event.stopPropagation(); }
      event.target.value = hiddenOf(box).value ? labelFor(box, hiddenOf(box).value) : '';
      close(box);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const chosen = list.querySelector('li.active[data-value]') || list.querySelector('li[data-value]');
      if (chosen) commit(box, chosen.dataset.value);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.classList.contains('hidden')) open(box, event.target.value);
      else move(list, event.key === 'ArrowDown' ? 1 : -1);
    }
  });
  document.addEventListener('mousedown', (event) => {
    const li = event.target.closest?.('.search-select .picker-list li[data-value]');
    if (!li) return;
    event.preventDefault();   // keep focus in the text box rather than blurring first
    commit(li.closest('[data-search-select]'), li.dataset.value);
  });
}

/**
 * A grid of selectable plan cards — the richer alternative to field()'s
 * plain <select> for a choice worth seeing laid out: each plan's price,
 * one-line description and enforced limits, side by side, instead of
 * buried in an option label.
 *
 * Renders one native radio input per card (all sharing `name`), so it reads
 * back through readForm() exactly like any other field, degrades to a
 * normal (if unstyled) set of radio buttons with no JS at all, and needs a
 * script only to repaint the `.selected` highlight on change — wiring left
 * to the caller since that also differs by page (see pages/auth.js).
 */
export function planCards({
  id, name, label, plans, value, required, help, tags = {}, showRecommended = true,
}) {
  const cards = plans.map((p) => {
    const price = p.monthly_price_cents != null
      ? `$${(p.monthly_price_cents / 100).toFixed(0)}/mo`
      : 'Custom Pricing';
    const employees = p.max_employees != null
      ? `Up to ${p.max_employees} employee${p.max_employees === 1 ? '' : 's'}`
      : 'Unlimited Employees';
    const devices = p.max_devices != null
      ? `Up to ${p.max_devices} device${p.max_devices === 1 ? '' : 's'}`
      : 'Unlimited Devices';
    const speed = p.min_sync_interval_minutes != null
      ? `Syncs as often as every ${p.min_sync_interval_minutes} min`
      : 'No Sync-Speed Limit';
    const checked = p.id === value;
    return `
      <label class="plan-card${checked ? ' selected' : ''}">
        <input type="radio" name="${esc(name)}" value="${esc(p.id)}"
          ${checked ? 'checked' : ''}${required ? ' required' : ''}>
        <div class="plan-card-body">
          <div class="plan-card-head">
            <span class="plan-card-name">${esc(p.name)}</span>
            ${tags[p.id] ? `<span class="pill ${esc(tags[p.id].tone || 'ok')}">${esc(tags[p.id].label)}</span>` : ''}
            ${showRecommended && p.is_default && !tags[p.id] ? '<span class="pill ok">Recommended</span>' : ''}
          </div>
          <div class="plan-card-price">${esc(price)}</div>
          ${p.description ? `<div class="plan-card-desc">${esc(p.description)}</div>` : ''}
          <ul class="plan-card-features">
            <li>${esc(employees)}</li>
            <li>${esc(devices)}</li>
            <li>${esc(speed)}</li>
          </ul>
        </div>
      </label>`;
  }).join('');
  return `
    <div class="field"${id ? ` id="${esc(id)}"` : ''}>
      <label>${esc(titleCase(label))}${required ? '' : ' <span class="opt">optional</span>'}</label>
      <div class="plan-grid" role="radiogroup" aria-label="${esc(titleCase(label))}">${cards}</div>
      ${help ? `<div class="help">${esc(help)}</div>` : ''}
    </div>`;
}

/**
 * A full pricing-style grid — one big card per plan, price out front, a
 * checklist under "Includes", one button per card. The richer alternative to
 * planCards() for a screen whose whole job is comparing and picking a plan
 * (the pricing page ahead of signup, "Choose a plan" from Settings), rather
 * than one field within a longer form.
 *
 * Unlike planCards() this renders plain buttons, not radios — there is
 * nothing else on these screens for a plan to be one field among, and the
 * two contexts that use it want different button behaviour (carry the pick
 * back to signup; PATCH the tenant immediately), which only the caller
 * knows. wirePricingCards() below wires whichever one it is.
 */
export function pricingCards({ plans, tags = {}, showRecommended = true, ctaLabel = 'Get Started' }) {
  const cards = plans.map((p) => {
    const employees = p.max_employees != null
      ? `Up to ${p.max_employees} employee${p.max_employees === 1 ? '' : 's'}`
      : 'Unlimited Employees';
    const devices = p.max_devices != null
      ? `Up to ${p.max_devices} device${p.max_devices === 1 ? '' : 's'}`
      : 'Unlimited Devices';
    const speed = p.min_sync_interval_minutes != null
      ? `Syncs as often as every ${p.min_sync_interval_minutes} min`
      : 'No Sync-Speed Limit';
    const tag = tags[p.id] || (showRecommended && p.is_default ? { label: 'Recommended', tone: 'ok' } : null);
    // 'current' (this plan, today) and 'warn' (a switch to it is already
    // queued) both default to locked — the button has nothing left to do —
    // only 'ok' (Recommended) is a plain badge that leaves the card pickable.
    // A caller can override either way with tag.locked (see the "cancel a
    // scheduled switch by picking the current plan again" case in
    // settings.js, where a 'current' tag stays clickable).
    const locked = tag?.locked ?? (tag?.tone === 'current' || tag?.tone === 'warn');
    const label = locked ? tag.label : (tag?.ctaLabel || ctaLabel);
    return `
      <div class="pricing-card${tag ? ' tagged' : ''}${locked ? ' locked' : ''}">
        ${tag ? `<span class="pricing-tag ${esc(tag.tone || 'ok')}">${esc(tag.label)}</span>` : ''}
        <div class="pricing-card-name">${esc(p.name)}</div>
        <div class="pricing-card-price">${p.monthly_price_cents != null
          ? `<span class="amt">$${(p.monthly_price_cents / 100).toFixed(0)}</span><span class="per">/mo</span>`
          : '<span class="amt custom">Custom Pricing</span>'}</div>
        <p class="pricing-card-desc">${esc(p.description || '')}</p>
        <button type="button" class="pricing-cta"${locked ? ' disabled' : ''} data-pick="${esc(p.id)}">
          ${esc(label)}
        </button>
        <div class="pricing-includes">Includes</div>
        <ul class="pricing-features">
          <li>${esc(employees)}</li>
          <li>${esc(devices)}</li>
          <li>${esc(speed)}</li>
        </ul>
      </div>`;
  }).join('');
  return `<div class="pricing-grid">${cards}</div>`;
}

/** Wires a pricingCards() grid's buttons to one callback — onPick(planId) —
 * so the caller (signup vs. Settings) supplies the one thing that differs. */
export function wirePricingCards(root, onPick) {
  $$('.pricing-cta[data-pick]', root).forEach((button) => {
    button.addEventListener('click', () => onPick(button.dataset.pick, button));
  });
}

/** Read every [name] control under a root into a plain object. */
export function readForm(root) {
  const out = {};
  $$('[name]', root).forEach((el) => {
    // A radio group shares one [name] across several elements — only the
    // checked one carries the group's value. Without this, whichever radio
    // happens to be last in the DOM would silently win over the one the
    // person actually picked.
    if (el.type === 'radio') {
      if (el.checked) out[el.name] = el.value;
      return;
    }
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else if (el.dataset.bool) out[el.name] = el.value === 'true';
    else out[el.name] = el.value;
  });
  return out;
}

export function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), 5200);
}

/** Run an async action, surfacing failures as a toast instead of a dead click. */
export async function guard(fn, successMessage) {
  try {
    const result = await fn();
    if (successMessage) toast(successMessage, 'ok');
    return result;
  } catch (error) {
    if (error.status !== 401) toast(error.message || 'Something Went Wrong', 'bad');
    return undefined;
  }
}

/** Disable a button while its action runs, so it cannot be double-fired. */
export async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Working…';
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/** Hover / focus labels for anything with ``data-tip``.
 *
 * One floating box, positioned in the viewport, so a label inside a scrolling
 * table is never cut off by the table's edge. Keyboard focus shows it too. */
export function wireTips() {
  const box = document.createElement('div');
  box.id = 'tip';
  box.setAttribute('role', 'tooltip');
  document.body.append(box);
  const show = (el) => {
    // An open modal <dialog> sits in the browser's top layer, above every
    // z-index on the page — so inside one, the label has to live in that
    // dialog too or it draws behind it. Still position:fixed, so the
    // dialog's own overflow never clips it.
    const host = el.closest('dialog[open]') || document.body;
    if (box.parentNode !== host) host.append(box);
    box.textContent = el.dataset.tip;
    box.classList.add('on');
    const r = el.getBoundingClientRect();
    const w = box.offsetWidth;
    const h = box.offsetHeight;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const top = r.top - h - 8 >= 8 ? r.top - h - 8 : r.bottom + 8;
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  };
  const hide = () => box.classList.remove('on');
  const find = (event) => event.target.closest?.('[data-tip]');
  document.addEventListener('mouseover', (event) => { const el = find(event); if (el) show(el); });
  document.addEventListener('mouseout', (event) => { if (find(event)) hide(); });
  document.addEventListener('focusin', (event) => { const el = find(event); if (el) show(el); });
  document.addEventListener('focusout', hide);
  window.addEventListener('scroll', hide, true);
}
