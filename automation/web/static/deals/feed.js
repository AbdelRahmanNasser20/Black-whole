// static/deals/feed.js — public /deals feed (plan §6). ES module.
// The URL is the state (shareable links, back button works); `bbox` is transient and never written to it.
// Every read goes through UI.load; the only mutation (Load more) through UI.pending. No exclusions live here —
// public_deals.py owns the policy and the API rows never carry photos, verdicts, or distance.
//
// Contract with Workstream D (map.js), on `document`:
//   dispatch  feed:params  detail = {params: URLSearchParams (filters, no page/per_page/bbox/view), total, rows,
//                                    page, view, bbox}   — after every successful lots load
//   dispatch  feed:view    detail = {view: 'list'|'map'}  — after the toggle or a URL-driven view change (additive)
//   listen    feed:bbox    detail = {bbox: 'S,W,N,E' | ''} — sets st.bbox, reloads from page 1
// `view=map`: this file only toggles body.dataset.view + the toggle label and hides the grid; D does the rest.
import {load, api, pending, fmt, esc} from '../ui/state.js';
import {card, tickTimers} from '../ui/card.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const KEYS = ['q', 'category', 'state', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price', 'sort', 'dir', 'page', 'view'];
const DEFAULTS = {status: 'active', sort: 'ends', page: '1', view: 'list'};
const FILTER_KEYS = ['q', 'category', 'state', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price'];
const NARROWING_KEYS = ['category', 'state', 'max_bids', 'ending_within', 'min_price', 'max_price'];
const PER_PAGE = 24;          // divisible by 4/3/2 columns; the server clamps to its own choices and reports back
const MAX_RESTORE_PAGES = 10; // refresh restores `page` depth by appending pages 1..N, capped
const CAT_LABELS = {
  general_merchandise: 'General', vehicles: 'Vehicles', collectibles_jewelry: 'Collectibles & jewelry',
  computers_electronics: 'Computers & electronics', other: 'Other',
};
const catLabel = v => CAT_LABELS[v] || String(v || '').replace(/_/g, ' ');

const st = Object.assign({}, DEFAULTS, Object.fromEntries(
  KEYS.map(k => [k, new URLSearchParams(location.search).get(k)]).filter(([, v]) => v)));
st.bbox = '';
if (st.view !== 'map') st.view = 'list';
let facets = null;
let lastBody = null;

// ── URL / query helpers ─────────────────────────────────────────────────
function filterParams() {
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) if (st[k]) p.set(k, st[k]);
  if (st.sort) p.set('sort', st.sort);
  if (st.dir) p.set('dir', st.dir);
  return p;
}
function apiQs() {
  const p = filterParams();
  if (st.bbox) p.set('bbox', st.bbox);
  p.set('page', st.page || '1');
  p.set('per_page', String(PER_PAGE));
  return p.toString();
}
function pushUrl() {
  const p = new URLSearchParams();
  for (const k of KEYS) { const v = st[k]; if (v !== undefined && v !== null && v !== '' && DEFAULTS[k] !== v) p.set(k, v); }
  history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
}
function set(patch, {resetPage = true} = {}) {
  Object.assign(st, patch);
  if (resetPage) st.page = '1';
  pushUrl(); syncControls(); loadLots();
}
function resetFilters() {
  for (const k of FILTER_KEYS) delete st[k];
  st.status = DEFAULTS.status;
  set({});
}

// ── controls ────────────────────────────────────────────────────────────
function sortValue() {
  if (st.sort === 'bid' || st.sort === 'bids') return `${st.sort}:${st.dir || 'desc'}`;   // server default for bid sorts is desc
  return st.sort || 'ends';
}
function syncControls() {
  for (const sel of ['#feed-q', '#feed-rail-q']) { const el = $(sel); if (el && el.value !== (st.q || '')) el.value = st.q || ''; }
  $$('.seg[data-key]').forEach(seg => {
    const cur = st[seg.dataset.key] ?? '';
    $$('.seg-btn', seg).forEach(b => b.classList.toggle('is-active', (b.dataset.value || '') === String(cur)));
  });
  $('#feed-ending').value = st.ending_within || '';
  $('#feed-sort').value = sortValue();
  $('#feed-state').value = st.state || '';
  $('#feed-min-price').value = st.min_price || '';
  $('#feed-max-price').value = st.max_price || '';
  $$('#feed-chips .chip[data-cat]').forEach(c => {
    const on = (c.dataset.cat || '') === (st.category || '');
    c.classList.toggle('is-active', on); c.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  renderActiveFilters();
  applyView();
}
function applyView() {
  document.body.dataset.view = st.view;
  const btn = $('#feed-view-toggle');
  btn.textContent = st.view === 'map' ? 'List' : 'Map';
  btn.setAttribute('aria-pressed', st.view === 'map' ? 'true' : 'false');
}
function renderActiveFilters() {
  const chips = [];
  const add = (key, label, value = '') => chips.push({key, label, value});
  if (st.q) add('q', `“${st.q}”`);
  if (st.category) add('category', catLabel(st.category));
  if (st.state) add('state', st.state);
  if (st.max_bids === '0') add('max_bids', 'no bids'); else if (st.max_bids) add('max_bids', `≤ ${st.max_bids} bids`);
  if (st.ending_within) add('ending_within', `ending < ${st.ending_within >= 24 ? (st.ending_within / 24) + ' d' : st.ending_within + ' h'}`);
  if (st.status && st.status !== DEFAULTS.status) add('status', st.status === 'closed' ? 'closed' : 'live + closed', DEFAULTS.status);
  if (st.min_price) add('min_price', `min ${fmt.money(st.min_price)}`);
  if (st.max_price) add('max_price', `max ${fmt.money(st.max_price)}`);
  const host = $('#feed-filters');
  host.hidden = !chips.length;
  host.innerHTML = chips.map(c =>
    `<span class="filter-chip">${esc(c.label)}<button class="filter-chip-x" type="button" data-unset="${esc(c.key)}" data-value="${esc(c.value)}" aria-label="Remove ${esc(c.label)}">×</button></span>`
  ).join('') + (chips.length ? '<button class="clear-all" type="button" data-clear-all>Clear all</button>' : '');
}

// ── facets → chips, state select, stats ─────────────────────────────────
function chipsHtml(cats) {
  const total = cats.reduce((a, c) => a + Number(c.count || 0), 0);
  const chip = (value, label, count) =>
    `<button class="chip" type="button" data-cat="${esc(value)}" aria-pressed="false">${esc(label)} <span class="chip-count">${esc(fmt.int(count))}</span></button>`;
  return chip('', 'All', total) + cats.map(c => chip(c.value, catLabel(c.value), c.count)).join('');
}
async function loadChips() {
  facets = await load($('#feed-chips'), ({signal}) => api('/deals/api/facets', {signal}), {
    skeleton: 'pill', count: 6,
    isEmpty: () => false,
    render: f => chipsHtml(f.categories || []),
    errorMessage: () => "Couldn't load categories.",
  });
  if (!facets) return;
  const sel = $('#feed-state');
  sel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
  (facets.states || []).forEach(f => {
    const o = document.createElement('option'); o.value = f.value; o.textContent = `${f.value} (${fmt.int(f.count)})`; sel.appendChild(o);
  });
  const s = facets.stats || {};
  $$('#feed-stats [data-stat]').forEach(el => { const v = s[el.dataset.stat]; el.textContent = v == null ? '—' : fmt.int(v); });
  syncControls();
  renderCount();
}

// ── lots ────────────────────────────────────────────────────────────────
function renderCount() {
  const el = $('#feed-count');
  if (!lastBody) { el.textContent = ''; return; }
  const states = facets && facets.stats && facets.stats.states;
  el.innerHTML = `<strong>${esc(fmt.int(lastBody.total))}</strong> lot${lastBody.total === 1 ? '' : 's'}`
    + (states ? ` · ${esc(fmt.int(states))} states` : '') + (st.bbox ? ' · in map view' : '');
}
function renderMore() {
  const btn = $('#feed-more');
  const b = lastBody;
  const remaining = b ? b.total - b.page * b.per_page : 0;
  btn.hidden = !(remaining > 0);
  if (remaining > 0) btn.textContent = `Load more (${fmt.int(remaining)} remaining)`;
}
function emptyArgs() {
  const q = st.q || '', cat = st.category ? catLabel(st.category) : '';
  const narrowed = NARROWING_KEYS.some(k => st[k]);
  const title = q ? `No matches for “${q}”${cat ? ' in ' + cat : ''}` : cat ? `Nothing live in ${cat} right now` : 'No lots match these filters';
  let cta;
  if (narrowed) cta = {label: q || cat ? 'Search everywhere' : 'Clear filters', onClick: () => set(Object.fromEntries(NARROWING_KEYS.map(k => [k, ''])))};
  else if (q) cta = {label: 'Clear search', onClick: () => set({q: ''})};
  else cta = {label: 'Clear filters', onClick: resetFilters};
  return {glyph: '⌕', title, body: 'Try fewer filters, or search everywhere.', cta};
}
async function loadLots({append = false} = {}) {
  const grid = $('#feed-grid');
  const body = await load(grid, ({signal}) => api('/deals/api/lots?' + apiQs(), {signal}), {
    skeleton: 'card', count: 8, keepOld: append,
    isEmpty: b => !b.total,
    empty: emptyArgs(),
    render: b => {
      const html = b.rows.map(r => card(r)).join('');
      if (append) { grid.insertAdjacentHTML('beforeend', html); return null; }
      return html;
    },
    errorMessage: err => "Couldn't load lots. " + (err.status ? `The server said ${err.status}.` : 'The database didn\'t answer in 15 s.'),
  });
  if (!body) { $('#feed-more').hidden = true; return undefined; }
  lastBody = body;
  if (String(body.page) !== st.page) { st.page = String(body.page); pushUrl(); }
  renderCount(); renderMore(); tickTimers(grid);
  document.dispatchEvent(new CustomEvent('feed:params', {detail: {
    params: filterParams(), total: body.total, rows: body.rows, page: body.page, view: st.view, bbox: st.bbox,
  }}));
  return body;
}
async function restoreDepth() {
  const target = Math.min(Math.max(1, Number(st.page) || 1), MAX_RESTORE_PAGES);
  st.page = '1';
  const first = await loadLots();
  if (!first) return;
  for (let p = 2; p <= target; p++) {
    st.page = String(p);
    const b = await loadLots({append: true});
    if (!b || !b.rows.length) break;
  }
  pushUrl();
}

// ── wiring ──────────────────────────────────────────────────────────────
let qTimer = null;
const onSearchInput = e => { clearTimeout(qTimer); qTimer = setTimeout(() => set({q: e.target.value.trim()}), 300); };
for (const sel of ['#feed-q', '#feed-rail-q']) $(sel).addEventListener('input', onSearchInput);
$('#feed-search').addEventListener('submit', e => { e.preventDefault(); clearTimeout(qTimer); set({q: $('#feed-q').value.trim()}); });
$('#feed-rail-q').addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(qTimer); set({q: e.target.value.trim()}); } });
$$('[data-feed-search-open]').forEach(b => b.addEventListener('click', () => setTimeout(() => $('#feed-rail-q').focus(), 200)));

$$('.seg[data-key]').forEach(seg => seg.addEventListener('click', e => {
  const b = e.target.closest('.seg-btn'); if (!b) return;
  set({[seg.dataset.key]: b.dataset.value});
}));
$('#feed-ending').addEventListener('change', e => set({ending_within: e.target.value}));
$('#feed-sort').addEventListener('change', e => { const [sort, dir] = e.target.value.split(':'); set({sort, dir: dir || ''}); });
$('#feed-state').addEventListener('change', e => set({state: e.target.value}));
let priceTimer = null;
for (const sel of ['#feed-min-price', '#feed-max-price']) $(sel).addEventListener('input', () => {
  clearTimeout(priceTimer);
  priceTimer = setTimeout(() => set({min_price: $('#feed-min-price').value, max_price: $('#feed-max-price').value}), 400);
});
$('#feed-clear').addEventListener('click', resetFilters);
$('#feed-chips').addEventListener('click', e => { const c = e.target.closest('.chip[data-cat]'); if (c) set({category: c.dataset.cat}); });
$('#feed-filters').addEventListener('click', e => {
  const x = e.target.closest('[data-unset]');
  if (x) { set({[x.dataset.unset]: x.dataset.value || ''}); return; }
  if (e.target.closest('[data-clear-all]')) resetFilters();
});
$('#feed-more').addEventListener('click', e => pending(e.currentTarget, 'Loading…', () => {
  st.page = String((Number(st.page) || 1) + 1);
  pushUrl();
  return loadLots({append: true});
}).then(renderMore));   // pending() restores the button's old label on settle; re-render the countdown after it
$('#feed-view-toggle').addEventListener('click', () => {
  st.view = st.view === 'map' ? 'list' : 'map';
  pushUrl(); applyView();
  document.dispatchEvent(new CustomEvent('feed:view', {detail: {view: st.view}}));
  if (st.view === 'list' && st.bbox) { st.bbox = ''; set({}); }
});
document.addEventListener('feed:bbox', e => {
  const bbox = (e.detail && e.detail.bbox) || '';
  if (bbox === st.bbox) return;
  st.bbox = bbox;
  set({});
});

// about block: collapsed by default; remember when the visitor opens it
try {
  const about = $('#feed-about');
  about.open = localStorage.getItem('feed.about') === 'open';
  about.addEventListener('toggle', () => { try { localStorage.setItem('feed.about', about.open ? 'open' : 'closed'); } catch (_) {} });
} catch (_) {}

syncControls();
document.dispatchEvent(new CustomEvent('feed:view', {detail: {view: st.view}}));
loadChips();
restoreDepth();
setInterval(() => tickTimers($('#feed-grid')), 30000);
