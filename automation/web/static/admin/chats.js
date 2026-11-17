// static/admin/chats.js — Sales › Chats: what visitors told the storefront chat, and how each one reached the site.
// Read-only. GET /api/chats (the list) and GET /api/chats/stats (the 30-day strip) go through UI.load / UI.api.
// The view filter lives in the URL (?cview=). A finished chat is also an inquiry and is worked on Inquiries.
import {$, $$, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const filter = {view: 'answered'};      // mirrors ?cview=
const list = () => $('#chat-list');
const VIEW_WORDS = {answered: 'answered', completed: 'finished', open: 'stopped', all: 'opened'};

// ───────── URL ↔ controls ─────────

function viewValues() { return $$('#chat-view-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const p = getParams();
  filter.view = viewValues().includes(p.cview || '') ? p.cview : 'answered';
  syncControls();
}

function syncControls() {
  $$('#chat-view-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.view;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setView(value) {
  filter.view = viewValues().includes(value) ? value : 'answered';
  setParams({cview: filter.view === 'answered' ? '' : filter.view});
  syncControls();
  loadChats({keepOld: true});
}

// ───────── formatting ─────────

function fmtWhen(iso) { return iso ? String(iso).replace('T', ' ').slice(0, 16) : ''; }

function fmtDuration(seconds) {
  if (seconds == null) return '';
  if (seconds < 90) return `${seconds} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

function fmtPhone(p) {
  const d = String(p || '').replace(/\D/g, '');
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : String(p || '');
}

function sourceOf(c) { return c.attr_source || c.attr_referrer || 'direct'; }

function pct(part, whole) { return whole ? `${Math.round(100 * part / whole)}%` : '0%'; }

// ───────── the 30-day strip ─────────

function statTile(value, label) {
  return `<div class="chat-stat"><div class="chat-stat-value">${escapeHtml(String(value))}</div>`
       + `<div class="chat-stat-label">${escapeHtml(label)}</div></div>`;
}

function rankList(title, rows, line) {
  if (!rows.length) return '';
  return `<div class="chat-rank"><div class="chat-rank-title">${escapeHtml(title)}</div><ul>`
       + rows.map(r => `<li>${line(r)}</li>`).join('') + '</ul></div>';
}

function statsHtml(s) {
  const tiles = [
    statTile(s.opened, `chats opened in ${s.days} days`),
    statTile(s.answered, 'answered a question'),
    statTile(s.completed, 'finished with a phone or email'),
    statTile(pct(s.completed, s.answered), 'of answered chats finish'),
    statTile(pct(s.on_phone, s.opened), 'opened on a phone'),
    statTile(s.median_seconds != null ? fmtDuration(s.median_seconds) : 'n/a', 'median time to finish'),
  ].join('');
  const ranks = [
    rankList('Where buyers stop', s.stopped_at, r => `<b>${escapeHtml(String(r.n))}</b> at ${escapeHtml(r.label)}`),
    rankList('Where chats come from', s.sources, r => `<b>${escapeHtml(String(r.chats))}</b> ${escapeHtml(r.source)} (${escapeHtml(String(r.completed))} finished)`),
    rankList('Page the chat was opened on', s.pages, r => `<b>${escapeHtml(String(r.chats))}</b> ${escapeHtml(r.page)}`),
  ].join('');
  return `<div class="chat-stat-row">${tiles}</div>` + (ranks ? `<div class="chat-rank-row">${ranks}</div>` : '');
}

async function loadStats() {
  const el = $('#chat-stats');
  if (!el) return;
  try {
    el.innerHTML = statsHtml(await api('/api/chats/stats'));
    el.hidden = false;
  } catch (err) { el.hidden = true; }
}

// ───────── one chat = one card ─────────

function answersHtml(c) {
  if (!c.log.length) return '<p class="chat-none">No answers.</p>';
  return '<dl class="chat-qa">' + c.log.map(e =>
    `<div><dt>${escapeHtml(e.q)}</dt><dd>${escapeHtml(e.a)}</dd></div>`).join('') + '</dl>';
}

function visitorHtml(c) {
  const rows = [
    ['Came from', [sourceOf(c), c.attr_medium, c.attr_campaign].filter(Boolean).join(' / ')],
    ['First page', c.attr_landing || ''],
    ['Opened chat on', c.page || ''],
    ['Device', [c.device, c.country].filter(Boolean).join(', ')],
    ['First seen', fmtWhen(c.first_seen)],
    ['Days on site', c.visit_days != null ? String(c.visit_days) : ''],
    ['Chats by this visitor', c.visitor_chats > 1 ? String(c.visitor_chats) : ''],
    ['Time in chat', fmtDuration(c.seconds)],
  ].filter(r => r[1]);
  const trail = (c.trail || []).map(t => {
    const at = new Date(t.t * 1000);
    const stamp = isNaN(at) ? '' : at.toISOString().replace('T', ' ').slice(5, 16);
    return `<li><span class="chat-trail-time">${escapeHtml(stamp)}</span> ${escapeHtml(t.p)}</li>`;
  }).join('');
  return '<dl class="chat-facts">' + rows.map(r => `<div><dt>${escapeHtml(r[0])}</dt><dd>${escapeHtml(r[1])}</dd></div>`).join('') + '</dl>'
       + (trail ? `<details class="chat-trail"><summary>${(c.trail || []).length} pages viewed (UTC)</summary><ol>${trail}</ol></details>` : '');
}

function cardHtml(c) {
  const done = c.status === 'completed';
  const contact = [
    c.phone ? `<a href="tel:${escapeAttr(c.phone)}">${escapeHtml(fmtPhone(c.phone))}</a>` : '',
    c.email ? `<a href="mailto:${escapeAttr(c.email)}">${escapeHtml(c.email)}</a>` : '',
    c.zip_code ? `ZIP ${escapeHtml(c.zip_code)}` : '',
    c.quantity ? `qty ${escapeHtml(String(c.quantity))}` : '',
  ].filter(Boolean).join('<span class="chat-sep">|</span>');
  const lots = (c.picked || []).map(p =>
    `<a class="chat-lot" href="${escapeAttr(p.url || '#')}" target="_blank" rel="noopener">${escapeHtml(p.title || p.lot_id)}</a>`).join('');
  return `
    <header class="chat-head">
      <span class="badge chat-status chat-status-${done ? 'done' : 'open'}">${done ? 'finished' : 'stopped at ' + escapeHtml(c.step_label || '')}</span>
      <span class="chat-src">${escapeHtml(sourceOf(c))}</span>
      ${c.inquiry_id ? `<a class="chat-inq" href="?tab=inquiries">inquiry #${escapeHtml(String(c.inquiry_id))}</a>` : ''}
      <span class="chat-when">${escapeHtml(fmtWhen(c.created_at))}</span>
    </header>
    <div class="chat-name">${escapeHtml(c.name || 'No name yet')}</div>
    ${contact ? `<div class="chat-contact">${contact}</div>` : ''}
    ${lots ? `<div class="chat-lots">${lots}</div>` : ''}
    <div class="chat-cols">
      <section><h3 class="chat-h">Answers</h3>${answersHtml(c)}</section>
      <section><h3 class="chat-h">Visitor</h3>${visitorHtml(c)}</section>
    </div>`;
}

function cardsFragment(items) {
  const frag = document.createDocumentFragment();
  items.forEach(c => {
    const card = document.createElement('article');
    card.className = `card card-chat chat-${c.status === 'completed' ? 'done' : 'open'}`;
    card.dataset.id = c.id;
    card.innerHTML = cardHtml(c);
    frag.appendChild(card);
  });
  return frag;
}

// ───────── data ─────────

function renderCount(n) {
  const el = $('#chat-count');
  if (el) el.textContent = n == null ? '' : `${n} ${VIEW_WORDS[filter.view]} chat${n === 1 ? '' : 's'}`;
}

function emptyArgs() {
  if (filter.view !== 'answered') {
    return {title: `No ${VIEW_WORDS[filter.view]} chats`, body: 'Nothing matches this filter right now.',
            cta: {label: 'Show answered', onClick: () => setView('answered')}};
  }
  return {glyph: '◌', title: 'No chats yet', body: 'The chat button on the storefront feeds this list.',
          cta: {label: 'Open the storefront', href: '/'}};
}

async function loadChats({keepOld = false} = {}) {
  const el = list();
  if (!el) return undefined;
  loadStats();
  return uiLoad(el, ({signal}) => api('/api/chats?view=' + encodeURIComponent(filter.view), {signal}), {
    skeleton: 'row', count: 5, keepOld,
    isEmpty(data) {
      const items = data.items || [];
      renderCount(items.length);
      return !items.length;
    },
    empty: emptyArgs(),
    render: (data) => cardsFragment(data.items || []),
    errorMessage: (err) => err.status === 409 ? String(err.detail)
      : err.status ? `Couldn't load chats. The server said ${err.status}.`
                   : "Couldn't load chats. The database didn't answer in 15 s.",
  });
}

// ───────── mount / load ─────────

let mounted = false;

export function mount() {
  if (mounted) return;
  mounted = true;
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }
  $('#chat-refresh')?.addEventListener('click', (e) => { pending(e.currentTarget, '↻ loading…', () => loadChats({keepOld: true})); });
  $$('#chat-view-filter .seg-btn').forEach(b => b.addEventListener('click', () => setView(b.dataset.value)));
}

export async function load() {
  readParams();
  const el = list();
  return loadChats({keepOld: !!el && el.dataset.state === 'ready'});
}
