/* Staff console → Leads: everyone who used the website's Contact / Book a demo
 * form, worked as a pipeline. The request is already in the database — this is
 * where it moves through New → Contacted → Qualified → Demo → Won / Lost, as a
 * board (drag a card to a column) or as a list. Every move is written to the
 * lead's history, which is the timeline in the lead's wizard. */

import { api, auth } from '../api.js';
import { banner, empty, esc, fmtAgo, guard, loading, pill, stat } from '../ui.js';

const STAGES = [
  ['new', 'New'], ['contacted', 'Contacted'],
  ['qualified', 'Qualified'], ['won', 'Won'], ['lost', 'Lost'],
];
const LABEL = { ...Object.fromEntries(STAGES), demo: 'Demo' };   // 'demo' only for old history entries
// The demo sub-stage of Contacted: where a demo the lead asked for has got to.
const DEMOS = [['pending', 'Pending'], ['scheduled', 'Scheduled'], ['completed', 'Completed']];
const DEMO_LABEL = Object.fromEntries(DEMOS);
const DEMO_TONE = { pending: 'pending', scheduled: 'trialing', completed: 'success' };
const demoPill = (r) => (r.demo_status
  ? `<span class="pill demo-pill ${esc(r.demo_status)}">Demo ${esc(DEMO_LABEL[r.demo_status]).toLowerCase()}</span>` : '');
const wantsDemo = (r) => r.topic === 'Demo' || !!r.preferred_date || !!r.demo_status;
const TONE = { new: 'pending', contacted: 'trialing', qualified: 'trialing', won: 'success', lost: 'skipped' };

const VIEW_KEY = 'bb.leads.view';
const getView = () => {
  try { return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'board'; } catch { return 'board'; }
};
const setView = (v) => { try { localStorage.setItem(VIEW_KEY, v); } catch { /* a convenience only */ } };

const inStage = (r) => r.stage_changed_at || r.created_at;

function setup(r) {
  const parts = [
    r.odoo_version && `Odoo ${r.odoo_version}`, r.odoo_hosting, r.biometric_system, r.device_setup,
    r.employees && `${r.employees} employees`,
  ].filter(Boolean);
  return parts.map(esc).join(' · ');
}

function demoWhen(r) {
  if (!r.preferred_date) return '';
  return `${esc(r.preferred_date)}${r.preferred_window ? ` · ${esc(r.preferred_window.toLowerCase())}` : ''}`
    + `${r.timezone ? ` <span class="hint">(${esc(r.timezone)})</span>` : ''}`;
}

/* ---- the summary strip ---------------------------------------------- */
function summaryHtml(p) {
  const rate = p.conversion == null ? '—' : `${Math.round(p.conversion * 100)}%`;
  return `
    <div class="grid cols-4" style="margin-bottom:16px">
      ${stat({ label: 'Open Leads', value: p.open, note: 'still in play' })}
      ${stat({ label: 'Won', value: p.won, note: `${p.lost} lost` })}
      ${stat({ label: 'Conversion', value: rate, note: 'won ÷ (won + lost)' })}
      ${stat({ label: 'Days To Win', value: p.avg_days_to_win == null ? '—' : p.avg_days_to_win, note: 'average, new → won' })}
    </div>`;
}

/* ---- board ------------------------------------------------------------ */
function boardCard(r) {
  return `
    <div class="board-card" draggable="true" data-id="${esc(r.id)}" tabindex="0" role="button"
         aria-label="${esc(r.name)}, ${esc(r.company)}">
      <strong class="bc-name">${esc(r.name)}</strong>
      <div class="hint bc-co">${esc(r.company)}</div>
      <div class="bc-topic"><span class="pill mute">${esc(r.topic)}</span>${r.demo_status ? ` ${demoPill(r)}` : ''}</div>
      ${r.preferred_date ? `<div class="bc-demo">Demo: ${demoWhen(r)}</div>` : ''}
      <div class="hint bc-age" title="${esc(inStage(r))}">${esc(fmtAgo(inStage(r)))} in ${esc(LABEL[r.status] || r.status)}</div>
    </div>`;
}

function boardHtml(rows) {
  return `<div class="board">${STAGES.map(([key, label]) => {
    const here = rows.filter((r) => r.status === key);
    return `
      <section class="board-col" data-stage="${key}" aria-label="${esc(label)}">
        <header><span class="bc-title">${esc(label)}</span><span class="count">${here.length}</span></header>
        <div class="board-drop">${here.map(boardCard).join('')}</div>
      </section>`;
  }).join('')}</div>`;
}

/* ---- list (the earlier view, with the new stages) --------------------- */
function listCard(r) {
  const when = demoWhen(r);
  return `
    <div class="card lead" data-id="${esc(r.id)}" tabindex="0" role="button"
         aria-label="Open ${esc(r.name)}, ${esc(r.company)}" style="margin-bottom:12px">
      <div class="row" style="justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
        <div>
          <strong>${esc(r.name)}</strong> <span class="hint">· ${esc(r.company)}</span>
          <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}
            · <span title="${esc(r.created_at)}">${esc(fmtAgo(r.created_at))}</span></div>
        </div>
        <div>${pill(TONE[r.status] || 'mute', LABEL[r.status] || r.status)}
          ${demoPill(r)} <span class="pill mute">${esc(r.topic)}</span></div>
      </div>
      ${when ? `<div style="margin-top:8px"><span class="hint">Wants a demo:</span> <strong>${when}</strong></div>` : ''}
      ${setup(r) ? `<div style="margin-top:8px"><span class="hint">Setup:</span> ${setup(r)}</div>` : ''}
      ${r.message ? `<p style="white-space:pre-wrap;margin:8px 0 0">${esc(r.message)}</p>` : ''}
      ${r.status === 'lost' && r.lost_reason ? `<div class="hint" style="margin-top:6px">Lost: ${esc(r.lost_reason)}</div>` : ''}
      ${r.handled_by ? `<div class="hint" style="margin-top:10px">Last updated by ${esc(r.handled_by)}</div>` : ''}
    </div>`;
}

/* ---- the side panel ---------------------------------------------------- */
function eventLine(e) {
  const who = esc(e.actor || 'someone');
  const when = `<span class="hint" title="${esc(e.created_at)}">${esc(fmtAgo(e.created_at))}</span>`;
  if (e.kind === 'demo') {
    const text = e.to_stage
      ? `Demo ${e.from_stage ? `${esc(DEMO_LABEL[e.from_stage] || e.from_stage).toLowerCase()} → ` : 'set to '}<strong>${esc(DEMO_LABEL[e.to_stage] || e.to_stage).toLowerCase()}</strong>`
      : 'Demo Status Cleared';
    return `<li><div>${text}</div><div class="hint">${who} · ${when}</div></li>`;
  }
  if (e.kind === 'note') return `<li><div>${esc(e.note)}</div><div class="hint">${who} · ${when}</div></li>`;
  const text = e.from_stage
    ? `Moved ${esc(LABEL[e.from_stage] || e.from_stage)} → <strong>${esc(LABEL[e.to_stage] || e.to_stage)}</strong>`
    : `Came in as <strong>${esc(LABEL[e.to_stage] || e.to_stage)}</strong>`;
  return `<li><div>${text}</div><div class="hint">${who} · ${when}</div></li>`;
}

const FLOW = ['new', 'contacted', 'qualified', 'won'];

/* The status bar: the five steps of the pipeline, the current one lit and the
 * ones before it ticked, with Lost off to the side as the other way out. */
function statusBar(status) {
  const at = FLOW.indexOf(status);
  const step = (key, cls) => `
    <button type="button" class="wiz-step ${cls}" data-stage="${key}"
      aria-current="${key === status ? 'step' : 'false'}" title="Move to ${esc(LABEL[key])}">
      <span class="wdot">${cls === 'done' ? '&#10003;' : ''}</span><span class="lbl">${esc(LABEL[key])}</span>
    </button>`;
  return `<div class="wiz-bar" role="group" aria-label="Lead status">
    ${FLOW.map((k, i) => step(k, status === 'lost' ? '' : (i < at ? 'done' : i === at ? 'current' : ''))).join('<span class="wiz-line"></span>')}
    <span class="wiz-gap"></span>
    ${step('lost', status === 'lost' ? 'current lost' : 'lost')}
  </div>`;
}

/* The demo sub-stage. Shown once the lead has been contacted, and only for a
 * lead that asked for a demo (or already has a demo status). Clicking the
 * active choice clears it. */
function demoRow(r) {
  if (r.status === 'new' || !wantsDemo(r)) return '';
  return `<span class="wiz-demo-label">Demo</span>
    <div class="seg" role="group" aria-label="Demo status">${DEMOS.map(([v, l]) => `
      <button type="button" class="seg-btn ${v} ${r.demo_status === v ? 'on' : ''}" data-demo="${v}"
        aria-pressed="${r.demo_status === v}">${esc(l)}</button>`).join('')}</div>`;
}

function panelHtml(r, events) {
  return `
    <div class="wiz-backdrop" data-close></div>
    <div class="wiz" role="dialog" aria-modal="true" aria-label="Lead: ${esc(r.name)}">
      <header>
        <div>
          <h3>${esc(r.name)} <span class="hint">· ${esc(r.company)}</span></h3>
          <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}
            · came in ${esc(fmtAgo(r.created_at))}</div>
        </div>
        <button class="sm" data-close aria-label="Close">Close</button>
      </header>
      <div class="wiz-status" id="wStatus">${statusBar(r.status)}</div>
      <div class="wiz-demo" id="wDemo">${demoRow(r)}</div>
      <div class="wiz-body">
        <div class="wiz-cols">
          <section>
            <h4>Request</h4>
            <div class="wiz-facts">
              <div><span class="hint">Topic</span><span class="pill mute">${esc(r.topic)}</span></div>
              ${r.preferred_date ? `<div><span class="hint">Wants A Demo</span><strong>${demoWhen(r)}</strong></div>` : ''}
              ${setup(r) ? `<div><span class="hint">Setup</span><span>${setup(r)}</span></div>` : ''}
            </div>
            ${r.message ? `<p class="wiz-msg">${esc(r.message)}</p>` : ''}
            <div id="wLostWrap" class="${r.status === 'lost' ? '' : 'hidden'}" style="margin-top:12px">
              <label for="wLost">Why Was It Lost? <span class="opt">optional</span></label>
              <input id="wLost" type="text" maxlength="160" value="${esc(r.lost_reason || '')}"
                placeholder="Too expensive, chose a competitor, no response…">
            </div>
            <label for="wNotes" style="margin-top:12px">Notes</label>
            <textarea id="wNotes" rows="4" placeholder="What matters about this lead…">${esc(r.notes || '')}</textarea>
            <div class="row" style="margin-top:10px"><button class="primary" id="wSave">Save Notes</button></div>
          </section>
          <section>
            <h4>History</h4>
            <div class="row" style="gap:8px;margin-bottom:10px">
              <input id="wNote" type="text" maxlength="4000" placeholder="Add to the history — “called, voicemail”" style="flex:1">
              <button class="sm" id="wAddNote">Add</button>
            </div>
            <ul class="timeline" id="wTimeline">${events.map(eventLine).join('')}</ul>
          </section>
        </div>
      </div>
    </div>`;
}

/* ---- list filters ------------------------------------------------------------
 * Kept at module level so they survive a redraw (saving a lead refreshes the
 * page). Empty = no filter on that field, so everything shows. */
const FILTERS = { stage: new Set(), demo: new Set() };
let filterOpen = false;
const DEMO_FILTERS = [...DEMOS, ['none', 'No Demo']];
const activeFilters = () => FILTERS.stage.size + FILTERS.demo.size;
const matches = (r) =>
  (!FILTERS.stage.size || FILTERS.stage.has(r.status))
  && (!FILTERS.demo.size || FILTERS.demo.has(r.demo_status || 'none'));
const FUNNEL = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 5h18l-7 8v6l-4-2v-4z"/></svg>';

function filterMenu() {
  const group = (title, key, options) => `
    <fieldset class="flt-group"><legend>${esc(title)}</legend>
      ${options.map(([v, l]) => `<label class="flt-opt"><input type="checkbox" data-flt="${key}" value="${esc(v)}"
        ${FILTERS[key].has(v) ? 'checked' : ''}> <span>${esc(l)}</span></label>`).join('')}
    </fieldset>`;
  return `
    ${group('Stage', 'stage', STAGES)}
    ${group('Demo', 'demo', DEMO_FILTERS)}
    <div class="flt-foot"><button type="button" class="sm" data-flt-clear ${activeFilters() ? '' : 'disabled'}>Clear All Filters</button></div>`;
}

/* ---- the page ------------------------------------------------------------ */
export async function render(mount) {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not Available', 'This section is for platform staff.', 'warn');
    return;
  }
  const view = getView();
  mount.innerHTML = loading();
  const [pipeline, rows] = await Promise.all([
    api.get('/admin/contact-requests/pipeline'),
    api.get('/admin/contact-requests'),
  ]);
  const byId = new Map(rows.map((r) => [r.id, r]));

  mount.innerHTML = `
    ${summaryHtml(pipeline)}
    <div class="row" style="margin-bottom:14px;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="sm ${view === 'board' ? 'primary' : ''}" data-view="board">Board</button>
      <button class="sm ${view === 'list' ? 'primary' : ''}" data-view="list">List</button>
      ${view === 'list' ? `<span class="flt-wrap">
        <button class="sm flt-btn ${activeFilters() ? 'on' : ''}" id="fltBtn" aria-haspopup="true"
          aria-expanded="${filterOpen}" title="Filters">${FUNNEL}<span>Filters</span>
          ${activeFilters() ? `<span class="flt-count">${activeFilters()}</span>` : ''}</button>
        <div class="flt-menu ${filterOpen ? '' : 'hidden'}" id="fltMenu">${filterMenu()}</div></span>` : ''}
    </div>
    ${view === 'board' ? boardHtml(rows) : '<div id="listBody"></div>'}
    <div id="leadPanel"></div>`;

  let refreshBehind = () => {};
  const redraw = () => render(mount);
  mount.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => { setView(b.dataset.view); redraw(); }));

  /* the wizard */
  const openPanel = async (id, { focusLost = false } = {}) => {
    const lead = byId.get(id);
    if (!lead) return;
    const events = await api.get(`/admin/contact-requests/${id}/events`);
    const host = mount.querySelector('#leadPanel');
    host.innerHTML = panelHtml(lead, events);
    let changed = false;
    const close = () => {
      document.removeEventListener('keydown', onKey);
      host.innerHTML = '';
      if (changed) redraw();
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    host.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', close));
    const lostWrap = host.querySelector('#wLostWrap');
    const refreshHistory = async () => {
      const fresh = await api.get(`/admin/contact-requests/${id}/events`);
      host.querySelector('#wTimeline').innerHTML = fresh.map(eventLine).join('');
    };
    const paintDemo = () => {
      const box = host.querySelector('#wDemo');
      box.innerHTML = demoRow(lead);
      box.querySelectorAll('[data-demo]').forEach((b) => b.addEventListener('click', () => {
        const to = b.dataset.demo === lead.demo_status ? null : b.dataset.demo;
        guard(async () => {
          const saved = await api.patch(`/admin/contact-requests/${id}`, { demo_status: to });
          // An older server ignores the field and answers without it.
          if (!('demo_status' in saved)) {
            throw new Error('The server did not save the demo status — run the database upgrade and restart it.');
          }
          Object.assign(lead, saved);
          changed = true;
          paintDemo();
          refreshBehind();
          await refreshHistory();
        }, to ? `Demo ${DEMO_LABEL[to].toLowerCase()}` : 'Demo Status Cleared');
      }));
    };
    const bindBar = () => host.querySelectorAll('.wiz-step').forEach((b) =>
      b.addEventListener('click', () => {
        const to = b.dataset.stage;
        if (to === lead.status) return;
        guard(async () => {
          const saved = await api.patch(`/admin/contact-requests/${id}`, { status: to });
          Object.assign(lead, saved && saved.id ? saved : { status: to });
          changed = true;
          host.querySelector('#wStatus').innerHTML = statusBar(lead.status);
          bindBar();
          paintDemo();
          lostWrap.classList.toggle('hidden', lead.status !== 'lost');
          refreshBehind();
          if (lead.status !== 'lost') host.querySelector('#wLost').value = '';
          else host.querySelector('#wLost').focus();
          await refreshHistory();
        }, `Moved to ${LABEL[to]}`);
      }));
    bindBar();
    paintDemo();
    host.querySelector('#wSave').addEventListener('click', () => guard(async () => {
      const saved = await api.patch(`/admin/contact-requests/${id}`, {
        status: lead.status,
        notes: host.querySelector('#wNotes').value,
        lost_reason: host.querySelector('#wLost').value,
      });
      Object.assign(lead, saved && saved.id ? saved : {});
      changed = true;
      refreshBehind();
    }, 'Saved'));
    const noteInput = host.querySelector('#wNote');
    const addNote = () => guard(async () => {
      const text = noteInput.value.trim();
      if (!text) return;
      await api.post(`/admin/contact-requests/${id}/events`, { note: text });
      noteInput.value = '';
      await refreshHistory();
    });
    host.querySelector('#wAddNote').addEventListener('click', addNote);
    noteInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addNote(); } });
    if (focusLost) host.querySelector('#wLost')?.focus();
  };

  /* the list, filtered in the browser — no refetch as filters change */
  const paintList = () => {
    const host = mount.querySelector('#listBody');
    if (!host) return;
    const shown = rows.filter(matches);
    host.innerHTML = shown.length ? shown.map(listCard).join('')
      : `<div class="card">${activeFilters()
        ? empty('No Leads Match These Filters', 'Try removing a filter.')
        : empty('No Leads Yet', 'Requests from the website’s Contact / Book a demo form appear here.')}
        ${activeFilters() ? '<div style="text-align:center;margin-top:8px"><button class="sm" data-flt-clear>Clear All Filters</button></div>' : ''}</div>`;
    host.querySelectorAll('.lead').forEach((card) => {
      // The e-mail link inside a row still opens the mail client, not the wizard.
      card.addEventListener('click', (e) => { if (!e.target.closest('a')) openPanel(card.dataset.id); });
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target === card) openPanel(card.dataset.id);
      });
    });
    host.querySelectorAll('[data-flt-clear]').forEach((b) => b.addEventListener('click', clearFilters));
  };
  const syncMenu = () => {
    const menu = mount.querySelector('#fltMenu');
    const btn = mount.querySelector('#fltBtn');
    if (!menu) return;
    menu.innerHTML = filterMenu();
    bindMenu();
    btn.classList.toggle('on', activeFilters() > 0);
    btn.querySelector('.flt-count')?.remove();
    if (activeFilters()) btn.insertAdjacentHTML('beforeend', `<span class="flt-count">${activeFilters()}</span>`);
  };
  const clearFilters = () => { FILTERS.stage.clear(); FILTERS.demo.clear(); syncMenu(); paintList(); };
  const bindMenu = () => {
    const menu = mount.querySelector('#fltMenu');
    menu.querySelectorAll('[data-flt]').forEach((box) => box.addEventListener('change', () => {
      const set = FILTERS[box.dataset.flt];
      if (box.checked) set.add(box.value); else set.delete(box.value);
      syncMenu(); paintList();
    }));
    menu.querySelector('[data-flt-clear]')?.addEventListener('click', clearFilters);
  };
  if (view === 'list') {
    const btn = mount.querySelector('#fltBtn');
    const menu = mount.querySelector('#fltMenu');
    bindMenu();
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      filterOpen = !filterOpen;
      menu.classList.toggle('hidden', !filterOpen);
      btn.setAttribute('aria-expanded', String(filterOpen));
    });
    // Click outside (or Esc) closes the menu; the filters themselves stay applied.
    const away = (e) => {
      if (!document.body.contains(btn)) { document.removeEventListener('click', away); return; }
      if (filterOpen && !menu.contains(e.target)) {
        filterOpen = false; menu.classList.add('hidden'); btn.setAttribute('aria-expanded', 'false');
      }
    };
    document.addEventListener('click', away);
    paintList();
  }

  /* the board: click to open, drag to move */
  const bindBoard = () => {
  mount.querySelectorAll('.board-card').forEach((card) => {
    card.addEventListener('click', () => openPanel(card.dataset.id));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter') openPanel(card.dataset.id); });
    card.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', card.dataset.id);
      e.dataTransfer.effectAllowed = 'move';
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
  });
  mount.querySelectorAll('.board-col').forEach((col) => {
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
    col.addEventListener('dragleave', (e) => {
      if (!col.contains(e.relatedTarget)) col.classList.remove('over');
    });
    col.addEventListener('drop', (e) => {
      e.preventDefault();
      col.classList.remove('over');
      const id = e.dataTransfer.getData('text/plain');
      const lead = byId.get(id);
      const to = col.dataset.stage;
      if (!lead || lead.status === to) return;
      // Let the browser finish the drag (dragend fires on the source card)
      // before the board is rebuilt underneath it.
      setTimeout(() => guard(async () => {
        await api.patch(`/admin/contact-requests/${id}`, { status: to });
        await redraw();
        // Dropping on Lost is the moment the reason is known — ask for it.
        if (to === 'lost') {
          byId.get(id).status = 'lost';
          openPanel(id, { focusLost: true });
        }
      }), 0);
    });
  });
  };
  bindBoard();
  // Show a change on the board / list behind the wizard straight away.
  refreshBehind = () => {
    if (view === 'board') {
      const board = mount.querySelector('.board');
      if (board) { board.outerHTML = boardHtml(rows); bindBoard(); }
    } else paintList();
  };
}
