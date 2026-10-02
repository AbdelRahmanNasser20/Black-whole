// static/site/platform/map.js — the combined map on GET /platform.
// Three layers on one dark map:
//   · auctions = REAL, from the existing public endpoint /deals/api/pins (automation/web/public_deals.py owns
//                every exclusion). Only the fields that endpoint returns are read. Loaded once, when the
//                section scrolls into view — the query can take many seconds and may return nothing.
//   · lots + buyers = SAMPLE, invented: inventory.sample.json + crm.sample.json joined by id with the
//                city-level coordinates in map.sample.json.
// Click a sample lot → a radius ring + the sample buyers inside it, the lot↔buyer match the CRM map does.
// Read-only: no writes, no storage. The map library is the storefront's own (/static/admin_map.js).
import { api, esc, fmt } from '/static/ui/state.js';

const PINS_URL = '/deals/api/pins';
const SAMPLE = '/static/site/platform/';
const PINS_TIMEOUT_MS = 60000;
const MI = 1609.344;   // metres

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  q: '',
  on: { auctions: true, lots: true, buyers: true },
  deals: false,          // "great deals" = open auctions nobody has bid on yet
  radius: 300,
  picked: null,          // lot_id
  auctions: [], lots: [], buyers: [],
  auctionsState: 'idle', // idle | loading | ready | empty | error
  capped: false,
};
let map = null, L = null, lotLayer = null, buyerLayer = null, ring = null;
const lotMarkers = new Map(), buyerMarkers = new Map();

/* ── helpers ─────────────────────────────────────────────────────────────── */
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

function miles(a, b) {
  const rad = (d) => d * Math.PI / 180;
  const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(h));
}
const place = (o) => [o.city, o.state].filter(Boolean).join(', ');
const hit = (hay) => !state.q || hay.toLowerCase().includes(state.q);
const noBids = (pt) => (pt.bid_count ?? 0) === 0;
const plural = (n, word) => `${fmt.int(n)} ${word}${n === 1 ? '' : 's'}`;

const shownAuctions = () => (state.on.auctions
  ? state.auctions.filter((pt) => (!state.deals || noBids(pt)) && hit(`${pt.title || ''} ${pt.city || ''} ${pt.state || ''}`))
  : []);
const shownLots = () => (state.on.lots
  ? state.lots.filter((l) => hit(`${l.lot_id} ${l.title} ${l.city} ${l.state} ${l.status}`)) : []);
const shownBuyers = () => (state.on.buyers
  ? state.buyers.filter((b) => hit(`${b.name} ${b.about} ${b.wants} ${b.city} ${b.state}`)) : []);

/* ── cards ───────────────────────────────────────────────────────────────── */
const kind = (cls, label) => `<div class="pf-map-card-kind"><i class="pf-map-dot pf-map-dot--${cls}"></i>${label}</div>`;

function auctionCard(pt) {
  const bids = pt.bid_count ?? 0;
  return `<div class="pf-map-card">${kind('auctions', 'Auction · real')}
    <div class="pf-map-card-title">${esc(pt.title || 'Untitled lot')}</div>
    <div class="pf-map-card-line">${esc(place(pt) || '—')}</div>
    <div class="pf-map-card-num">${esc(fmt.money(pt.current_bid))} · ${bids ? esc(plural(bids, 'bid')) : 'no bids'} · ${esc(fmt.endsIn(pt.end_utc))}</div>
    <a href="/deals/${esc(pt.asset_id)}/${esc(pt.account_id)}/${esc(pt.auction_id)}">View lot</a>
  </div>`;
}

function lotCard(lot) {
  const m = state.picked === lot.lot_id ? matchFor(lot) : null;
  return `<div class="pf-map-card">${kind('lots', 'Lot · sample')}
    <div class="pf-map-card-title">${esc(lot.title)}</div>
    <div class="pf-map-card-line">${esc(lot.lot_id)} · ${esc(place(lot))}</div>
    <div class="pf-map-card-num">${esc(fmt.int(lot.quantity_remaining))} left · ${esc(fmt.money(lot.price_per_unit))} / ${esc(lot.unit)}</div>
    ${m ? `<div class="pf-map-card-hit">${esc(plural(m.length, 'buyer'))} within ${state.radius} mi</div>` : ''}
  </div>`;
}

function buyerCard(b) {
  const want = b.quantity_wanted ? `${fmt.int(b.quantity_wanted)} ${b.wants}` : b.wants;
  return `<div class="pf-map-card">${kind('buyers', 'Buyer · sample')}
    <div class="pf-map-card-title">${esc(b.name)}</div>
    <div class="pf-map-card-line">${esc(b.about)} · ${esc(place(b))}</div>
    <div class="pf-map-card-num">Wants ${esc(want)}</div>
  </div>`;
}

/* ── drawing ─────────────────────────────────────────────────────────────── */
const icon = (cls, id, size) => L.divIcon({
  className: `pf-map-pin pf-map-pin--${cls}`, html: `<i data-id="${esc(id)}"></i>`, iconSize: [size, size],
});

function drawAuctions() {
  map.setPoints(shownAuctions().map((pt) => ({ lat: pt.lat, lng: pt.lng, title: pt.title || '', popup: auctionCard(pt) })));
}

function drawSample() {
  lotLayer.clearLayers(); buyerLayer.clearLayers(); lotMarkers.clear(); buyerMarkers.clear();
  const lots = shownLots();
  if (state.picked && !lots.some((l) => l.lot_id === state.picked)) state.picked = null;
  for (const b of shownBuyers()) {
    const m = L.marker([b.lat, b.lng], { icon: icon('buyer', b.id, 14), title: `${b.name} (sample buyer)` });
    m.bindPopup(buyerCard(b), { maxWidth: 260 });
    buyerLayer.addLayer(m); buyerMarkers.set(b.id, m);
  }
  for (const lot of lots) {
    const m = L.marker([lot.lat, lot.lng], { icon: icon('lot', lot.lot_id, 18), title: `${lot.lot_id} ${lot.title} (sample lot)`, zIndexOffset: 500 });
    m.bindPopup(lotCard(lot), { maxWidth: 260 });
    m.on('click', () => pick(lot.lot_id));
    lotLayer.addLayer(m); lotMarkers.set(lot.lot_id, m);
  }
  paintMatch();
}

function matchFor(lot) {
  return state.buyers
    .filter((b) => hit(`${b.name} ${b.about} ${b.wants} ${b.city} ${b.state}`))
    .map((b) => ({ ...b, mi: miles(lot, b) }))
    .filter((b) => b.mi <= state.radius)
    .sort((a, b) => a.mi - b.mi);
}

// Ring + highlight for the picked lot. Markers are kept (not rebuilt) so the open card stays open.
function paintMatch() {
  if (ring) { ring.remove(); ring = null; }
  const lot = state.picked && state.lots.find((l) => l.lot_id === state.picked);
  const box = $('#pf-map-match');
  const cls = (m, name, on) => { const el = m.getElement(); if (el) el.classList.toggle(name, on); };
  if (!lot) {
    lotMarkers.forEach((m) => { cls(m, 'is-picked', false); cls(m, 'is-dim', false); });
    buyerMarkers.forEach((m) => { cls(m, 'is-match', false); cls(m, 'is-dim', false); });
    box.textContent = 'Click a lot to see buyers in range.';
    return;
  }
  const match = matchFor(lot);
  const ids = new Set(match.map((b) => b.id));
  ring = L.circle([lot.lat, lot.lng], { radius: state.radius * MI, className: 'pf-map-ring', interactive: false }).addTo(map.leaflet);
  lotMarkers.forEach((m, id) => { cls(m, 'is-picked', id === lot.lot_id); cls(m, 'is-dim', id !== lot.lot_id); });
  buyerMarkers.forEach((m, id) => { cls(m, 'is-match', ids.has(id)); cls(m, 'is-dim', !ids.has(id)); });
  const marker = lotMarkers.get(lot.lot_id);
  if (marker) marker.setPopupContent(lotCard(lot));
  const names = match.map((b) => `${esc(b.name)} <span class="pf-map-n">${Math.round(b.mi)} mi</span>`).join(' · ');
  box.innerHTML = `<strong>${esc(lot.lot_id)}</strong> ${esc(place(lot))}: `
    + `<span class="pf-map-match-n" data-match-count="${match.length}">${esc(plural(match.length, 'buyer'))} within ${state.radius} mi</span>`
    + (names ? ` — ${names}` : '')
    + '<button class="pf-map-clear" type="button" data-clear>Clear</button>';
}

function fitAll() {
  const all = [...state.lots, ...state.buyers].map((p) => [p.lat, p.lng]);
  if (all.length) map.leaflet.fitBounds(L.latLngBounds(all).pad(0.08), { animate: false });
}

function pick(lotId) {
  state.picked = lotId;
  if (!state.on.buyers) { state.on.buyers = true; drawSample(); }   // the match is pointless with buyers hidden
  else paintMatch();
  paintChrome();
  if (ring) map.leaflet.fitBounds(ring.getBounds(), { padding: [16, 16], animate: false });
}

/* ── chrome: counts, toggles, status line ────────────────────────────────── */
function statusText() {
  if (state.auctionsState === 'loading') return 'Loading auctions…';
  if (state.auctionsState === 'empty') return 'No open auctions right now.';
  if (state.auctionsState === 'error') return 'Auctions unavailable right now.';
  if (state.auctionsState !== 'ready') return '';
  const free = state.auctions.filter(noBids).length;
  return `${plural(state.auctions.length, 'open auction')}${state.capped ? ' (first 5,000)' : ''} · ${fmt.int(free)} with no bids`;
}

function paintChrome() {
  const n = { auctions: shownAuctions().length, lots: shownLots().length, buyers: shownBuyers().length };
  for (const btn of $$('.pf-map-layer')) {
    const key = btn.dataset.layer;
    btn.classList.toggle('is-on', state.on[key]);
    btn.setAttribute('aria-pressed', String(state.on[key]));
    const out = $('[data-n]', btn);
    out.textContent = key === 'auctions' && state.auctionsState !== 'ready' ? (state.auctionsState === 'loading' ? '…' : '0') : fmt.int(n[key]);
  }
  const deals = $('#pf-map-deals');
  deals.disabled = state.auctionsState !== 'ready';
  deals.classList.toggle('is-on', state.deals);
  deals.setAttribute('aria-pressed', String(state.deals));
  for (const r of $$('.pf-map-r')) {
    const on = Number(r.dataset.mi) === state.radius;
    r.classList.toggle('is-on', on); r.setAttribute('aria-pressed', String(on));
  }
  $('#pf-map-status').textContent = statusText();
}

function redraw() { drawAuctions(); drawSample(); paintChrome(); }

/* ── data ────────────────────────────────────────────────────────────────── */
async function loadSample() {
  const [inv, crm, geo] = await Promise.all(
    ['inventory.sample.json', 'crm.sample.json', 'map.sample.json'].map((f) => api(SAMPLE + f)));
  const lotAt = new Map(geo.lots.map((g) => [g.lot_id, g]));
  const buyerAt = new Map(geo.buyers.map((g) => [g.id, g]));
  state.radius = geo.radius_mi || state.radius;
  state.lots = inv.lots.filter((l) => lotAt.has(l.lot_id)).map((l) => ({ ...l, lat: lotAt.get(l.lot_id).lat, lng: lotAt.get(l.lot_id).lng }));
  state.buyers = crm.buyers.filter((b) => buyerAt.has(b.id)).map((b) => ({ ...b, ...buyerAt.get(b.id) }));
}

async function loadAuctions() {
  state.auctionsState = 'loading'; paintChrome();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), PINS_TIMEOUT_MS);
  try {
    const data = await api(PINS_URL, { signal: ctl.signal });
    state.auctions = (data.points || []).filter((pt) => pt.lat != null && pt.lng != null);
    state.capped = !!data.capped;
    state.auctionsState = state.auctions.length ? 'ready' : 'empty';
  } catch {
    state.auctions = []; state.auctionsState = 'error';
  } finally { clearTimeout(timer); }
  drawAuctions(); paintChrome();
}

/* ── wiring ──────────────────────────────────────────────────────────────── */
function wire(shell) {
  let t;
  $('#pf-map-q').addEventListener('input', (e) => {
    clearTimeout(t);
    t = setTimeout(() => { state.q = e.target.value.trim().toLowerCase(); redraw(); }, 150);
  });
  shell.addEventListener('click', (e) => {
    const layer = e.target.closest('.pf-map-layer');
    if (layer) {
      const key = layer.dataset.layer;
      state.on[key] = !state.on[key];
      if (key === 'auctions' && !state.on.auctions) state.deals = false;
      return redraw();
    }
    if (e.target.closest('#pf-map-deals')) {
      state.deals = !state.deals;
      if (state.deals) state.on.auctions = true;
      return redraw();
    }
    const r = e.target.closest('.pf-map-r');
    if (r) { state.radius = Number(r.dataset.mi); paintMatch(); paintChrome(); if (ring) map.leaflet.fitBounds(ring.getBounds(), { padding: [16, 16], animate: false }); return; }
    if (e.target.closest('[data-clear]')) { state.picked = null; map.leaflet.closePopup(); paintMatch(); fitAll(); }
  });
}

async function start(el) {
  const shell = el.closest('.pf-map-shell') || el.parentElement;
  const matchBox = $('#pf-map-match');
  try {
    const [AdminMap] = await Promise.all([ensureAdminMap(), loadSample()]);
    map = await AdminMap.mount(el, { tiles: 'dark' });
  } catch {
    matchBox.textContent = 'The map could not load. Reload to try again.';
    return;
  }
  L = window.L;
  // a landing page scrolls past the map: the wheel zooms only once the map has been clicked
  map.leaflet.scrollWheelZoom.disable();
  map.leaflet.on('click', () => map.leaflet.scrollWheelZoom.enable());
  el.addEventListener('mouseleave', () => map.leaflet.scrollWheelZoom.disable());
  buyerLayer = L.layerGroup().addTo(map.leaflet);
  lotLayer = L.layerGroup().addTo(map.leaflet);
  wire(shell);
  drawSample(); paintChrome();
  fitAll();
  el.dataset.ready = '1';
  loadAuctions();   // not awaited: the sample layers work while this runs, and if it comes back empty
}

const el = document.querySelector('[data-pf-map]');
if (el) {
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) { io.disconnect(); start(el); }
    }, { rootMargin: '200px 0px' });
    io.observe(el);
  } else {
    start(el);
  }
}
