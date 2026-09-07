// static/deals/map.js — /deals?view=map (plan §8, Workstream D).
// Owns only the map box (#feed-map) and its side panel (#feed-map-panel). feed.js owns the URL, the
// grid, the toggle, and the filters; the two talk through the §6 contract:
//   feed.js → document 'feed:params'  detail = {params: URLSearchParams, total, rows}   (after every lots load)
//   map.js  → document 'feed:bbox'    detail = {bbox: 'S,W,N,E' | ''}                  (viewport moved / list restored)
//   feed.js → body[data-view="map"]   (from ?view=map or the #feed-view-toggle click) — map.js watches it.
// Public pins carry no photos, no verdicts, no distance: title · city, ST · bid · bids · timer · links only.
import {load, api, esc, fmt, skeleton} from '../ui/state.js';
import {card, tickTimers} from '../ui/card.js';

const $ = (s, r = document) => r.querySelector(s);
const FILTER_KEYS = ['q', 'category', 'state', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price'];
const PINS_CAP_LABEL = '5,000';
const VIEWPORT_DEBOUNCE_MS = 400;

let map = null;            // AdminMap handle (lazy — costs nothing until the map view opens)
let mounting = null;       // in-flight AdminMap.mount() promise
let shown = false;
let fitted = false;        // fit once per map session; later pin refreshes keep the operator's viewport
let lastParams = null;     // URLSearchParams from the last feed:params
let lastRows = [];         // rows from the last feed:params (the side list)
let pinsKey = null;        // filter signature of the pins currently on the map
let els = null;            // {box, panel, split}

const debounce = (ms, fn) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ── wiring that does not depend on feed.js ─────────────────────────────── */
function ensureCss() {
  if (document.querySelector('link[href^="/static/deals/map.css"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/static/deals/map.css';
  document.head.appendChild(link);
}

function ensureAdminMap() {
  if (window.AdminMap) return Promise.resolve(window.AdminMap);
  const existing = document.querySelector('script[src^="/static/admin_map.js"]');
  return new Promise((resolve, reject) => {
    const s = existing || document.createElement('script');
    const done = () => window.AdminMap ? resolve(window.AdminMap) : reject(new Error('admin_map.js loaded without AdminMap'));
    s.addEventListener('load', done, {once: true});
    s.addEventListener('error', () => reject(new Error('admin_map.js failed to load')), {once: true});
    if (!existing) { s.src = '/static/admin_map.js'; document.head.appendChild(s); }
  });
}

// feed.js may have placed #feed-map and #feed-map-panel as siblings anywhere in main; the 70/30 split
// needs one grid parent, so wrap them once (idempotent).
function ensureEls() {
  if (els) return els;
  const box = $('#feed-map'), panel = $('#feed-map-panel');
  if (!box || !panel) return null;
  let split = box.closest('.feed-map-split');
  if (!split) {
    split = document.createElement('div');
    split.className = 'feed-map-split'; split.hidden = true;
    box.before(split); split.append(box, panel);
  }
  box.setAttribute('role', 'region'); box.setAttribute('aria-label', 'Map of lots');
  panel.setAttribute('aria-label', 'Lots in view');
  els = {box, panel, split};
  return els;
}

/* ── params ─────────────────────────────────────────────────────────────── */
function filterParams() {
  const src = lastParams || new URLSearchParams(location.search);
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) { const v = src.get(k); if (v) p.set(k, v); }
  return p;
}
const filterKey = (p) => [...p.entries()].sort().map(([k, v]) => `${k}=${v}`).join('&');

/* ── pins → map ─────────────────────────────────────────────────────────── */
function lotUrl(pt) { return `/deals/${pt.asset_id}/${pt.account_id}/${pt.auction_id}`; }

function popupHtml(pt) {
  const place = [pt.city, pt.state].filter(Boolean).map(esc).join(', ');
  const bids = pt.bid_count ?? 0;
  return `<strong>${esc(pt.title || 'Untitled lot')}</strong><br>`
    + `${place || '—'}<br>`
    + `<span class="mono">${esc(fmt.money(pt.current_bid))} · ${esc(fmt.int(bids))} bid${bids === 1 ? '' : 's'} · ⏱ ${esc(fmt.endsIn(pt.end_utc))}</span><br>`
    + `<a href="${esc(lotUrl(pt))}">View lot</a>`
    + (pt.govdeals_url ? ` · <a href="${esc(pt.govdeals_url)}" target="_blank" rel="noopener">GovDeals ↗</a>` : '');
}

async function mountMap() {
  if (map) return map;
  if (!mounting) {
    mounting = ensureAdminMap().then((AdminMap) => AdminMap.mount(els.box)).then((m) => {
      map = m;
      let lastBbox = '';
      const announce = debounce(VIEWPORT_DEBOUNCE_MS, () => {
        if (!shown) return;
        const bbox = map.bboxParam();
        if (bbox === lastBbox) return;            // invalidateSize + fit both fire moveend — announce once
        lastBbox = bbox;
        document.dispatchEvent(new CustomEvent('feed:bbox', {detail: {bbox}}));
      });
      map.onViewport(() => { updateCount(); announce(); });
      return m;
    }).catch((err) => { mounting = null; throw err; });
  }
  return mounting;
}

async function fetchPins({signal}) {
  const p = filterParams();
  const key = filterKey(p);
  const [data] = await Promise.all([api('/deals/api/pins?' + p.toString(), {signal}), mountMap()]);
  if (signal.aborted) return data;
  const points = (data.points || []).map((pt) => ({
    lat: pt.lat, lng: pt.lng, title: pt.title || '', popup: popupHtml(pt),
  }));
  const filtersChanged = key !== pinsKey;
  map.setPoints(points);
  pinsKey = key;
  els.box.classList.remove('map-loading');
  map.invalidateSize();
  // Fit on the first load and whenever a filter changed (new pins may be off-screen); a plain refresh keeps
  // the operator's viewport. fit → moveend → feed:bbox → the grid follows.
  if ((!fitted || filtersChanged) && points.length) { fitted = true; map.fit(); }
  return data;
}

/* ── panel ──────────────────────────────────────────────────────────────── */
function listHtml() {
  return lastRows.length ? lastRows.map((r) => card(r)).join('') : skeleton('card', 3);
}

function panelHtml(data) {
  const n = map ? map.visibleCount() : (data.points || []).length;
  return `<div class="map-panel-head">
  <div class="map-panel-count"><strong class="mono" data-map-count>${esc(fmt.int(n))}</strong> lots in view</div>
  <p class="map-panel-hint">Pan or zoom to narrow the list.</p>
  ${data.capped ? `<p class="map-panel-note">Showing the first ${PINS_CAP_LABEL} pins — narrow the filters to see every lot.</p>` : ''}
</div>
<div class="grid grid-1 map-panel-list" data-map-list>${listHtml()}</div>`;
}

function updateCount() {
  const el = els && $('[data-map-count]', els.panel);
  if (el && map) el.textContent = fmt.int(map.visibleCount());
}

function renderList() {
  const list = els && $('[data-map-list]', els.panel);
  if (!list) return;
  list.innerHTML = listHtml();
  tickTimers(list);
}

function loadPanel({keepOld = false} = {}) {
  return load(els.panel, fetchPins, {
    skeleton: 'card', count: 3, keepOld,
    render: (d) => panelHtml(d),
    isEmpty: (d) => !(d.points || []).length,
    empty: {glyph: '⌖', title: 'No lots in this area', body: 'Zoom out or clear a filter.',
            cta: {label: 'Show list', onClick: showList}},
    errorMessage: (err) => err.status ? `Couldn't load the map pins. The server said ${err.status}.`
      : `Couldn't load the map pins. ${err.message || 'Network error.'}`,
    onError: () => { if (els) els.box.classList.remove('map-loading'); },
  }).then((d) => { if (d) { tickTimers(els.panel); updateCount(); } return d; });
}

/* ── view switching ─────────────────────────────────────────────────────── */
function showMap() {
  if (!ensureEls()) return;
  if (shown) { if (map) map.invalidateSize(); return; }
  shown = true;
  els.split.hidden = false; els.box.hidden = false; els.panel.hidden = false;
  els.box.classList.add('map-loading');
  loadPanel();
}

function showList() {
  if (!els) return;
  shown = false; fitted = false;
  els.split.hidden = true; els.box.hidden = true; els.panel.hidden = true;
  document.dispatchEvent(new CustomEvent('feed:bbox', {detail: {bbox: ''}}));
  const body = document.body;
  if (body.dataset.view === 'map') {
    const toggle = $('#feed-view-toggle');       // feed.js owns the URL — let its click handler drop view=map
    if (toggle) toggle.click();
    if (body.dataset.view === 'map') body.dataset.view = '';
  }
}

function syncView() {
  if (document.body.dataset.view === 'map') showMap(); else if (shown) showList();
}

/* ── feed contract ──────────────────────────────────────────────────────── */
document.addEventListener('feed:params', (e) => {
  const d = e.detail || {};
  lastParams = d.params instanceof URLSearchParams ? d.params : new URLSearchParams(d.params || '');
  lastRows = Array.isArray(d.rows) ? d.rows : [];
  if (!shown || !els) return;
  if (filterKey(filterParams()) !== pinsKey) loadPanel({keepOld: true});   // a filter changed (page/sort/bbox ignored)
  else if (els.panel.dataset.state === 'ready') renderList();
});

function init() {
  ensureCss();
  if (!ensureEls()) return;   // page without the §6 ids — nothing to do
  new MutationObserver(syncView).observe(document.body, {attributes: true, attributeFilter: ['data-view']});
  if (document.body.dataset.view !== 'map' && new URLSearchParams(location.search).get('view') === 'map') {
    document.body.dataset.view = 'map';   // feed.js normally sets this; cover the case where it has not yet
  }
  syncView();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
else init();

export {showMap, showList};
