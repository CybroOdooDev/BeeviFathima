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

export function fmtAgo(value) {
  if (!value) return 'never';
  const then = new Date(/Z|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z');
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (Number.isNaN(seconds)) return '—';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
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
  if (seconds < 3600) return `in ${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `in ${Math.round(seconds / 3600)} h`;
  return `in ${Math.round(seconds / 86400)} d`;
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
export const stat = ({ label, value, note, tone: t }) => `
  <div class="stat">
    <div class="k">${esc(label)}</div>
    <div class="v ${t || ''}">${esc(value)}</div>
    ${note ? `<div class="note">${esc(note)}</div>` : ''}
  </div>`;

export const empty = (title, body) => `
  <div class="empty"><strong>${esc(title)}</strong>${body ? esc(body) : ''}</div>`;

export const banner = (title, body, kind = '') => `
  <div class="banner ${kind}"><strong>${esc(title)}</strong>${body ? esc(body) : ''}</div>`;

export const loading = () => '<div class="skeleton">Loading…</div>';

export function field({ name, label, type = 'text', value = '', help, required, placeholder, options, strongHelp, boolean }) {
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
    : `<input type="${esc(type)}" name="${esc(name)}" id="${esc(name)}"
         value="${esc(value)}" ${required ? 'required' : ''}
         ${placeholder ? `placeholder="${esc(placeholder)}"` : ''}>`;
  return `
    <div class="field">
      <label for="${esc(name)}">${esc(label)}${required ? '' : ' <span class="opt">optional</span>'}</label>
      ${control}
      ${help ? `<div class="help ${strongHelp ? 'strong' : ''}">${esc(help)}</div>` : ''}
    </div>`;
}

/** Read every [name] control under a root into a plain object. */
export function readForm(root) {
  const out = {};
  $$('[name]', root).forEach((el) => {
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
    if (error.status !== 401) toast(error.message || 'Something went wrong', 'bad');
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
