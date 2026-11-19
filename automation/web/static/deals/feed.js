// static/deals/feed.js — public /deals feed (plan §6). ES module.
// The URL is the state (shareable links, back button works); `bbox` is transient and never written to it.
// Every read goes through UI.load; the only mutation (Load more) through UI.pending. No exclusions live here —
// public_deals.py owns the policy and the API rows never carry photos, verdicts, or distance.
//
// Contract with Workstream D (map.js), on `document`:
//   dispatch  feed:params  detail = {params: URLSearchParams (filters, no page/per_page/bbox/view), total, rows,
//                                    page, view, bbox}   — after every successful lots load
//   dispatch  feed:view    detail = {view: 'cards'|'table'|'map'} — after the switch or a URL-driven view change
//   listen    feed:bbox    detail = {bbox: 'S,W,N,E' | ''} — sets st.bbox, reloads from page 1
//   listen    feed:view-request  detail = {view} — another module asks for a view (the map's "Show list")
// Three views over the same filters (#feed-view-toggle): cards (Load more), table (a real pager, 50 rows a page),
// map. The view lives in the URL (wins) and in localStorage 'feed.view'. `view=map`: this file sets
// body.dataset.view and hides the grid; map.js does the rest.
import {load, api, pending, fmt, esc, renderEmpty} from '../ui/state.js';
import {tickTimers} from '../ui/card.js';
import {renderCards, renderTable, renderPager, lastPage} from './views.js';
import * as stars from '../ui/stars.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const KEYS = ['q', 'category', 'state', 'site', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price', 'near', 'radius', 'sort', 'dir', 'page', 'view'];
const DEFAULTS = {status: 'active', sort: 'ends', page: '1', view: 'cards'};
const VIEWS = ['cards', 'table', 'map'];
const VIEW_ALIAS = {list: 'cards'};   // links shared before the table existed
const VIEW_STORE = 'feed.view';
const FILTER_KEYS = ['q', 'category', 'state', 'site', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price', 'near', 'radius'];
const NARROWING_KEYS = ['category', 'state', 'site', 'max_bids', 'ending_within', 'min_price', 'max_price', 'near'];
const DEFAULT_RADIUS = '100';
const PER_PAGE = 24;          // divisible by 4/3/2 columns; the server clamps to its own choices and reports back
const TABLE_PER_PAGE = 50;    // one of the server's own page sizes
const MAX_RESTORE_PAGES = 10; // refresh restores `page` depth by appending pages 1..N, capped
const CAT_LABELS = {
  general_merchandise: 'General', vehicles: 'Vehicles', collectibles_jewelry: 'Collectibles & jewelry',
  computers_electronics: 'Computers & electronics', other: 'Other',
};
const catLabel = v => CAT_LABELS[v] || String(v || '').replace(/_/g, ' ');
// Site names come from the facets payload (server-owned list, see deals_sources.SITES); fall back to the key.
const siteLabel = v => ((facets && facets.sites) || []).find(s => s.value === v)?.name || String(v || '');

const st = Object.assign({}, DEFAULTS, Object.fromEntries(
  KEYS.map(k => [k, new URLSearchParams(location.search).get(k)]).filter(([, v]) => v)));
st.bbox = '';
function storedView() { try { return localStorage.getItem(VIEW_STORE) || ''; } catch (_) { return ''; } }
// The URL wins; with no view in the URL, the visitor's last choice; else cards.
st.view = (() => {
  const fromUrl = new URLSearchParams(location.search).get('view') || '';
  const want = VIEW_ALIAS[fromUrl] || fromUrl || storedView();
  return VIEWS.includes(want) ? want : DEFAULTS.view;
})();
const isTable = () => st.view === 'table';
let facets = null;
let lastBody = null;
let lastGoodPage = '';   // table: the last page that loaded, to go back to when a deeper one is refused

// ── URL / query helpers ─────────────────────────────────────────────────
function filterParams() {
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) if (st[k]) p.set(k, st[k]);
  if (!st.near) p.delete('radius');          // miles mean nothing without a place
  if (st.sort) p.set('sort', st.sort);
  if (st.dir) p.set('dir', st.dir);
  return p;
}
function apiQs() {
  const p = filterParams();
  if (st.bbox) p.set('bbox', st.bbox);
  p.set('page', st.page || '1');
  p.set('per_page', String(isTable() ? TABLE_PER_PAGE : PER_PAGE));
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
  $('#feed-site').value = st.site || '';
  $('#feed-min-price').value = st.min_price || '';
  $('#feed-max-price').value = st.max_price || '';
  if ($('#feed-near').value !== (st.near || '')) $('#feed-near').value = st.near || '';
  $('#feed-radius').value = st.radius || DEFAULT_RADIUS;
  $('#feed-radius-out').value = `${st.radius || DEFAULT_RADIUS} mi`;
  $$('#feed-chips .chip[data-cat]').forEach(c => {
    const on = (c.dataset.cat || '') === (st.category || '');
    c.classList.toggle('is-active', on); c.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  renderActiveFilters();
  applyView();
}
function applyView() {
  document.body.dataset.view = st.view;
  $$('#feed-view-toggle [data-view]').forEach(b => {
    const on = b.dataset.view === st.view;
    b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
function setView(view) {
  if (!VIEWS.includes(view) || view === st.view) return;
  const leftMap = st.view === 'map';
  st.view = view; st.page = '1';
  if (leftMap) st.bbox = '';                       // the map's viewport filter does not follow you out of the map
  try { localStorage.setItem(VIEW_STORE, view); } catch (_) {}
  pushUrl(); applyView();
  document.dispatchEvent(new CustomEvent('feed:view', {detail: {view}}));
  loadLots();                                      // the target element and the page size differ per view
}
function renderActiveFilters() {
  const chips = [];
  const add = (key, label, value = '') => chips.push({key, label, value});
  if (st.q) add('q', `“${st.q}”`);
  if (st.category) add('category', catLabel(st.category));
  if (st.state) add('state', st.state);
  if (st.site) add('site', siteLabel(st.site));
  if (st.max_bids === '0') add('max_bids', 'no bids'); else if (st.max_bids) add('max_bids', `≤ ${st.max_bids} bids`);
  if (st.ending_within) add('ending_within', `ending < ${st.ending_within >= 24 ? (st.ending_within / 24) + ' d' : st.ending_within + ' h'}`);
  if (st.status && st.status !== DEFAULTS.status) add('status', st.status === 'closed' ? 'closed' : 'live + closed', DEFAULTS.status);
  if (st.min_price) add('min_price', `min ${fmt.money(st.min_price)}`);
  if (st.max_price) add('max_price', `max ${fmt.money(st.max_price)}`);
  if (st.near) add('near', `within ${st.radius || DEFAULT_RADIUS} mi of ${st.near}`);
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
  const siteSel = $('#feed-site');
  siteSel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
  (facets.sites || []).forEach(f => {
    const o = document.createElement('option'); o.value = f.value; o.textContent = `${f.name || f.value} (${fmt.int(f.count)})`; siteSel.appendChild(o);
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
  const near = lastBody.near;
  if (near && near.resolved === false) { el.innerHTML = `<strong>0</strong> lots · we could not place “${esc(near.label)}” — try a 5-digit ZIP`; return; }
  el.innerHTML = `<strong>${esc(fmt.int(lastBody.total))}</strong> lot${lastBody.total === 1 ? '' : 's'}`
    + (states ? ` · ${esc(fmt.int(states))} states` : '') + (near ? ` · within ${esc(String(near.radius))} mi of ${esc(near.label)}` : '') + (st.bbox ? ' · in map view' : '');
}
function renderMore() {
  const btn = $('#feed-more');
  const b = lastBody;
  const pager = $('#feed-pager');
  pager.innerHTML = isTable() && b ? renderPager(b) : '';
  pager.hidden = !pager.innerHTML;
  const remaining = b && !isTable() ? b.total - b.page * b.per_page : 0;
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
// ── starred lots (this browser) ─────────────────────────────────────────
let favMode = false;
function paintFavCount() { $('#feed-fav-count').textContent = String(stars.count()); }
function renderFavs() {
  const grid = $('#feed-grid');
  const rows = stars.list();
  $('#feed-table').innerHTML = ''; $('#feed-pager').hidden = true; $('#feed-more').hidden = true;
  $('#feed-fav-note').hidden = false;
  if (!rows.length) {
    renderEmpty(grid, {glyph: '☆', title: 'No starred lots yet', body: 'Press ☆ on any lot and it stays here, in this browser.',
      cta: {label: 'Back to the feed', onClick: () => setFavMode(false)}});
  } else {
    grid.dataset.state = 'ready';
    grid.innerHTML = renderCards(rows);
    stars.paint(grid); tickTimers(grid);
  }
  $('#feed-count').innerHTML = `<strong>${esc(fmt.int(rows.length))}</strong> starred lot${rows.length === 1 ? '' : 's'}`;
}
function setFavMode(on) {
  favMode = on;
  $('#feed-fav-toggle').setAttribute('aria-pressed', on ? 'true' : 'false');
  document.body.dataset.fav = on ? '1' : '';
  if (on) { if (st.view === 'map') setView('cards'); renderFavs(); }
  else { $('#feed-fav-note').hidden = true; loadLots(); }
}
$('#feed-fav-toggle').addEventListener('click', () => setFavMode(!favMode));
stars.onChange(() => { paintFavCount(); if (favMode) renderFavs(); });
paintFavCount();

// ── the results box: it scrolls on its own, from wherever it starts on the page ──
function sizeScrollBox() {
  const box = $('#feed-scroll'); if (!box) return;
  const top = Math.max(0, Math.round(box.getBoundingClientRect().top + window.scrollY));
  document.documentElement.style.setProperty('--feed-scroll-top', `${top + 8}px`);
}
window.addEventListener('resize', sizeScrollBox);
new ResizeObserver(sizeScrollBox).observe(document.body);

async function loadLots({append = false, clamped = false} = {}) {
  if (favMode) { renderFavs(); return undefined; }
  let refused = false;
  const view = st.view;                            // an answer that lands after a view switch is dropped
  const table = isTable();
  if (table) append = false;                       // a table page replaces the last one
  const grid = table ? $('#feed-table') : $('#feed-grid');
  const body = await load(grid, ({signal}) => api('/deals/api/lots?' + apiQs(), {signal}), {
    skeleton: table ? 'row' : 'card', count: 8, keepOld: append,
    isEmpty: b => !b.total,
    empty: emptyArgs(),
    render: b => {
      if (table) return renderTable(b.rows);
      const html = renderCards(b.rows);
      if (append) { grid.insertAdjacentHTML('beforeend', html); return null; }
      return html;
    },
    // 401 = the anonymous depth cap (scrape guard): deeper pages need a free account.
    errorMessage: err => err.status === 401 ? 'Sign in to see more lots.'
      : "Couldn't load lots. " + (err.status ? `The server said ${err.status}.` : 'The database didn\'t answer in 15 s.'),
    onError: err => {
      if (err.status === 401) { refused = true; grid.insertAdjacentHTML('beforeend', '<p><a class="btn btn-primary btn-small" href="/platform/signup">Sign in to see more</a></p>'); }
    },
  });
  if (st.view !== view) return undefined;          // the visitor moved on: this view's state is not ours to write
  if (!body) {
    // Table, deeper page refused (the anonymous depth cap): go back to the last page that loaded
    // (page 1 for a deep link) so Prev/Next stay on screen, and say why.
    const back = lastGoodPage || '1';
    if (table && refused && st.page !== back) {
      st.page = back; pushUrl();
      const again = await loadLots();
      if (again && st.view === view) $('#feed-pager').insertAdjacentHTML('beforeend',
        ' <span class="feed-pager-where">Sign in to see more lots.</span>');
      return again;
    }
    $('#feed-more').hidden = true; $('#feed-pager').hidden = true; return undefined;
  }
  // A page past the end (a stale link): open the last page this viewer can read instead of an empty table.
  if (table && body.total > 0 && (body.page > body.pages || body.page > lastPage(body)) && !clamped) {
    st.page = String(lastPage(body)); pushUrl();
    return loadLots({clamped: true});
  }
  lastBody = body;
  if (table) lastGoodPage = String(body.page);
  if (String(body.page) !== st.page) { st.page = String(body.page); pushUrl(); }
  renderCount(); renderMore(); tickTimers(grid); stars.paint(grid);
  document.dispatchEvent(new CustomEvent('feed:params', {detail: {
    params: filterParams(), total: body.total, rows: body.rows, page: body.page, view: st.view, bbox: st.bbox,
  }}));
  return body;
}
async function restoreDepth() {
  if (isTable()) { await loadLots(); return; }     // the table opens on its own page number
  const target = Math.min(Math.max(1, Number(st.page) || 1), MAX_RESTORE_PAGES);
  const view = st.view;
  st.page = '1';
  const first = await loadLots();
  if (!first) return;
  for (let p = 2; p <= target; p++) {
    if (st.view !== view) return;                  // the visitor switched view during the restore
    st.page = String(p);
    const b = await loadLots({append: true});
    if (!b || !b.rows.length) break;
  }
  if (st.view !== view) return;
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
$('#feed-site').addEventListener('change', e => set({site: e.target.value}));
let priceTimer = null;
for (const sel of ['#feed-min-price', '#feed-max-price']) $(sel).addEventListener('input', () => {
  clearTimeout(priceTimer);
  priceTimer = setTimeout(() => set({min_price: $('#feed-min-price').value, max_price: $('#feed-max-price').value}), 400);
});
let nearTimer = null;
$('#feed-near').addEventListener('input', e => { clearTimeout(nearTimer); nearTimer = setTimeout(() => set({near: e.target.value.trim(), radius: $('#feed-radius').value}), 500); });
$('#feed-near').addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(nearTimer); set({near: e.target.value.trim(), radius: $('#feed-radius').value}); } });
let radiusTimer = null;
$('#feed-radius').addEventListener('input', e => {
  $('#feed-radius-out').value = `${e.target.value} mi`;
  clearTimeout(radiusTimer);
  radiusTimer = setTimeout(() => { if (st.near) set({radius: e.target.value}); else st.radius = e.target.value; }, 350);
});
$('#feed-clear').addEventListener('click', resetFilters);
// Filter panel (under the control row). Open when the page lands with a filter set, else as last left.
const panel = $('#feed-rail');
function showPanel(open) {
  panel.hidden = !open;
  $$('[data-filters-toggle]').forEach(b => b.setAttribute('aria-expanded', open ? 'true' : 'false'));
  try { localStorage.setItem('feed.filters', open ? 'open' : 'closed'); } catch (_) {}
}
$$('[data-filters-toggle]').forEach(b => b.addEventListener('click', () => showPanel(panel.hidden)));
$$('[data-filters-close]').forEach(b => b.addEventListener('click', () => showPanel(false)));
showPanel((() => { try { return localStorage.getItem('feed.filters') === 'open'; } catch (_) { return false; } })()
  || NARROWING_KEYS.some(k => st[k]));
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
$('#feed-view-toggle').addEventListener('click', e => {
  const b = e.target.closest('[data-view]');
  if (b) setView(b.dataset.view);
});
document.addEventListener('feed:view-request', e => setView((e.detail && e.detail.view) || DEFAULTS.view));
$('#feed-pager').addEventListener('click', e => {
  const b = e.target.closest('[data-page]');
  if (!b || b.disabled) return;
  st.page = String(Math.max(1, Number(b.dataset.page) || 1));
  pushUrl();
  loadLots().then(() => $('#feed-controls').scrollIntoView({block: 'start'}));
});
document.addEventListener('feed:bbox', e => {
  const bbox = (e.detail && e.detail.bbox) || '';
  if (bbox === st.bbox) return;
  if (bbox && st.view !== 'map') return;           // a late viewport event after the visitor left the map
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
setInterval(() => { tickTimers($('#feed-grid')); tickTimers($('#feed-table')); }, 30000);
