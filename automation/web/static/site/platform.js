// static/site/platform.js — the product window on GET /platform. ES module.
//
//   The shell   sidebar sections, the top-bar search (it searches whichever section is open), deep links.
//   Auctions    REAL    ./platform/auctions.js — filters, list and map in sync; also owns Favorites
//                       (the visitor's own stars, this browser only).
//   Inventory   SAMPLE  static/site/platform/inventory.sample.json — invented lots, filtered/sorted in memory.
//   Buyers      SAMPLE  static/site/platform/crm.sample.json — invented buyers and threads, in memory.
//                       The lot → buyers-in-radius map under it is ./platform/map.js.
//   Sites       LIVE    GET /platform/api/sites, else GET /platform/api/sources. Each site shows its true
//                       status: Live, Paused or Planned. The names are already in the HTML; a failed read
//                       leaves them names-only and claims nothing.
//
// The one write on the page is the request-access form: POST /contact (existing endpoint, unchanged),
// with the lead tagged in `message` because `inquiries` has no source column. Everything else — sorting,
// filtering, selecting a lot, approving a draft reply — changes memory only and is gone on reload.
import {api, pending, fmt, esc} from '../ui/state.js';
import * as auctions from './platform/auctions.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const LEAD_TAG = '[PLATFORM PAGE]';
const SAMPLE_BASE = '/static/site/platform/';

function words(v) { return String(v || '').replace(/_/g, ' '); }
// Whole dollars stay whole; anything else shows cents ($5,062.50, never $5,062.5).
function money(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const d = Number.isInteger(n) ? 0 : 2;
  return '$' + n.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function ago(mins) {
  const m = Math.max(0, Number(mins) || 0);
  if (m < 1) return 'just now';
  if (m < 60) return `${Math.round(m)} min ago`;
  if (m < 60 * 48) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}
function fillSelect(sel, items) {
  for (const it of items) {
    const o = document.createElement('option');
    o.value = it.value; o.textContent = it.label; sel.appendChild(o);
  }
}
function msgRow(tbody, cols, html) {
  tbody.innerHTML = `<tr class="pf-row-msg"><td colspan="${cols}">${html}</td></tr>`;
}

// ═════════════════════════════ the window shell ═════════════════════════════
const TABS = ['auctions', 'favorites', 'inventory', 'buyers', 'sites'];
const SEARCH_HINT = {
  auctions: 'Search auctions', favorites: 'Search favorites', inventory: 'Search lots',
  buyers: 'Search buyers', sites: 'Search sites',
};
const opened = new Set();
const onOpen = {};                         // section → loader, run the first time the section is shown
const onShow = {};                         // section → run every time it is shown
const onSearch = {};                       // section → apply the top-bar query
const query = {};                          // section → what its search field holds
let current = null;

function showTab(name, {focus = false, scroll = false, hash = true} = {}) {
  if (!TABS.includes(name)) return;
  current = name;
  for (const t of TABS) {
    const tab = $(`#pf-tab-${t}`), pane = $(`#pf-pane-${t}`);
    const on = t === name;
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    pane.hidden = !on;
    if (on && focus) tab.focus();
  }
  $('#pf-win').dataset.section = name;
  const q = $('#pf-q');
  q.value = query[name] || ''; q.placeholder = SEARCH_HINT[name];
  if (!opened.has(name)) { opened.add(name); onOpen[name]?.(); }
  onShow[name]?.();
  if (hash) history.replaceState(null, '', '#' + name);
  if (scroll) $('#app').scrollIntoView({block: 'start'});
}

function initShell() {
  const list = $('.pf-side-nav');
  list.addEventListener('click', e => {
    const tab = e.target.closest('.pf-side-item');
    if (tab) showTab(tab.dataset.tab);
  });
  list.addEventListener('keydown', e => {
    const i = TABS.indexOf(document.activeElement?.dataset?.tab);
    if (i < 0) return;
    let next = null;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = TABS[(i + TABS.length - 1) % TABS.length];
    else if (e.key === 'Home') next = TABS[0];
    else if (e.key === 'End') next = TABS[TABS.length - 1];
    if (next) { e.preventDefault(); showTab(next, {focus: true}); }
  });
  // One search field for the window: it searches the section on screen and remembers each section's query.
  let qTimer = null;
  const apply = () => { query[current] = $('#pf-q').value.trim(); onSearch[current]?.(query[current]); };
  $('#pf-q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(apply, current === 'auctions' ? 400 : 120); });
  $('#pf-q').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(qTimer); apply(); } });
  // Any [data-pf-jump] link opens its section (kept for deep links from copy).
  $$('[data-pf-jump]').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    showTab(a.dataset.pfJump, {scroll: true});
  }));
  const fromHash = location.hash.replace('#', '').replace(/^crm$/, 'buyers').replace(/^deals$/, 'auctions');
  showTab(TABS.includes(fromHash) ? fromHash : 'auctions', {hash: false, scroll: TABS.includes(fromHash)});
}
function clearSearch() { query[current] = ''; $('#pf-q').value = ''; }

// ═════════════════════════ auctions + favorites (REAL) ═════════════════════════
// Everything lives in ./platform/auctions.js; the shell only routes the search field and section changes.
onShow.auctions = () => auctions.shown();
onSearch.auctions = q => auctions.setQuery(q);
onSearch.favorites = q => auctions.setFavoritesQuery(q);

// ═════════════════════ inventory & listings (SAMPLE) ═════════════════════
const STATUS_LABELS = {
  draft: 'Draft', listed: 'Listed', hidden: 'Hidden', sold_out: 'Sold out',
  owned: 'In storage', won_pickup: 'Won, awaiting pickup', active_bid: 'Bidding',
};
const CH_SHORT = {off: 'off', queued: 'queued', pending_approval: 'needs OK', live: 'live', delisted: 'down', error: 'error'};
const CH_EXPLAIN = {
  live: 'Listed. Quantity and price follow the ledger.',
  pending_approval: 'Waiting in the approval queue. Nothing posts until the operator approves it.',
  queued: 'Approved. Posts on the next sync pass.',
  off: 'Not listed on this channel.',
  delisted: 'Taken down because the lot is no longer for sale.',
  error: 'The last attempt failed and is waiting for a retry.',
};
const inv = {q: '', status: '', channel: '', sort: 'lot_id', dir: 'desc', selected: null, data: null};

function invRows() {
  const q = inv.q.toLowerCase();
  const rows = inv.data.lots.filter(l => {
    if (inv.status && l.status !== inv.status) return false;
    if (inv.channel && l.channels?.[inv.channel]?.state !== 'live') return false;
    if (!q) return true;
    return [l.lot_id, l.title, l.city, l.state, STATUS_LABELS[l.status]].join(' ').toLowerCase().includes(q);
  });
  const k = inv.sort, sign = inv.dir === 'asc' ? 1 : -1;
  return rows.sort((a, b) => {
    const x = a[k], y = b[k];
    return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sign;
  });
}

function invDetail(l) {
  const chans = inv.data.channels.map(c => {
    const st = l.channels?.[c.key] || {state: 'off'};
    const state = CH_SHORT[st.state] ? st.state : 'off';
    const synced = st.synced_hours_ago != null ? `<span class="pf-sub">Checked ${esc(ago(st.synced_hours_ago * 60))}.</span>` : '';
    const why = state === 'error' && st.last_error ? `Last error: ${st.last_error}.` : CH_EXPLAIN[state];
    return `<li><span class="pf-detail-ch">${esc(c.label)}</span><span class="pf-ch pf-ch--${state}">${esc(CH_SHORT[state])}</span><span class="pf-detail-why">${esc(why)} ${synced}</span></li>`;
  }).join('');
  const sold = l.quantity_original - l.quantity_remaining;
  return `<tr class="pf-detail-row"><td colspan="11">
    <div class="pf-detail">
      <div class="pf-detail-facts">
        <h3 class="pf-detail-title">${esc(l.title)}</h3>
        <dl>
          <div><dt>Lot</dt><dd>${esc(l.lot_id)}</dd></div>
          <div><dt>Where</dt><dd>${esc(l.city)}, ${esc(l.state)}</dd></div>
          <div><dt>Left</dt><dd>${esc(fmt.int(l.quantity_remaining))} of ${esc(fmt.int(l.quantity_original))}</dd></div>
          <div><dt>Sold so far</dt><dd>${esc(fmt.int(sold))}</dd></div>
          <div><dt>Price</dt><dd>${esc(money(l.price_per_unit))} per ${esc(l.unit || 'unit')}</dd></div>
          <div><dt>Status</dt><dd>${esc(STATUS_LABELS[l.status] || words(l.status))}</dd></div>
        </dl>
      </div>
      <ul class="pf-detail-channels" aria-label="Where this lot is listed">${chans}</ul>
    </div>
  </td></tr>`;
}

function renderInv() {
  const tbody = $('#pf-inv-rows');
  const rows = invRows();
  $('#pf-inv-count').textContent = `${rows.length} of ${inv.data.lots.length} sample lots`;
  $$('#pf-pane-inventory .pf-sort').forEach(b => {
    const on = b.dataset.sort === inv.sort;
    b.classList.toggle('is-sorted', on);
    b.closest('th').setAttribute('aria-sort', on ? (inv.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    b.dataset.dir = on ? inv.dir : '';
  });
  if (!rows.length) {
    msgRow(tbody, 11, 'No sample lots match. <button class="pf-btn" type="button" data-inv-reset>Clear filters</button>');
    return;
  }
  tbody.innerHTML = rows.map(l => {
    const sel = l.lot_id === inv.selected;
    const cells = inv.data.channels.map(c => {
      const raw = l.channels?.[c.key]?.state;
      const state = CH_SHORT[raw] ? raw : 'off';
      return `<td class="pf-c-ch" data-label="${esc(c.label)}"><span class="pf-ch pf-ch--${state}">${esc(CH_SHORT[state])}</span></td>`;
    }).join('');
    return `<tr class="pf-pick${sel ? ' is-selected' : ''}" data-lot="${esc(l.lot_id)}">
      <td class="pf-c-id" data-label="Lot"><button class="pf-pick-btn" type="button" aria-expanded="${sel}">${esc(l.lot_id)}</button></td>
      <td class="pf-c-lot" data-label="Item"><span class="pf-lot-title">${esc(l.title)}</span><span class="pf-sub">${esc(l.city)}, ${esc(l.state)}</span></td>
      <td class="num" data-label="Qty left">${esc(fmt.int(l.quantity_remaining))}<span class="pf-sub">of ${esc(fmt.int(l.quantity_original))}</span></td>
      <td class="num" data-label="Price / unit">${esc(money(l.price_per_unit))}</td>
      <td class="pf-c-status" data-label="Status"><span class="pf-status pf-status--${esc(l.status)}">${esc(STATUS_LABELS[l.status] || words(l.status))}</span></td>
      ${cells}
    </tr>${sel ? invDetail(l) : ''}`;
  }).join('');
}

function selectLot(lotId) {
  inv.selected = inv.selected === lotId ? null : lotId;
  renderInv();
}

async function loadInv() {
  const tbody = $('#pf-inv-rows');
  try {
    inv.data = await api(SAMPLE_BASE + 'inventory.sample.json');
  } catch {
    msgRow(tbody, 11, 'The sample lots did not load. <button class="pf-btn" type="button" data-inv-retry>Try again</button>');
    return;
  }
  const statuses = [...new Set(inv.data.lots.map(l => l.status))];
  if ($('#pf-inv-status').options.length === 1) {
    fillSelect($('#pf-inv-status'), statuses.map(s => ({value: s, label: STATUS_LABELS[s] || words(s)})));
    fillSelect($('#pf-inv-channel'), inv.data.channels.map(c => ({value: c.key, label: c.label})));
  }
  renderInv();
}

function initInv() {
  $('#pf-inv-form').addEventListener('submit', e => e.preventDefault());
  $('#pf-inv-status').addEventListener('change', e => { inv.status = e.target.value; if (inv.data) renderInv(); });
  $('#pf-inv-channel').addEventListener('change', e => { inv.channel = e.target.value; if (inv.data) renderInv(); });
  $('#pf-inv-sort').addEventListener('change', e => { [inv.sort, inv.dir] = e.target.value.split(':'); if (inv.data) renderInv(); });
  $$('#pf-pane-inventory .pf-sort').forEach(b => b.addEventListener('click', () => {
    if (inv.sort === b.dataset.sort) inv.dir = inv.dir === 'asc' ? 'desc' : 'asc';
    else { inv.sort = b.dataset.sort; inv.dir = 'asc'; }
    if (inv.data) renderInv();
  }));
  $('#pf-inv-rows').addEventListener('click', e => {
    if (e.target.closest('[data-inv-retry]')) { loadInv(); return; }
    if (e.target.closest('[data-inv-reset]')) {
      clearSearch(); $('#pf-inv-status').value = ''; $('#pf-inv-channel').value = '';
      Object.assign(inv, {q: '', status: '', channel: ''}); renderInv(); return;
    }
    const tr = e.target.closest('tr.pf-pick');
    if (tr) selectLot(tr.dataset.lot);
  });
}
onOpen.inventory = loadInv;
onSearch.inventory = q => { inv.q = q; if (inv.data) renderInv(); };

// ═════════════════════════ buyer CRM (SAMPLE) ═════════════════════════
const crm = {q: '', stage: '', channel: '', sort: 'recent', selected: null, data: null};
const lastMins = b => Math.min(...b.thread.map(m => m.mins_ago));
const label = (list, key) => (list.find(x => x.key === key) || {}).label || words(key);

function crmRows() {
  const q = crm.q.toLowerCase();
  const rows = crm.data.buyers.filter(b => (!crm.stage || b.stage === crm.stage) && (!crm.channel || b.channel === crm.channel)
    && (!q || [b.name, b.about, b.wants, b.lot_id].join(' ').toLowerCase().includes(q)));
  if (crm.sort === 'qty') rows.sort((a, b) => (b.quantity_wanted || 0) - (a.quantity_wanted || 0));
  else if (crm.sort === 'name') rows.sort((a, b) => a.name.localeCompare(b.name));
  else rows.sort((a, b) => lastMins(a) - lastMins(b));
  return rows;
}

function renderBuyers() {
  const rows = crmRows();
  const list = $('#pf-crm-list');
  $('#pf-crm-count').textContent = `${rows.length} of ${crm.data.buyers.length} sample buyers`;
  if (!rows.length) {
    list.innerHTML = '<li class="pf-row-msg">No sample buyers match. <button class="pf-btn" type="button" data-crm-reset>Clear filters</button></li>';
    return;
  }
  list.innerHTML = rows.map(b => {
    const last = b.thread[b.thread.length - 1];
    const sel = b.id === crm.selected;
    return `<li><button class="pf-buyer${sel ? ' is-selected' : ''}" type="button" data-buyer="${esc(b.id)}" aria-pressed="${sel}">
      <span class="pf-buyer-top"><span class="pf-buyer-name">${esc(b.name)}</span><span class="pf-buyer-when">${esc(ago(lastMins(b)))}</span></span>
      <span class="pf-buyer-meta"><span class="pf-stage pf-stage--${esc(b.stage)}">${esc(label(crm.data.stages, b.stage))}</span>${esc(label(crm.data.channels, b.channel))}${b.quantity_wanted ? `, wants ${esc(fmt.int(b.quantity_wanted))}` : ''}</span>
      <span class="pf-buyer-last">${last.from === 'seller' ? 'You: ' : ''}${esc(last.text)}</span>
    </button></li>`;
  }).join('');
}

function renderThread() {
  const host = $('#pf-crm-thread');
  const b = crm.data.buyers.find(x => x.id === crm.selected);
  $('#pf-crm').classList.toggle('is-thread-open', !!b);
  if (!b) { host.innerHTML = '<p class="pf-thread-empty">Pick a buyer to read the conversation.</p>'; return; }
  const msgs = b.thread.map(m => `<li class="pf-msg pf-msg--${m.from === 'seller' ? 'out' : 'in'}">
      <span class="pf-msg-who">${m.from === 'seller' ? 'You' : esc(b.name)}, ${esc(ago(m.mins_ago))}${m.demo ? ' (demo only, nothing was sent)' : ''}</span>
      <span class="pf-msg-text">${esc(m.text)}</span></li>`).join('');
  const draft = b.draft ? `<div class="pf-draft">
      <span class="pf-draft-head">Draft reply, waiting for approval. Held because it ${esc(b.draft.held_for)}.</span>
      <p class="pf-draft-text">${esc(b.draft.text)}</p>
      <span class="pf-draft-btns"><button class="pf-btn pf-btn--go" type="button" data-draft="approve">Approve reply</button><button class="pf-btn" type="button" data-draft="discard">Discard draft</button></span>
    </div>` : '';
  host.innerHTML = `<button class="pf-btn pf-thread-back" type="button" data-crm-back>Back to buyers</button>
    <div class="pf-thread-head">
      <h3 class="pf-thread-name">${esc(b.name)}</h3>
      <span class="pf-sub">${esc(b.about)}, on ${esc(label(crm.data.channels, b.channel))}</span>
      <dl>
        <div><dt>Stage</dt><dd><span class="pf-stage pf-stage--${esc(b.stage)}">${esc(label(crm.data.stages, b.stage))}</span></dd></div>
        <div><dt>Wants</dt><dd>${b.quantity_wanted ? esc(fmt.int(b.quantity_wanted)) + ' ' : ''}${esc(b.wants)}</dd></div>
        <div><dt>Lot</dt><dd><button class="pf-link" type="button" data-open-lot="${esc(b.lot_id)}">${esc(b.lot_id)}</button></dd></div>
        <div><dt>Next action</dt><dd>${esc(b.next_action)}</dd></div>
      </dl>
    </div>
    <ol class="pf-msgs">${msgs}</ol>${draft}`;
}

function selectBuyer(id) { crm.selected = id; renderBuyers(); renderThread(); }

async function loadCrm() {
  try {
    crm.data = await api(SAMPLE_BASE + 'crm.sample.json');
  } catch {
    $('#pf-crm-list').innerHTML = '<li class="pf-row-msg">The sample buyers did not load. <button class="pf-btn" type="button" data-crm-retry>Try again</button></li>';
    return;
  }
  if ($('#pf-crm-stage').options.length === 1) {
    fillSelect($('#pf-crm-stage'), crm.data.stages.map(s => ({value: s.key, label: s.label})));
    fillSelect($('#pf-crm-channel'), crm.data.channels.map(c => ({value: c.key, label: c.label})));
  }
  // Wide screens open the first conversation so the pane is never half empty; phones start on the list.
  if (!crm.selected && window.matchMedia('(min-width: 861px)').matches) crm.selected = crmRows()[0]?.id || null;
  renderBuyers(); renderThread();
}

function initCrm() {
  $('#pf-crm-form').addEventListener('submit', e => e.preventDefault());
  for (const [sel, key] of [['#pf-crm-stage', 'stage'], ['#pf-crm-channel', 'channel'], ['#pf-crm-sort', 'sort']]) {
    $(sel).addEventListener('change', e => { crm[key] = e.target.value; if (crm.data) renderBuyers(); });
  }
  $('#pf-crm-list').addEventListener('click', e => {
    if (e.target.closest('[data-crm-retry]')) { loadCrm(); return; }
    if (e.target.closest('[data-crm-reset]')) {
      clearSearch(); $('#pf-crm-stage').value = ''; $('#pf-crm-channel').value = '';
      Object.assign(crm, {q: '', stage: '', channel: ''}); renderBuyers(); return;
    }
    const b = e.target.closest('[data-buyer]');
    if (b) selectBuyer(b.dataset.buyer);
  });
  $('#pf-crm-thread').addEventListener('click', e => {
    if (e.target.closest('[data-crm-back]')) { selectBuyer(null); return; }
    const lot = e.target.closest('[data-open-lot]');
    if (lot) {                                   // jump to the same lot in the Inventory section
      const go = () => { Object.assign(inv, {q: '', status: '', channel: '', selected: lot.dataset.openLot}); renderInv(); };
      query.inventory = '';
      showTab('inventory', {focus: true});
      if (inv.data) go(); else loadInv().then(() => inv.data && go());
      return;
    }
    const act = e.target.closest('[data-draft]');
    const b = crm.data.buyers.find(x => x.id === crm.selected);
    if (!act || !b || !b.draft) return;
    // Memory only: the demo never sends, saves or posts anything.
    if (act.dataset.draft === 'approve') b.thread.push({from: 'seller', mins_ago: 0, text: b.draft.text, demo: true});
    delete b.draft;
    renderBuyers(); renderThread();
  });
}
onOpen.buyers = loadCrm;
onSearch.buyers = q => { crm.q = q; if (crm.data) renderBuyers(); };

// ═════════════════════════ request access ═════════════════════════
// POST /contact as-is. `inquiries` has no source column, so the page tags the lead in the message text.
export function accessPayload(fields) {
  const clean = k => String(fields[k] || '').trim();
  const parts = [`I am: ${clean('role') || 'not given'}`];
  if (clean('company')) parts.push(`Company: ${clean('company')}`);
  if (clean('note')) parts.push(clean('note'));
  // The tag leads the message so it survives the 240-character Telegram preview and any truncation.
  return {kind: 'buy', name: clean('name'), email: clean('email'), message: `${LEAD_TAG} ${parts.join(' | ')}`};
}

function initAccess() {
  const form = $('#pf-access-form');
  if (!form) return;
  const result = form.querySelector('.mf-result');
  const show = (text, ok) => {
    result.textContent = text;
    result.classList.toggle('mf-result--ok', ok); result.classList.toggle('mf-result--err', !ok);
    result.hidden = false;
  };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const payload = accessPayload(Object.fromEntries(new FormData(form).entries()));
    if (!payload.name) { show('Add your name so I know who is asking.', false); form.elements.name.focus(); return; }
    if (!/^\S+@\S+\.\S+$/.test(payload.email)) { show('Add an email address I can reply to.', false); form.elements.email.focus(); return; }
    try {
      const data = await pending(form.querySelector('button[type="submit"]'), 'REQUESTING…', () => api('/contact', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      })) || {};
      show(`Access requested. Request #${data.id} is in the inbox, and the reply will come by email.`, true);
      form.reset();
    } catch (err) {
      show(`The request did not send${err.message ? ': ' + err.message : ''}. Try again in a moment.`, false);
    }
  });
}

// ═════════════════════════ sites (LIVE) ═════════════════════════
// The server renders the names. This adds each site's true status. /platform/api/sites answers
// live | paused | planned; the older /platform/api/sources only knows live or not (shown as Paused).
// A planned site never shows a lot count, and the page never says "all".
const SITE_STATUS = {live: 'Live', paused: 'Paused', planned: 'Planned'};
const SITE_KIND = {government: 'Government', commercial: 'Commercial'};
const sites = {rows: null, q: ''};

async function fetchSites() {
  try {
    const d = await api('/platform/api/sites');
    const rows = Array.isArray(d) ? d : Array.isArray(d?.sites) ? d.sites : [];
    if (rows.length) return rows;
  } catch { /* route missing or down: try the older one */ }
  try {
    const d = await api('/platform/api/sources');
    if (d && d.ok && Array.isArray(d.sources) && d.sources.length) {
      return d.sources.map(s => ({key: s.key, name: s.name, kind: null, status: s.live ? 'live' : 'paused', lots: s.lots, last_seen: s.last_seen}));
    }
  } catch { /* names only */ }
  return null;
}

function renderSites() {
  if (!sites.rows) return;
  const q = sites.q.toLowerCase();
  const rows = sites.rows.filter(s => !q || `${s.name} ${s.kind || ''} ${s.status || ''}`.toLowerCase().includes(q));
  const n = k => sites.rows.filter(s => s.status === k).length;
  $('#pf-sites-count').textContent = ['live', 'paused', 'planned'].filter(n).map(k => `${n(k)} ${k}`).join(', ');
  $('#pf-n-sites').textContent = n('live') ? `${n('live')} live` : '';
  $('#pf-sites-list').innerHTML = rows.map(s => {
    const status = SITE_STATUS[s.status] ? s.status : null;
    const mins = s.last_seen ? (Date.now() - new Date(s.last_seen).getTime()) / 60000 : null;
    const scraped = status === 'live' || status === 'paused';
    return `<li class="pf-source${status ? ' is-' + status : ''}">
      <span class="pf-source-name">${esc(s.name)}</span>
      <span class="pf-source-kind">${esc(SITE_KIND[s.kind] || '')}</span>
      <span class="pf-source-n">${scraped && Number(s.lots) > 0 ? esc(fmt.int(s.lots)) + ' lots' : ''}</span>
      <span class="pf-source-seen">${scraped && mins != null ? 'seen ' + esc(ago(mins)) : ''}</span>
      ${status ? `<span class="pf-site-chip pf-site-chip--${status}">${SITE_STATUS[status]}</span>` : ''}
    </li>`;
  }).join('') || '<li class="pf-row-msg">No sites match this search.</li>';
}

async function loadSites() {
  if (!$('#pf-sites-list')) return;
  sites.rows = await fetchSites();
  renderSites();
}
onSearch.sites = q => { sites.q = q; renderSites(); };

if ($('#pf-win')) {
  auctions.init({showSection: name => showTab(name), clearSearch});
  initInv(); initCrm(); initShell(); initAccess();
  loadSites();
}
