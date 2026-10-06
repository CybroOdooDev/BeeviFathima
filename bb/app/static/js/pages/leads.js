/* Staff console → Leads: everyone who used the website's Contact / Book a demo
 * form, worked as a pipeline. The request is already in the database — this is
 * where it moves through New → Contacted → Qualified → Demo → Won / Lost, as a
 * board (drag a card to a column) or as a list. Every move is written to the
 * lead's history, which is the timeline in the lead's wizard. */

import { api, auth } from '../api.js';
import { banner, empty, esc, fmtAgo, guard, loading, pill, stat } from '../ui.js';

const STAGES = [
  ['new', 'New'], ['contacted', 'Contacted'], ['demo', 'Demo'],
  ['qualified', 'Qualified'], ['won', 'Won'], ['lost', 'Lost'],
];
const LABEL = Object.fromEntries(STAGES);
const TONE = { new: 'pending', contacted: 'trialing', qualified: 'trialing', demo: 'trialing', won: 'success', lost: 'skipped' };

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
  return parts.length ? parts.map(esc).join(' · ') : '—';
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
      ${stat({ label: 'Open leads', value: p.open, note: 'still in play' })}
      ${stat({ label: 'Won', value: p.won, note: `${p.lost} lost` })}
      ${stat({ label: 'Conversion', value: rate, note: 'won ÷ (won + lost)' })}
      ${stat({ label: 'Days to win', value: p.avg_days_to_win == null ? '—' : p.avg_days_to_win, note: 'average, new → won' })}
    </div>`;
}

/* ---- board ------------------------------------------------------------ */
function boardCard(r) {
  return `
    <div class="board-card" draggable="true" data-id="${esc(r.id)}" tabindex="0" role="button"
         aria-label="${esc(r.name)}, ${esc(r.company)}">
      <strong class="bc-name">${esc(r.name)}</strong>
      <div class="hint bc-co">${esc(r.company)}</div>
      <div class="bc-topic"><span class="pill mute">${esc(r.topic)}</span>${r.status === 'demo' ? ' <span class="pill bc-demo-badge">In Demo</span>' : ''}</div>
      ${r.preferred_date ? `<div class="bc-demo">Demo: ${demoWhen(r)}</div>` : ''}
      <div class="hint bc-age" title="${esc(inStage(r))}">${esc(fmtAgo(inStage(r)))} in ${esc(LABEL[r.status === 'demo' ? 'contacted' : r.status] || r.status)}</div>
    </div>`;
}

/* Demo is a sub-stage of Contacted: it has no column. A lead that has reached
 * it stays in Contacted and carries a Demo badge on its card (see boardCard). */
const COLUMNS = [
  ['new', 'New', ['new']],
  ['contacted', 'Contacted', ['contacted', 'demo']],
  ['qualified', 'Qualified', ['qualified']],
  ['won', 'Won', ['won']],
  ['lost', 'Lost', ['lost']],
];

function boardHtml(rows) {
  return `<div class="board">${COLUMNS.map(([key, label, stages]) => {
    const here = rows.filter((r) => stages.includes(r.status));
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
    <div class="card lead" data-id="${esc(r.id)}" style="margin-bottom:12px">
      <div class="row" style="justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
        <div>
          <strong>${esc(r.name)}</strong> <span class="hint">· ${esc(r.company)}</span>
          <div class="hint"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · ${esc(r.phone)}` : ''}
            · <span title="${esc(r.created_at)}">${esc(fmtAgo(r.created_at))}</span></div>
        </div>
        <div>${pill(TONE[r.status] || 'mute', LABEL[r.status] || r.status)}
          <span class="pill mute">${esc(r.topic)}</span></div>
      </div>
      ${when ? `<div style="margin-top:8px"><span class="hint">Wants a demo:</span> <strong>${when}</strong></div>` : ''}
      <div style="margin-top:8px"><span class="hint">Setup:</span> ${setup(r)}</div>
      ${r.message ? `<p style="white-space:pre-wrap;margin:8px 0 0">${esc(r.message)}</p>` : ''}
      ${r.status === 'lost' && r.lost_reason ? `<div class="hint" style="margin-top:6px">Lost: ${esc(r.lost_reason)}</div>` : ''}
      <div class="row" style="margin-top:12px;gap:8px">
        <button class="sm" data-open>Open</button>
        ${r.handled_by ? `<span class="hint">Last updated by ${esc(r.handled_by)}</span>` : ''}
      </div>
    </div>`;
}

/* ---- the side panel ---------------------------------------------------- */
function eventLine(e) {
  const who = esc(e.actor || 'someone');
  const when = `<span class="hint" title="${esc(e.created_at)}">${esc(fmtAgo(e.created_at))}</span>`;
  if (e.kind === 'note') return `<li><div>${esc(e.note)}</div><div class="hint">${who} · ${when}</div></li>`;
  const text = e.from_stage
    ? `Moved ${esc(LABEL[e.from_stage] || e.from_stage)} → <strong>${esc(LABEL[e.to_stage] || e.to_stage)}</strong>`
    : `Came in as <strong>${esc(LABEL[e.to_stage] || e.to_stage)}</strong>`;
  return `<li><div>${text}</div><div class="hint">${who} · ${when}</div></li>`;
}

const FLOW = ['new', 'contacted', 'demo', 'qualified', 'won'];

/* The status bar: the five steps of the pipeline, the current one lit and the
 * ones before it ticked, with Lost off to the side as the other way out. */
function statusBar(status) {
  const at = FLOW.indexOf(status);
  const step = (key, cls) => `
    <button type="button" class="wiz-step ${key === 'demo' ? 'sub' : ''} ${cls}" data-stage="${key}"
      aria-current="${key === status ? 'step' : 'false'}" title="Move to ${esc(LABEL[key])}">
      <span class="wdot">${cls === 'done' ? '&#10003;' : ''}</span><span class="lbl">${esc(LABEL[key])}</span>
    </button>`;
  return `<div class="wiz-bar" role="group" aria-label="Lead status">
    ${FLOW.map((k, i) => step(k, status === 'lost' ? '' : (i < at ? 'done' : i === at ? 'current' : ''))).join('<span class="wiz-line"></span>')}
    <span class="wiz-gap"></span>
    ${step('lost', status === 'lost' ? 'current lost' : 'lost')}
  </div>`;
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
      <div class="wiz-body">
        <div class="wiz-cols">
          <section>
            <h4>Request</h4>
            <div class="wiz-facts">
              <div><span class="hint">Topic</span><span class="pill mute">${esc(r.topic)}</span></div>
              ${r.preferred_date ? `<div><span class="hint">Wants a demo</span><strong>${demoWhen(r)}</strong></div>` : ''}
              <div><span class="hint">Setup</span><span>${setup(r)}</span></div>
            </div>
            ${r.message ? `<p class="wiz-msg">${esc(r.message)}</p>` : ''}
            <div id="wLostWrap" class="${r.status === 'lost' ? '' : 'hidden'}" style="margin-top:12px">
              <label for="wLost">Why was it lost? <span class="opt">optional</span></label>
              <input id="wLost" type="text" maxlength="160" value="${esc(r.lost_reason || '')}"
                placeholder="Too expensive, chose a competitor, no response…">
            </div>
            <label for="wNotes" style="margin-top:12px">Notes</label>
            <textarea id="wNotes" rows="4" placeholder="What matters about this lead…">${esc(r.notes || '')}</textarea>
            <div class="row" style="margin-top:10px"><button class="primary" id="wSave">Save notes</button></div>
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

/* ---- the page ------------------------------------------------------------ */
export async function render(mount, filter = 'open') {
  if (!auth.isPlatformAdmin) {
    mount.innerHTML = banner('Not available', 'This section is for platform staff.', 'warn');
    return;
  }
  const view = getView();
  mount.innerHTML = loading();
  const [pipeline, rows] = await Promise.all([
    api.get('/admin/contact-requests/pipeline'),
    api.get(`/admin/contact-requests${view === 'list' && filter === 'open' ? '?status=open' : ''}`),
  ]);
  const byId = new Map(rows.map((r) => [r.id, r]));

  const body = view === 'board'
    ? boardHtml(rows)
    : (rows.length ? rows.map(listCard).join('')
      : `<div class="card">${empty('No leads here', filter === 'open'
        ? 'Everything has been dealt with. New website requests appear here.'
        : 'Requests from the website’s Contact / Book a demo form appear here.')}</div>`);

  mount.innerHTML = `
    ${summaryHtml(pipeline)}
    <div class="row" style="margin-bottom:14px;gap:8px;flex-wrap:wrap">
      <button class="sm ${view === 'board' ? 'primary' : ''}" data-view="board">Board</button>
      <button class="sm ${view === 'list' ? 'primary' : ''}" data-view="list">List</button>
      ${view === 'list' ? `<span style="width:12px"></span>
        <button class="sm ${filter === 'open' ? 'primary' : ''}" data-filter="open">Open</button>
        <button class="sm ${filter === 'all' ? 'primary' : ''}" data-filter="all">All</button>` : ''}
    </div>
    ${body}
    <div id="leadPanel"></div>`;

  const redraw = () => render(mount, filter);
  mount.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => { setView(b.dataset.view); redraw(); }));
  mount.querySelectorAll('[data-filter]').forEach((b) =>
    b.addEventListener('click', () => render(mount, b.dataset.filter)));

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
          lostWrap.classList.toggle('hidden', lead.status !== 'lost');
          if (lead.status !== 'lost') host.querySelector('#wLost').value = '';
          else host.querySelector('#wLost').focus();
          await refreshHistory();
        }, `Moved to ${LABEL[to]}`);
      }));
    bindBar();
    host.querySelector('#wSave').addEventListener('click', () => guard(async () => {
      const saved = await api.patch(`/admin/contact-requests/${id}`, {
        status: lead.status,
        notes: host.querySelector('#wNotes').value,
        lost_reason: host.querySelector('#wLost').value,
      });
      Object.assign(lead, saved && saved.id ? saved : {});
      changed = true;
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

  mount.querySelectorAll('.lead [data-open]').forEach((b) =>
    b.addEventListener('click', () => openPanel(b.closest('.lead').dataset.id)));

  /* the board: click to open, drag to move */
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
      // A lead in the Demo sub-stage already sits in the Contacted column.
      if (!lead || lead.status === to || (to === 'contacted' && lead.status === 'demo')) return;
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
}
