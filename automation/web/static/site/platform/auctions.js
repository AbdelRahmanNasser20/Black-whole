// static/site/platform/auctions.js — the Auctions and Favorites sections of the /platform window.
//
//   Auctions   REAL   one filter bar, a result list and a map, kept in sync: every filter change
//                     re-reads the list AND redraws the pins; a row focuses its pin, a pin its row.
//     primary   GET /platform/api/auctions   (server-side filters, items carry lat/lng)
//     fallback  GET /deals/api/lots + /deals/api/pins + /deals/api/facets — the public endpoints the
//               page used before; used when the primary route is missing or fails on first contact.
//               The fallback feed is one site, so the Site filter stays empty there.
//     Items with no coordinates are listed without a pin. Nothing is filtered or widened in the browser:
//     the endpoint decides what an auction is and what is public.
//   Favorites  the VISITOR's own stars. Kept in localStorage under STAR_KEY inside try/catch, so the
//              page works when storage is unavailable (stars then last until reload). The operator's
//              real favorites are private: nothing here reads them.
// Read-only: no writes to the server. The map library is the storefront's own (/static/admin_map.js).
import { api, esc, fmt } from '/static/ui/state.js';

const PRIMARY_URL = '/platform/api/auctions';
const LOTS_URL = '/deals/api/lots';
const PINS_URL = '/deals/api/pins';
const FACETS_URL = '/deals/api/facets';
const STAR_KEY = 'pf.stars.v1';
const PER_PAGE = 50;                           // one of the fallback's own per_page choices
const TIMEOUT_MS = { open: 20000, closed: 60000 };
const HOUR_MS = 3600 * 1000;
const ENDING_HOURS = { '24h': 24, '7d': 168 };

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const words = (v) => String(v || '').replace(/_/g, ' ');
const cap = (v) => words(v).replace(/^./, (c) => c.toUpperCase());

function money(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const d = Number.isInteger(n) ? 0 : 2;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
// Only http(s) and same-site paths ever reach an href.
const safeUrl = (u) => (typeof u === 'string' && /^(https?:\/\/|\/(?!\/))/.test(u) ? u : null);

/* ── favorites: the visitor's stars ──────────────────────────────────────── */
const STAR_FIELDS = ['id', 'site', 'site_name', 'title', 'category', 'city', 'state', 'lat', 'lng',
  'current_bid', 'bid_count', 'ends_at', 'status', 'final_price', 'closed_at', 'url'];
const starred = new Map();                     // id → the auction as it looked when starred
const starListeners = [];

function readStars() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STAR_KEY) || '{}');
    if (saved && typeof saved === 'object') {
      for (const [id, it] of Object.entries(saved)) if (it && typeof it === 'object') starred.set(id, { ...it, id });
    }
  } catch { /* storage blocked or corrupt: start with no stars */ }
}
function writeStars() {
  try { window.localStorage.setItem(STAR_KEY, JSON.stringify(Object.fromEntries(starred))); }
  catch { /* storage blocked or full: the stars still work until reload */ }
}
function toggleStar(item) {
  if (starred.has(item.id)) starred.delete(item.id);
  else starred.set(item.id, Object.fromEntries(STAR_FIELDS.map((k) => [k, item[k] ?? null])));
  writeStars();
  starListeners.forEach((cb) => cb());
}

/* ── state ───────────────────────────────────────────────────────────────── */
const f = { q: '', site: '', category: '', state: '', status: 'open', noBids: false, maxBid: '', ending: '', sort: 'ending' };
const view = {
  mode: null,            // null until the first answer, then 'primary' | 'fallback'
  items: [], total: 0, page: 1, pages: 1,
  pins: null,            // fallback only: every matching pin, when /deals/api/pins has answered
  selected: null,
  favQ: '',
  sawOpen: false,        // the endpoint has reported at least one open auction
};
let hooks = { showSection() {} };
let listAbort = null, pinsAbort = null;
let map = null, L = null, cluster = null, mapState = 'idle';   // idle | loading | ready | error
const markers = new Map();
const byId = new Map();                        // every auction on screen (list, pins, favorites)

const filtered = () => !!(f.q || f.site || f.category || f.state || f.noBids || f.maxBid || f.ending);
const isDeals = () => f.status === 'open' && f.noBids && f.ending === '24h';

/* ── one shape for both feeds ────────────────────────────────────────────── */
function fromPrimary(it) {
  return { ...it, id: String(it.id), status: it.status === 'closed' ? 'closed' : 'open' };
}
function fromDeal(r) {                         // a /deals/api/lots row or a /deals/api/pins point
  const closed = !!r.outcome_complete;
  return {
    id: `${r.asset_id}-${r.account_id}-${r.auction_id}`,
    site: 'govdeals', site_name: 'GovDeals', title: r.title, category: r.canonical_category || null,
    city: r.city || null, state: r.state || null, lat: r.lat ?? null, lng: r.lng ?? null,
    current_bid: r.current_bid ?? null,
    bid_count: (closed && r.final_bid_count != null ? r.final_bid_count : r.bid_count) ?? null,
    ends_at: r.end_utc || null, status: closed ? 'closed' : 'open',
    final_price: closed ? (r.final_bid ?? null) : null, closed_at: closed ? (r.end_utc || null) : null,
    url: r.govdeals_url || (r.asset_id != null ? `https://www.govdeals.com/en/asset/${r.asset_id}/${r.account_id}` : null),
  };
}

function primaryQuery(page) {
  const p = new URLSearchParams({ status: f.status, sort: f.sort, page: String(page), per_page: String(PER_PAGE) });
  if (f.q) p.set('q', f.q);
  if (f.site) p.set('site', f.site);
  if (f.category) p.set('category', f.category);
  if (f.state) p.set('state', f.state);
  if (f.noBids) p.set('no_bids', '1');
  if (f.maxBid) p.set('max_bid', f.maxBid);
  if (f.ending && f.status === 'open') p.set('ending', f.ending);
  return p.toString();
}
function dealsQuery(page) {
  const closed = f.status === 'closed';
  const [sort, dir] = { ending: ['ends', closed ? 'desc' : ''], bid_low: ['bid', 'asc'], bids: ['bids', 'desc'], newest: ['newest', ''] }[f.sort];
  const p = new URLSearchParams({ status: closed ? 'closed' : 'active' });
  if (page) { p.set('sort', sort); if (dir) p.set('dir', dir); p.set('page', String(page)); p.set('per_page', String(PER_PAGE)); }
  if (f.q) p.set('q', f.q);
  if (f.category) p.set('category', f.category);
  if (f.state) p.set('state', f.state);
  if (f.noBids) p.set('max_bids', '0');
  if (f.maxBid) p.set('max_price', f.maxBid);
  if (f.ending && !closed) p.set('ending_within', String(ENDING_HOURS[f.ending]));
  return p.toString();
}

/* ── facets → the three selects ──────────────────────────────────────────── */
function fillFacet(sel, rows, current, label) {
  if (!Array.isArray(rows) || !rows.length) return;
  sel.querySelectorAll('option:not([value=""])').forEach((o) => o.remove());
  for (const row of rows) {
    const value = String(row.key ?? row.value ?? '');
    if (!value) continue;
    const o = document.createElement('option');
    o.value = value;
    o.textContent = `${row.name ?? row.label ?? label(value)}${row.count != null ? ` (${fmt.int(row.count)})` : ''}`;
    sel.appendChild(o);
  }
  sel.value = current;
  if (sel.value !== current) {                 // the current pick has no rows any more: keep it selectable
    const o = document.createElement('option'); o.value = current; o.textContent = label(current);
    sel.appendChild(o); sel.value = current;
  }
  sel.disabled = false;
}
function paintFacets(facets) {
  if (!facets) return;
  fillFacet($('#pf-au-site'), facets.sites, f.site, cap);
  fillFacet($('#pf-au-category'), facets.categories, f.category, cap);
  fillFacet($('#pf-au-state'), facets.states, f.state, (v) => v);
}
async function loadFallbackFacets() {
  $('#pf-au-site').disabled = true;            // the fallback feed is a single site
  try { paintFacets(await api(FACETS_URL)); } catch { /* the selects stay at "Any …" */ }
}

/* ── rows ────────────────────────────────────────────────────────────────── */
function rowHtml(it) {
  const closed = it.status === 'closed';
  const price = closed && it.final_price != null ? it.final_price : it.current_bid;
  const bids = it.bid_count == null ? null : Number(it.bid_count) || 0;
  const place = [it.city, it.state].filter(Boolean).join(', ') || 'Location not listed';
  const on = starred.has(it.id);
  const url = safeUrl(it.url);
  const site = esc(it.site_name || cap(it.site) || 'Source');
  const noPin = it.lat == null || it.lng == null;
  let when;
  if (closed) {
    const at = it.closed_at || it.ends_at;
    when = `<span class="pf-au-when">Closed${at ? ' ' + esc(new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })) : ''}</span>`;
  } else {
    const left = it.ends_at ? new Date(it.ends_at).getTime() - Date.now() : NaN;
    const urgent = Number.isFinite(left) && left > 0 && left < HOUR_MS;
    when = `<span class="pf-au-when${urgent ? ' is-urgent' : ''}" data-ends="${esc(it.ends_at || '')}">${esc(fmt.endsIn(it.ends_at))}</span>`;
  }
  return `<li class="pf-au-row${it.id === view.selected ? ' is-selected' : ''}" data-id="${esc(it.id)}">
    <button class="pf-star" type="button" data-star aria-pressed="${on}" aria-label="${on ? 'Remove from favorites' : 'Add to favorites'}">★</button>
    <button class="pf-au-main" type="button" data-focus${noPin ? ' title="No map location for this auction"' : ''}>
      <span class="pf-au-title">${esc(it.title || 'Untitled lot')}</span>
      <span class="pf-au-sub">${esc(place)}${it.category ? `<span class="pf-au-cat">${esc(cap(it.category))}</span>` : ''}</span>
    </button>
    <span class="pf-au-num">
      <span class="pf-au-bid">${esc(money(price))}</span>
      <span class="pf-au-bids${bids === 0 ? ' is-zero' : ''}">${bids == null ? '' : bids === 0 ? 'no bids' : `${esc(fmt.int(bids))} bid${bids === 1 ? '' : 's'}`}</span>
    </span>
    <span class="pf-au-num">
      ${when}
      ${url ? `<a class="pf-au-site" href="${esc(url)}" target="_blank" rel="noopener">${site}</a>` : `<span class="pf-au-site">${site}</span>`}
    </span>
  </li>`;
}
const msgHtml = (html) => `<li class="pf-row-msg">${html}</li>`;

function emptyHtml() {
  if (filtered()) return 'No auctions match. <button class="pf-btn" type="button" data-au-reset>Clear filters</button>';
  if (f.status === 'open') return 'No open auctions in the feed right now. <button class="pf-btn pf-btn--go" type="button" data-au-closed>Show closed</button>';
  return 'No closed auctions on record yet.';
}

function paintCount() {
  const n = view.items.length;
  $('#pf-au-count').textContent = view.total ? `${fmt.int(n)} of ${fmt.int(view.total)} ${f.status}` : '';
  $('#pf-n-auctions').textContent = view.total ? fmt.int(view.total) : '';
  $('#pf-au-more').hidden = !(view.total && view.page < view.pages);
}
// The claim on the pane follows the data: "Live data" only once the endpoint has reported open auctions.
function paintTruth() {
  const live = view.sawOpen;
  $('#pf-au-tag').textContent = live ? 'Live data' : 'Real data';
}
function paintControls() {
  $$('#pf-au-status .pf-seg-btn').forEach((b) => {
    const on = b.dataset.status === f.status;
    b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on));
  });
  const deals = $('#pf-au-deals');
  deals.classList.toggle('is-on', isDeals()); deals.setAttribute('aria-pressed', String(isDeals()));
  $('#pf-au-site').value = f.site; $('#pf-au-category').value = f.category; $('#pf-au-state').value = f.state;
  $('#pf-au-ending').value = f.ending; $('#pf-au-ending').disabled = f.status === 'closed';
  $('#pf-au-maxbid').value = f.maxBid; $('#pf-au-nobids').checked = f.noBids; $('#pf-au-sort').value = f.sort;
}

function renderList() {
  const list = $('#pf-au-list');
  list.innerHTML = view.items.length ? view.items.map(rowHtml).join('') : msgHtml(emptyHtml());
  paintCount();
}

function renderFavorites() {
  const list = $('#pf-fav-list');
  if (!list) return;
  const all = [...starred.values()];
  const q = view.favQ.toLowerCase();
  const rows = q ? all.filter((it) => `${it.title} ${it.city} ${it.state} ${it.site_name}`.toLowerCase().includes(q)) : all;
  rows.forEach((it) => byId.set(it.id, it));
  $('#pf-n-favorites').textContent = all.length ? fmt.int(all.length) : '';
  $('#pf-fav-count').textContent = all.length ? `${fmt.int(all.length)} starred` : '';
  if (!all.length) list.innerHTML = msgHtml('No favorites yet. Star an auction to keep it here. <button class="pf-btn pf-btn--go" type="button" data-fav-browse>Browse auctions</button>');
  else if (!rows.length) list.innerHTML = msgHtml('No favorites match this search.');
  else list.innerHTML = rows.map(rowHtml).join('');
}

/* ── the map ─────────────────────────────────────────────────────────────── */
function ensureAdminMap() {
  if (window.AdminMap) return Promise.resolve(window.AdminMap);
  const existing = document.querySelector('script[src^="/static/admin_map.js"]');
  return new Promise((resolve, reject) => {
    const s = existing || document.createElement('script');
    s.addEventListener('load', () => (window.AdminMap ? resolve(window.AdminMap) : reject(new Error('no AdminMap'))), { once: true });
    s.addEventListener('error', () => reject(new Error('map library failed to load')), { once: true });
    if (!existing) { s.src = '/static/admin_map.js'; document.head.appendChild(s); }
  });
}

function popupHtml(it) {
  const closed = it.status === 'closed';
  const price = closed && it.final_price != null ? it.final_price : it.current_bid;
  const n = Number(it.bid_count) || 0;
  const bids = it.bid_count == null ? '' : n ? `${fmt.int(n)} bid${n === 1 ? '' : 's'}` : 'no bids';
  const url = safeUrl(it.url);
  const name = esc(it.site_name || 'the source');
  return `<div class="pf-au-card">
    <div class="pf-au-card-title">${esc(it.title || 'Untitled lot')}</div>
    <div class="pf-au-card-line">${esc([it.city, it.state].filter(Boolean).join(', ') || 'Location not listed')}</div>
    <div class="pf-au-card-num">${esc(money(price))}${bids ? ', ' + esc(bids) : ''}${closed ? ', closed' : ', ' + esc(fmt.endsIn(it.ends_at))}</div>
    ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">Open on ${name}</a>` : ''}
  </div>`;
}

// Pins = what the list is showing. In fallback mode the pins endpoint answers the same filters in full,
// so once it has arrived the map shows every match, not only the rows loaded so far.
function drawPins({ fit = false } = {}) {
  if (mapState !== 'ready') return;
  const points = (view.pins || view.items).filter((it) => it.lat != null && it.lng != null);
  cluster.clearLayers(); markers.clear();
  const made = points.map((it) => {
    const m = L.marker([it.lat, it.lng], {
      icon: L.divIcon({ className: 'amap-pin pf-au-pin', iconSize: [14, 14] }), title: it.title || '',
    });
    m.bindPopup(() => popupHtml(it), { maxWidth: 260 });
    m.on('click', () => select(it.id, 'map'));
    markers.set(it.id, m);
    return m;
  });
  cluster.addLayers(made);
  if (fit && points.length) {
    map.leaflet.fitBounds(L.latLngBounds(points.map((it) => [it.lat, it.lng])).pad(0.1), { maxZoom: 9, animate: false });
  }
}

async function startMap() {
  const el = $('#pf-au-map');
  if (!el || mapState !== 'idle') return;
  mapState = 'loading';
  try {
    const AdminMap = await ensureAdminMap();
    map = await AdminMap.mount(el, { tiles: 'dark' });
  } catch {
    mapState = 'error';
    el.innerHTML = '<p class="pf-au-map-msg">The map did not load. The list still works.</p>';
    return;
  }
  L = window.L;
  cluster = L.markerClusterGroup({
    showCoverageOnHover: false, chunkedLoading: true, maxClusterRadius: 50,
    iconCreateFunction: (c) => {
      const n = c.getChildCount();
      const size = n >= 1000 ? 44 : n >= 100 ? 38 : 32;
      return L.divIcon({ html: n >= 1000 ? (Math.round(n / 100) / 10) + 'k' : String(n), className: 'amap-cluster', iconSize: L.point(size, size) });
    },
  });
  map.leaflet.addLayer(cluster);
  // the page scrolls past the window: the wheel zooms only once the map has been clicked
  map.leaflet.scrollWheelZoom.disable();
  map.leaflet.on('click', () => map.leaflet.scrollWheelZoom.enable());
  el.addEventListener('mouseleave', () => map.leaflet.scrollWheelZoom.disable());
  mapState = 'ready';
  el.dataset.ready = '1';
  drawPins({ fit: true });
}

// A row focuses its pin; a pin focuses its row.
function select(id, from) {
  view.selected = id;
  const row = $$('#pf-au-list .pf-au-row').find((li) => {
    const on = li.dataset.id === id;
    li.classList.toggle('is-selected', on);
    return on;
  });
  if (from === 'map') {
    const box = $('#pf-au-listwrap');
    if (row && box) box.scrollTop = row.offsetTop - (box.clientHeight - row.clientHeight) / 2;
    return;
  }
  if (mapState !== 'ready') return;
  const it = byId.get(id);
  const m = markers.get(id);
  if (m) cluster.zoomToShowLayer(m, () => m.openPopup());
  else if (it && it.lat != null && it.lng != null) {   // e.g. a favorite that is not in the current result
    map.leaflet.setView([it.lat, it.lng], Math.max(map.leaflet.getZoom(), 8), { animate: false });
    L.popup({ maxWidth: 260 }).setLatLng([it.lat, it.lng]).setContent(popupHtml(it)).openOn(map.leaflet);
  } else map.leaflet.closePopup();
  if (window.matchMedia('(max-width: 860px)').matches) $('#pf-au-map').scrollIntoView({ block: 'nearest' });
}

/* ── data ────────────────────────────────────────────────────────────────── */
async function fetchPrimary(page, signal) {
  const b = await api(`${PRIMARY_URL}?${primaryQuery(page)}`, { signal });
  if (!b || b.ok === false || !Array.isArray(b.items)) throw new Error('unexpected answer');
  const per = Number(b.per_page) || PER_PAGE, total = Number(b.total) || 0;
  return { items: b.items.map(fromPrimary), total, page: Number(b.page) || page, pages: Math.max(1, Math.ceil(total / per)), facets: b.facets };
}
async function fetchFallback(page, signal) {
  const b = await api(`${LOTS_URL}?${dealsQuery(page)}`, { signal });
  return { items: (b.rows || []).map(fromDeal), total: Number(b.total) || 0, page: Number(b.page) || page, pages: Number(b.pages) || 1 };
}
// Fallback only, open auctions only: the closed archive is far too large to pin.
async function loadFallbackPins() {
  pinsAbort?.abort();
  view.pins = null;
  if (f.status !== 'open') return;
  const ac = new AbortController(); pinsAbort = ac;
  try {
    const data = await api(`${PINS_URL}?${dealsQuery(0)}`, { signal: ac.signal });
    if (pinsAbort !== ac) return;
    const pins = (data.points || []).map(fromDeal);
    if (!pins.length) return;
    pins.forEach((it) => { if (!byId.has(it.id)) byId.set(it.id, it); });
    view.pins = pins;
    drawPins({ fit: true });
  } catch { /* the pins of the listed rows stay on the map */ }
}

async function load({ append = false } = {}) {
  const list = $('#pf-au-list');
  listAbort?.abort();
  const ac = new AbortController(); listAbort = ac;
  const page = append ? view.page + 1 : 1;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ac.abort(); }, TIMEOUT_MS[f.status]);
  list.setAttribute('aria-busy', 'true');
  if (!append && !view.items.length) list.innerHTML = msgHtml(f.status === 'closed' ? 'Searching closed auctions…' : 'Loading auctions…');
  try {
    let res = null;
    if (view.mode !== 'fallback') {
      try { res = await fetchPrimary(page, ac.signal); view.mode = 'primary'; }
      catch (err) {
        // A route that answered before and fails now is an error to show; one that never answered is absent.
        if (ac.signal.aborted || view.mode === 'primary') throw err;
        view.mode = 'fallback';
        loadFallbackFacets();
      }
    }
    if (!res) res = await fetchFallback(page, ac.signal);
    if (listAbort !== ac) return;
    view.items = append ? view.items.concat(res.items) : res.items;
    view.total = res.total; view.page = res.page; view.pages = res.pages;
    if (!append) { byId.clear(); starred.forEach((it, id) => byId.set(id, it)); view.selected = null; }
    view.items.forEach((it) => byId.set(it.id, it));
    if (f.status === 'open' && res.total > 0) view.sawOpen = true;
    paintFacets(res.facets);
    paintTruth();
    renderList();
    if (!append && view.mode === 'fallback') { view.pins = null; loadFallbackPins(); }
    drawPins({ fit: !append });
    list.closest('.pf-pane').dataset.feed = view.mode;
  } catch (err) {
    if (listAbort !== ac) return;              // superseded by a newer request
    const why = timedOut ? 'The server took too long.' : err.status ? `The server said ${err.status}.` : 'The request did not go through.';
    if (!append) { view.items = []; view.total = 0; view.pins = null; paintCount(); drawPins(); }
    const msg = `Auctions did not load. ${esc(why)} <button class="pf-btn" type="button" data-au-retry>Try again</button>`;
    if (append) list.insertAdjacentHTML('beforeend', msgHtml(msg)); else list.innerHTML = msgHtml(msg);
  } finally {
    clearTimeout(timer);
    if (listAbort === ac) list.removeAttribute('aria-busy');
  }
}

function setFilters(patch) {
  Object.assign(f, patch);
  if (f.status === 'closed') f.ending = '';
  view.items = [];
  $('#pf-au-count').textContent = '';          // no stale count beside a list that is still loading
  paintControls();
  load();
}
const resetPatch = () => ({ q: '', site: '', category: '', state: '', noBids: false, maxBid: '', ending: '' });

/* ── wiring ──────────────────────────────────────────────────────────────── */
function onRowClick(e, { inFavorites = false } = {}) {
  const li = e.target.closest('.pf-au-row');
  if (!li) return false;
  const it = byId.get(li.dataset.id);
  if (!it) return true;
  if (e.target.closest('[data-star]')) { toggleStar(it); return true; }
  if (e.target.closest('[data-focus]')) {
    if (inFavorites) hooks.showSection('auctions');
    select(it.id, 'list');
  }
  return true;
}

function wire() {
  $('#pf-au-form').addEventListener('submit', (e) => e.preventDefault());
  $('#pf-au-status').addEventListener('click', (e) => {
    const b = e.target.closest('[data-status]');
    if (b && b.dataset.status !== f.status) setFilters({ status: b.dataset.status });
  });
  $('#pf-au-deals').addEventListener('click', () => setFilters(isDeals()
    ? { noBids: false, ending: '' }
    : { status: 'open', noBids: true, ending: '24h', sort: 'ending' }));
  for (const [sel, key] of [['#pf-au-site', 'site'], ['#pf-au-category', 'category'], ['#pf-au-state', 'state'], ['#pf-au-ending', 'ending'], ['#pf-au-sort', 'sort']]) {
    $(sel).addEventListener('change', (e) => setFilters({ [key]: e.target.value }));
  }
  $('#pf-au-nobids').addEventListener('change', (e) => setFilters({ noBids: e.target.checked }));
  let t;
  $('#pf-au-maxbid').addEventListener('input', (e) => {
    clearTimeout(t);
    const n = Number(e.target.value);
    t = setTimeout(() => setFilters({ maxBid: e.target.value !== '' && Number.isFinite(n) && n >= 0 ? String(n) : '' }), 400);
  });
  $('#pf-au-more').addEventListener('click', () => load({ append: true }));
  $('#pf-au-list').addEventListener('click', (e) => {
    if (e.target.closest('[data-au-retry]')) return load();
    if (e.target.closest('[data-au-closed]')) return setFilters({ status: 'closed' });
    if (e.target.closest('[data-au-reset]')) { hooks.clearSearch?.(); return setFilters(resetPatch()); }
    onRowClick(e);
  });
  $('#pf-fav-list').addEventListener('click', (e) => {
    if (e.target.closest('[data-fav-browse]')) return hooks.showSection('auctions');
    onRowClick(e, { inFavorites: true });
  });
  starListeners.push(() => {
    renderFavorites();
    $$('#pf-au-list .pf-au-row').forEach((li) => {
      const on = starred.has(li.dataset.id), b = $('[data-star]', li);
      b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-label', on ? 'Remove from favorites' : 'Add to favorites');
    });
  });
  setInterval(() => $$('.pf-au-list [data-ends]').forEach((el) => {
    const iso = el.dataset.ends; if (!iso) return;
    const left = new Date(iso).getTime() - Date.now();
    el.textContent = fmt.endsIn(iso);
    el.classList.toggle('is-urgent', left > 0 && left < HOUR_MS);
  }), 30000);
}

/* ── what the window shell calls ─────────────────────────────────────────── */
export function setQuery(q) { if (q !== f.q) setFilters({ q }); }
export function setFavoritesQuery(q) { view.favQ = q; renderFavorites(); }
export function shown() {                      // the Auctions pane is on screen (again)
  if (mapState === 'idle') startMap();
  else if (mapState === 'ready') map.invalidateSize();
}
export function init(shellHooks) {
  if (!$('#pf-au-list')) return;
  hooks = { ...hooks, ...shellHooks };
  readStars();
  starred.forEach((it, id) => byId.set(id, it));
  wire();
  paintControls();
  renderFavorites();
  load();
}
