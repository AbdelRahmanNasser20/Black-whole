// static/site/platform.js — the read-only demo on GET /platform. ES module.
//
//   Deal finder  LIVE    reads /deals/api/facets + /deals/api/lots through UI.api. Those endpoints are the
//                        public policy (automation/web/public_deals.py); nothing is filtered or widened here.
//                        Every number on the page comes from them; with no open auctions the tab says so.
//   Inventory    SAMPLE  static/site/platform/inventory.sample.json — invented lots, filtered/sorted in memory.
//   Buyer CRM    SAMPLE  static/site/platform/crm.sample.json — invented buyers and threads, in memory.
//
// The one write on the page is the request-access form: POST /contact (existing endpoint, unchanged),
// with the lead tagged in `message` because `inquiries` has no source column. Everything else — sorting,
// filtering, selecting a lot, approving a draft reply — changes memory only and is gone on reload.
import {api, pending, fmt, esc} from '../ui/state.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const LEAD_TAG = '[PLATFORM PAGE]';
const SAMPLE_BASE = '/static/site/platform/';
const PER_PAGE = 25;                       // one of the server's own per_page choices
const HOUR_MS = 3600 * 1000;

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

// ═════════════════════════════ tabs ═════════════════════════════
const TABS = ['deals', 'inventory', 'crm'];
const opened = new Set();
const onOpen = {};                         // tab → loader, run the first time the tab is shown

function showTab(name, {focus = false, scroll = false, hash = true} = {}) {
  if (!TABS.includes(name)) return;
  for (const t of TABS) {
    const tab = $(`#pf-tab-${t}`), pane = $(`#pf-pane-${t}`);
    const on = t === name;
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    pane.hidden = !on;
    if (on && focus) tab.focus();
  }
  if (!opened.has(name)) { opened.add(name); onOpen[name]?.(); }
  if (hash) history.replaceState(null, '', '#' + name);
  if (scroll) $('#demo').scrollIntoView({block: 'start'});
}

function initTabs() {
  const list = $('.pf-tabs');
  list.addEventListener('click', e => {
    const tab = e.target.closest('.pf-tab');
    if (tab) showTab(tab.dataset.tab);
  });
  list.addEventListener('keydown', e => {
    const i = TABS.indexOf(document.activeElement?.dataset?.tab);
    if (i < 0) return;
    let next = null;
    if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
    else if (e.key === 'ArrowLeft') next = TABS[(i + TABS.length - 1) % TABS.length];
    else if (e.key === 'Home') next = TABS[0];
    else if (e.key === 'End') next = TABS[TABS.length - 1];
    if (next) { e.preventDefault(); showTab(next, {focus: true}); }
  });
  // The three headline lines are the index of the demo.
  $$('[data-pf-jump]').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    showTab(a.dataset.pfJump, {scroll: true});
  }));
  const fromHash = location.hash.replace('#', '');
  showTab(TABS.includes(fromHash) ? fromHash : 'deals', {hash: false, scroll: TABS.includes(fromHash)});
}

// ═════════════════════════ deal finder (LIVE) ═════════════════════════
// Two honest states, both straight from the endpoint:
//   · the feed has open auctions  → "Open now" lists them, the tab tag reads "Live data";
//   · the feed has none           → the pane says so and offers the closed archive. It never auto-runs the
//     closed query: that one scans the whole archive (tens of seconds) and holds a pooled DB connection.
const CAT_LABELS = {
  general_merchandise: 'General', vehicles: 'Vehicles', collectibles_jewelry: 'Collectibles & jewelry',
  computers_electronics: 'Computers & electronics', other: 'Other',
};
const catLabel = v => CAT_LABELS[v] || words(v).replace(/^./, c => c.toUpperCase());
// Facets only count OPEN auctions, so with none open they come back empty. These keep the filters usable;
// they carry no numbers, and the server still decides what each one matches.
const FALLBACK_CATS = Object.keys(CAT_LABELS);
const US_STATES = ('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY '
  + 'NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY').split(' ');
const SORTS = {
  active: [['ends', 'Ending soonest'], ['newest', 'Newest'], ['bid:asc', 'Bid, low to high'],
           ['bid:desc', 'Bid, high to low'], ['bids:desc', 'Most bids'], ['bids:asc', 'Fewest bids']],
  closed: [['ends:desc', 'Most recently closed'], ['bid:desc', 'Final bid, high to low'],
           ['bid:asc', 'Final bid, low to high'], ['bids:desc', 'Most bids']],
};
const TIMEOUT_MS = {active: 20000, closed: 60000};

const deals = {status: 'active', q: '', category: '', state: '', sort: 'ends', dir: '', noBids: false, page: 1};
let dealsAbort = null;
let dealsBody = null;
let dealStats = null;                      // facets.stats, when it has arrived

const dealsFiltered = () => !!(deals.q || deals.category || deals.state || deals.noBids);

function dealsQuery() {
  const p = new URLSearchParams({status: deals.status, per_page: String(PER_PAGE), page: String(deals.page), sort: deals.sort});
  if (deals.dir) p.set('dir', deals.dir);
  if (deals.q) p.set('q', deals.q);
  if (deals.category) p.set('category', deals.category);
  if (deals.state) p.set('state', deals.state);
  if (deals.noBids) p.set('max_bids', '0');
  return p.toString();
}

function dealRow(r) {
  const closed = !!r.outcome_complete;
  const bid = closed && r.final_bid != null ? r.final_bid : r.current_bid;
  const bids = Number(closed && r.final_bid_count != null ? r.final_bid_count : r.bid_count) || 0;
  // $/unit and all-in are computed server-side from current_bid; show them only when that is the price on screen.
  const derived = !closed || r.final_bid == null || Number(r.final_bid) === Number(r.current_bid);
  const qty = Number(r.quantity) || 0;
  const none = '<span class="pf-none">—</span>';
  const perUnit = derived && qty > 1 && r.quantity_source !== 'default' && r.unit_bid != null
    ? `${esc(money(r.unit_bid))}<span class="pf-sub">× ${esc(fmt.int(qty))}</span>` : none;
  const allIn = derived && r.landed_cost != null ? esc(money(r.landed_cost)) : none;
  // A closed auction with no bids never had a final bid: the number on record is the opening price.
  const bidNote = closed && !bids ? '<span class="pf-sub">opening bid</span>' : '';
  const place = [r.city, r.state].filter(Boolean).map(esc).join(', ') || 'Location not listed';
  const src = r.govdeals_url
    ? `<a class="pf-src" href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">GovDeals<span aria-hidden="true"> ↗</span></a>` : '';
  let endCell;
  if (closed) {
    const when = r.end_utc ? new Date(r.end_utc).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'}) : '';
    endCell = `<td class="num pf-result" data-label="Result">${esc(words(r.outcome) || 'closed')}<span class="pf-sub">${esc(when)}</span></td>`;
  } else {
    const left = r.end_utc ? new Date(r.end_utc).getTime() - Date.now() : NaN;
    const urgent = Number.isFinite(left) && left > 0 && left < HOUR_MS;
    endCell = `<td class="num pf-timer${urgent ? ' is-urgent' : ''}" data-label="Ends in" data-ends="${esc(r.end_utc || '')}">${esc(fmt.endsIn(r.end_utc))}</td>`;
  }
  return `<tr>
    <td class="pf-c-lot" data-label="Lot">
      <a class="pf-lot-title" href="${esc(r.viewer_url || r.govdeals_url || '#')}" target="_blank" rel="noopener">${esc(r.title || 'Untitled lot')}</a>
      <span class="pf-sub">${place}${src ? ' ' + src : ''}</span>
    </td>
    <td class="pf-c-cat" data-label="Category">${esc(catLabel(r.canonical_category || 'other'))}</td>
    <td class="num" data-label="${closed ? 'Final bid' : 'Current bid'}">${esc(money(bid))}${bidNote}</td>
    <td class="num" data-label="Per unit">${perUnit}</td>
    <td class="num" data-label="Est. all-in">${allIn}</td>
    <td class="num${bids ? '' : ' pf-zero'}" data-label="Bids">${esc(fmt.int(bids))}</td>
    ${endCell}
  </tr>`;
}

function renderDealsPager() {
  const b = dealsBody;
  const count = $('#pf-deals-count');
  if (!b || !b.total) { count.textContent = ''; $('#pf-deals-prev').disabled = true; $('#pf-deals-next').disabled = true; return; }
  const from = (b.page - 1) * b.per_page + 1, to = Math.min(b.total, b.page * b.per_page);
  count.textContent = `${fmt.int(from)}–${fmt.int(to)} of ${fmt.int(b.total)} ${deals.status === 'closed' ? 'closed' : 'open'} auctions`;
  $('#pf-deals-prev').disabled = b.page <= 1;
  $('#pf-deals-next').disabled = b.page >= b.pages;
}

function dealsEmptyHtml() {
  if (dealsFiltered()) return 'No auctions match these filters. <button class="pf-btn" type="button" data-deals-reset>Clear filters</button>';
  if (deals.status === 'active') {
    return 'No auctions are open in the feed right now. The record of closed auctions is still searchable. '
      + '<button class="pf-btn pf-btn--go" type="button" data-deals-closed>Show closed auctions</button>';
  }
  return 'No closed auctions on record yet.';
}

// The claim on the tab follows the data: "Live data" only while the endpoint reports open auctions.
function paintDealsTruth(openCount) {
  if (openCount == null) return;
  const live = openCount > 0;
  $('#pf-deals-tag').textContent = live ? 'Live data' : 'Real data';
  $('#pf-pane-deals .pf-pane-note strong').textContent = live ? 'Live data.' : 'Real data.';
}

function renderDealStats() {
  const s = dealStats;
  if (!s) return;
  // Every number in this sentence comes from the endpoint. A missing one drops its clause rather than printing a guess.
  const bits = [];
  if (Number(s.active) > 0) {
    bits.push(`${fmt.int(s.active)} open now${s.states ? ` across ${fmt.int(s.states)} states` : ''}`);
    if (s.closed != null) bits.push(`${fmt.int(s.closed)} closed auctions on record`);
  } else {
    if (s.tracked != null) bits.push(`${fmt.int(s.tracked)} auctions on record`);
    if (s.no_bid != null) bits.push(`${fmt.int(s.no_bid)} of them closed with no bids`);
  }
  let text = bits.length ? bits.join(', ') + '.' : '';
  if (s.active != null && Number(s.active) === 0) text += ' None are open in the feed right now.';
  $('#pf-deals-stats').textContent = text.replace(/^./, c => c.toUpperCase());
  paintDealsTruth(Number(s.active));
}

async function loadDeals() {
  const tbody = $('#pf-deals-rows');
  dealsAbort?.abort();
  const ac = new AbortController(); dealsAbort = ac;
  const status = deals.status;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ac.abort(); }, TIMEOUT_MS[status]);
  tbody.setAttribute('aria-busy', 'true');
  if (!dealsBody || !dealsBody.total) {
    msgRow(tbody, 7, status === 'closed'
      ? 'Searching the closed auctions. The archive is large, so this can take up to half a minute.'
      : 'Loading auctions…');
  }
  try {
    const body = await api('/deals/api/lots?' + dealsQuery(), {signal: ac.signal});
    if (dealsAbort !== ac) return;
    dealsBody = body;
    if (status === 'active' && !dealsFiltered()) paintDealsTruth(body.total);
    if (!body.total) msgRow(tbody, 7, dealsEmptyHtml());
    else tbody.innerHTML = body.rows.map(dealRow).join('');
    renderDealsPager();
  } catch (err) {
    if (dealsAbort !== ac) return;               // superseded by a newer request
    dealsBody = null;
    const why = timedOut ? `The server did not answer in ${Math.round(TIMEOUT_MS[status] / 1000)} seconds.`
      : err.status ? `The server said ${err.status}.` : 'The request did not go through.';
    msgRow(tbody, 7, `The auction feed did not load. ${esc(why)} <button class="pf-btn" type="button" data-deals-retry>Try again</button>`);
    renderDealsPager();
  } finally {
    clearTimeout(timer);
    if (dealsAbort === ac) tbody.removeAttribute('aria-busy');
  }
}

function renderDealChips(cats) {
  const withCounts = cats.length > 0;
  const list = withCounts ? cats : FALLBACK_CATS.map(value => ({value}));
  const total = cats.reduce((a, c) => a + Number(c.count || 0), 0);
  const chip = (value, label, count) =>
    `<button class="pf-chip${value === deals.category ? ' is-active' : ''}" type="button" data-cat="${esc(value)}" aria-pressed="${value === deals.category}">${esc(label)}`
    + (withCounts ? ` <span class="pf-chip-n">${esc(fmt.int(count))}</span>` : '') + '</button>';
  $('#pf-deals-cats').innerHTML = chip('', 'All categories', total) + list.map(c => chip(c.value, catLabel(c.value), c.count)).join('');
}

function renderDealStates(states) {
  const sel = $('#pf-deals-state');
  sel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
  fillSelect(sel, states.length
    ? states.map(s => ({value: s.value, label: `${s.value} (${fmt.int(s.count)})`}))
    : US_STATES.map(v => ({value: v, label: v})));
  sel.value = deals.state;
}

function renderDealSorts() {
  const sel = $('#pf-deals-sort');
  sel.innerHTML = '';
  fillSelect(sel, SORTS[deals.status].map(([value, label]) => ({value, label})));
  const [sort, dir] = SORTS[deals.status][0][0].split(':');
  deals.sort = sort; deals.dir = dir || '';
  const closed = deals.status === 'closed';
  $('#pf-deals-th-bid').textContent = closed ? 'Final bid' : 'Current bid';
  $('#pf-deals-th-end').textContent = closed ? 'Result' : 'Ends in';
  $$('#pf-deals-status .pf-seg-btn').forEach(b => {
    const on = b.dataset.status === deals.status;
    b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

async function loadDealFacets() {
  let facets;
  try { facets = await api('/deals/api/facets'); } catch { return; }   // the fallback filters stay; the table reports its own errors
  if ((facets.categories || []).length) renderDealChips(facets.categories);
  if ((facets.states || []).length) renderDealStates(facets.states);
  dealStats = facets.stats || null;
  renderDealStats();
}

function setDeals(patch, {keepPage = false} = {}) {
  const statusChanged = patch.status && patch.status !== deals.status;
  Object.assign(deals, patch);
  if (statusChanged) { renderDealSorts(); dealsBody = null; }
  if (!keepPage) deals.page = 1;
  $$('#pf-deals-cats .pf-chip').forEach(c => {
    const on = (c.dataset.cat || '') === deals.category;
    c.classList.toggle('is-active', on); c.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  loadDeals();
}

function initDeals() {
  renderDealSorts(); renderDealChips([]); renderDealStates([]);
  let qTimer = null;
  $('#pf-deals-q').addEventListener('input', e => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => setDeals({q: e.target.value.trim()}), 400);
  });
  $('#pf-deals-form').addEventListener('submit', e => { e.preventDefault(); clearTimeout(qTimer); setDeals({q: $('#pf-deals-q').value.trim()}); });
  $('#pf-deals-status').addEventListener('click', e => { const b = e.target.closest('[data-status]'); if (b && b.dataset.status !== deals.status) setDeals({status: b.dataset.status}); });
  $('#pf-deals-state').addEventListener('change', e => setDeals({state: e.target.value}));
  $('#pf-deals-sort').addEventListener('change', e => { const [sort, dir] = e.target.value.split(':'); setDeals({sort, dir: dir || ''}); });
  $('#pf-deals-nobids').addEventListener('change', e => setDeals({noBids: e.target.checked}));
  $('#pf-deals-cats').addEventListener('click', e => { const c = e.target.closest('[data-cat]'); if (c) setDeals({category: c.dataset.cat}); });
  $('#pf-deals-prev').addEventListener('click', () => setDeals({page: Math.max(1, deals.page - 1)}, {keepPage: true}));
  $('#pf-deals-next').addEventListener('click', () => setDeals({page: deals.page + 1}, {keepPage: true}));
  $('#pf-deals-rows').addEventListener('click', e => {
    if (e.target.closest('[data-deals-retry]')) { loadDeals(); if (!dealStats) loadDealFacets(); }
    if (e.target.closest('[data-deals-closed]')) setDeals({status: 'closed'});
    if (e.target.closest('[data-deals-reset]')) {
      $('#pf-deals-q').value = ''; $('#pf-deals-state').value = ''; $('#pf-deals-nobids').checked = false;
      setDeals({q: '', category: '', state: '', noBids: false});
    }
  });
  setInterval(() => $$('#pf-deals-rows [data-ends]').forEach(el => {
    const iso = el.dataset.ends; if (!iso) return;
    const left = new Date(iso).getTime() - Date.now();
    el.textContent = fmt.endsIn(iso);
    el.classList.toggle('is-urgent', left > 0 && left < HOUR_MS);
  }), 30000);
}
onOpen.deals = () => { loadDealFacets(); loadDeals(); };

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
  $('#pf-inv-q').addEventListener('input', e => { inv.q = e.target.value.trim(); if (inv.data) renderInv(); });
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
      $('#pf-inv-q').value = ''; $('#pf-inv-status').value = ''; $('#pf-inv-channel').value = '';
      Object.assign(inv, {q: '', status: '', channel: ''}); renderInv(); return;
    }
    const tr = e.target.closest('tr.pf-pick');
    if (tr) selectLot(tr.dataset.lot);
  });
}
onOpen.inventory = loadInv;

// ═════════════════════════ buyer CRM (SAMPLE) ═════════════════════════
const crm = {stage: '', channel: '', sort: 'recent', selected: null, data: null};
const lastMins = b => Math.min(...b.thread.map(m => m.mins_ago));
const label = (list, key) => (list.find(x => x.key === key) || {}).label || words(key);

function crmRows() {
  const rows = crm.data.buyers.filter(b => (!crm.stage || b.stage === crm.stage) && (!crm.channel || b.channel === crm.channel));
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
      $('#pf-crm-stage').value = ''; $('#pf-crm-channel').value = '';
      Object.assign(crm, {stage: '', channel: ''}); renderBuyers(); return;
    }
    const b = e.target.closest('[data-buyer]');
    if (b) selectBuyer(b.dataset.buyer);
  });
  $('#pf-crm-thread').addEventListener('click', e => {
    if (e.target.closest('[data-crm-back]')) { selectBuyer(null); return; }
    const lot = e.target.closest('[data-open-lot]');
    if (lot) {                                   // jump to the same lot on the Inventory tab
      const go = () => { Object.assign(inv, {q: '', status: '', channel: '', selected: lot.dataset.openLot}); renderInv(); };
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
onOpen.crm = loadCrm;

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

if ($('#demo')) { initDeals(); initInv(); initCrm(); initTabs(); initAccess(); }
