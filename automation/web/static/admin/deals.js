// static/admin/deals.js — admin Deals tab (plan §10 E-deals).
// Every read goes through UI.load (skeleton → ready|empty|error, keepOld for re-queries), every mutation through
// UI.pending; filter state lives in the URL via shell.js getParams/setParams (`?map=1` replaced the old browser-side map pref).
// Tree / popovers / comps drawer kept; table rows stay visible (dimmed, "refreshing" badge) while re-querying.
import * as shared from './shared.js';
import {load as uiLoad, pending, markStale, clearStale} from '../ui/state.js';
const {$, $$, toast, apiFetch, esc, hooks} = shared;
// hooks: `deal` state is published for auctions.loadProfiles(); loadProfiles itself lives in auctions.js.

// URL params — the shell.js getParams/setParams contract (plan §10 E1), but NOT imported from shell.js: index.html
// loads shell.js as `shell.js?v=<asset_v>` while `import './shell.js'` resolves to the bare URL, so a tab importing
// it gets a SECOND shell instance that boots the whole admin again (double mount → TDZ crash). Until the helpers
// move into shared.js (E1 follow-up) the same 12 lines live here; shared.js's copy wins the moment it exists.
function _getParams() {
  const out = {};
  for (const [k, v] of new URLSearchParams(location.search)) out[k] = v;
  return out;
}
function _setParams(patch, {replace = true} = {}) {
  const sp = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === null || v === undefined || v === '') sp.delete(k);
    else sp.set(k, String(v));
  }
  const qs = sp.toString();
  const url = location.pathname + (qs ? '?' + qs : '') + location.hash;
  const cur = location.pathname + location.search + location.hash;
  if (url !== cur) history[replace ? 'replaceState' : 'pushState'](history.state, '', url);
  return _getParams();
}
const getParams = (...a) => (shared.getParams || _getParams)(...a);
const setParams = (...a) => (shared.setParams || _setParams)(...a);

const loadProfiles = (...a) => hooks.loadProfiles(...a);

/* ── state ─────────────────────────────────────────────────────── */
const deal = {q: '', category: '', native: '', state: '', maxBids: '', ending: '', profile: '',
              status: 'active', sort: 'ends', dir: null, offset: 0, limit: 50,
              minMargin: '', maxDist: '', minPrice: '', maxPrice: '', listId: '', tag: '',
              facetsLoaded: false, treeStatus: null, expanded: new Set(),
              // Deal-browser chrome (lists / tags / saved searches).
              // rows: last page fetched (drawer lookup). memb/tags: client-side
              // membership knowledge, seeded by list/tag-filtered fetches and
              // kept current by the user's own PUT/DELETEs — the /api/deals
              // rows themselves don't carry per-lot membership in v1.
              rows: [], lists: [], tagList: [], searches: [], outcomes: null,
              metaLoaded: false, rowsLoaded: false, memb: new Map(), lotTags: new Map(),
              // 🗺 map view: bbox = "s,w,n,e" viewport filter pushed into SQL,
              // map = AdminMap handle, geoQS = last /api/deals/geo query string.
              mapOn: false, map: null, bbox: null, geoQS: null};
hooks.deal = deal;

const COLS = 18;
const STATUSES = ['active', 'closed', 'all'];
const SORTS = ['bids', 'bid', 'landed', 'margin', 'ends'];
const dealKey = (r) => `${r.asset_id}/${r.account_id}/${r.auction_id}`;
const dealMemb = (key) => deal.memb.get(key) || (deal.memb.set(key, new Set()), deal.memb.get(key));
const dealLotTags = (key) => deal.lotTags.get(key) || (deal.lotTags.set(key, new Set()), deal.lotTags.get(key));

/* ── URL state (this tab owns: q category state max_bids ending status sort dir page profile map) ── */
const BOOT_TAB = new URLSearchParams(location.search).get('tab');
let _popped = false, _urlSynced = false;
window.addEventListener('popstate', () => { _popped = true; });   // registered before shell.js's own handler (tabs evaluate first)

function writeUrl() {
  if (getParams().tab !== 'deals') return;
  const page = Math.floor(deal.offset / deal.limit) + 1;
  setParams({q: deal.q, category: deal.category, state: deal.state, max_bids: deal.maxBids, ending: deal.ending,
             status: deal.status === 'active' ? '' : deal.status, sort: deal.sort === 'ends' ? '' : deal.sort,
             dir: deal.dir || '', page: page > 1 ? String(page) : '', profile: deal.profile, map: deal.mapOn ? '1' : ''});
}

/* URL → state. Returns whether the map should be on. */
function readUrl() {
  const p = getParams();
  deal.q = p.q || ''; deal.category = p.category || ''; deal.native = ''; deal.state = p.state || '';
  deal.maxBids = /^\d+$/.test(p.max_bids || '') ? p.max_bids : '';
  deal.ending = /^\d+$/.test(p.ending || '') ? p.ending : '';
  deal.status = STATUSES.includes(p.status) ? p.status : 'active';
  deal.sort = SORTS.includes(p.sort) ? p.sort : 'ends';
  deal.dir = p.dir === 'asc' || p.dir === 'desc' ? p.dir : null;
  deal.profile = p.profile || '';
  deal.offset = (Math.max(1, parseInt(p.page, 10) || 1) - 1) * deal.limit;
  return p.map === '1';
}

/* state → controls (used after a URL read, a saved-search apply, and clear-all) */
function syncDealControls() {
  $('#deal-q').value = deal.q;
  const syncSel = (sel, val) => {
    const el = $(sel);
    el.value = [...el.options].some(o => o.value === val) ? val : '';
  };
  syncSel('#deal-category', deal.category);
  syncSel('#deal-state', deal.state);
  syncSel('#deal-ending', deal.ending);
  syncSel('#deal-list', deal.listId);
  syncSel('#deal-tag', deal.tag);
  syncSel('#deal-profile', deal.profile);
  $$('#deal-bids-filter .seg-btn').forEach(b => b.classList.toggle('is-active', b.dataset.value === deal.maxBids));
  $$('#deal-status-filter .seg-btn').forEach(b => b.classList.toggle('is-active', b.dataset.value === deal.status));
  $('#deal-min-margin').value = deal.minMargin;
  $('#deal-min-price').value = deal.minPrice;
  $('#deal-max-price').value = deal.maxPrice;
  $('#deal-max-dist').value = deal.maxDist;
  syncSortHeaders();
}

function syncSortHeaders() {
  $$('#deal-table th.sortable').forEach(th => {
    if (th.dataset.sort !== deal.sort) { th.removeAttribute('aria-sort'); return; }
    const dir = deal.dir ?? (deal.sort === 'ends' ? 'asc' : 'desc');
    th.setAttribute('aria-sort', dir === 'asc' ? 'ascending' : 'descending');
  });
}

/* UI.load renders .ui-empty/.ui-error as a direct child of the <tbody>; move it into one full-width row. */
function cellWrap(tbody) {
  const box = tbody.querySelector(':scope > .ui-empty, :scope > .ui-error');
  if (!box) return;
  const tr = document.createElement('tr'); tr.className = 'deal-state-row';
  const td = document.createElement('td'); td.colSpan = COLS;
  td.appendChild(box); tr.appendChild(td); tbody.appendChild(tr);
}

/* ── cells ─────────────────────────────────────────────────────── */
function dealEndsCell(iso) {
  if (!iso) return '<td>—</td>';
  const ms = new Date(iso) - Date.now();
  const h = ms / 3.6e6;
  const cls = h < 2 ? 'deal-ends-red' : (h < 24 ? 'deal-ends-yellow' : '');
  const label = ms <= 0 ? 'ended'
    : h < 1 ? `${Math.round(ms / 6e4)}m`
    : h < 48 ? `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`
    : `${Math.floor(h / 24)}d ${Math.floor(h % 24)}h`;
  return `<td class="${cls}" title="${esc(iso)}">${label}</td>`;
}

/* Verdict columns: est. resale / margin % (loudest cell) / conf / comps / rank.
   Un-analyzed lots render em-dashes. */
function dealVerdictCells(r, key) {
  const v = r.verdict;
  if (!v) return '<td>—</td><td class="deal-margin">—</td><td>—</td><td>—</td><td>—</td>';
  const m = v.margin_pct;
  const mCls = m == null ? '' : m >= 100 ? 'm-hot' : m >= 25 ? 'm-good' : m >= 0 ? 'm-flat' : 'm-neg';
  const margin = m == null ? '—' : `${m > 0 ? '+' : ''}${Math.round(m)}%`;
  const conf = `<span class="deal-conf deal-conf-${esc(v.confidence)}" title="method: ${esc(v.method)}">` +
               `${esc(v.confidence)}${v.method !== 'comps' ? ' · est' : ''}</span>`;
  const comps = v.comp_count > 0
    ? `<a href="#" class="deal-comps-link" data-key="${esc(key)}" title="open comp detail">${v.comp_count}</a>`
    : '0';
  return `<td>${v.est_resale != null ? '$' + Math.round(v.est_resale) : '—'}</td>
    <td class="deal-margin ${mCls}">${margin}</td>
    <td>${conf}</td>
    <td>${comps}</td>
    <td>${v.rank_score != null ? Math.round(v.rank_score) : '—'}</td>`;
}

const tagChip = (key, tag) =>
  `<span class="chip chip-mono">${esc(tag)}<button type="button" class="filter-chip-x" data-key="${esc(key)}" data-tag="${esc(tag)}" title="remove tag">×</button></span>`;

function dealRowsHtml(rows) {
  return rows.map(r => {
    const key = dealKey(r);
    const img = r.archived_hero_url || r.hero_image_url;
    const thumb = img
      ? `<img class="deal-thumb" src="${esc(img)}" loading="lazy" alt="">`
      : '<span class="deal-thumb deal-thumb-empty">🪑</span>';
    const outcome = r.outcome_complete
      ? `<span class="badge badge-closed">${esc(r.outcome) || 'closed'}${r.final_bid != null ? ` $${r.final_bid}` : ''}</span>`
      : '<span class="badge badge-live">open</span>';
    const saved = deal.memb.get(key)?.size > 0;
    const chips = [...(deal.lotTags.get(key) || [])].map(t => tagChip(key, t)).join('');
    return `<tr>
      <td>${thumb}</td>
      <td><button type="button" class="deal-heart ${saved ? 'on' : ''}" data-key="${esc(key)}" title="save to a list">♥</button></td>
      <td><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
          <a class="deal-viewer-link" href="${esc(r.viewer_url)}" target="_blank" rel="noopener" title="archived copy">⧉</a>
          <span class="deal-tags">${chips}<button type="button" class="deal-tag-add" data-key="${esc(key)}" title="add tag">+</button></span></td>
      <td>${esc(r.canonical_category) || '—'}</td>
      <td>${esc(r.city)}${r.state ? ', ' + esc(r.state) : ''}</td>
      <td>${r.distance_mi != null ? Math.round(r.distance_mi) + ' mi' : '—'}</td>
      <td>${r.bid_count ?? '—'}</td>
      <td>${r.current_bid != null ? '$' + r.current_bid : '—'}</td>
      <td class="num">${r.quantity}${r.quantity_source === 'default' ? '<span class="deal-qty-src" title="no count in title">·</span>' : ''}</td>
      <td class="num">${r.unit_bid != null ? '$' + r.unit_bid : '—'}</td>
      <td>$${r.landed_cost}</td>
      ${dealVerdictCells(r, key)}
      ${dealEndsCell(r.end_utc)}
      <td>${outcome}</td>
    </tr>`;
  }).join('');
}

/* ── category tree (#43): branch = canonical bucket, twig = native GovDeals category.
   Clicking a node filters the table; the arrow only expands/collapses. ── */
let _dealTree = null;

function dealTreeHtml() {
  const counts = (o) => `<span class="dt-counts">${o.n} · ⚑${o.zero_bid} · ⏱${o.ending_24h}</span>`;
  const total = {n: _dealTree.total,
                 zero_bid: _dealTree.branches.reduce((a, b) => a + b.zero_bid, 0),
                 ending_24h: _dealTree.branches.reduce((a, b) => a + b.ending_24h, 0)};
  let html = `<div class="dt-node dt-root ${!deal.category && !deal.native ? 'active' : ''}"
                   data-cat="" data-native="">all deals ${counts(total)}</div>`;
  html += _dealTree.branches.map(b => {
    const open = deal.expanded.has(b.category);
    const branchActive = deal.category === b.category && !deal.native;
    const twigs = b.twigs.map(t => `
      <div class="dt-node dt-twig ${deal.native === t.native_id ? 'active' : ''}"
           data-cat="${esc(b.category)}" data-native="${esc(t.native_id)}"
           title="GovDeals category ${esc(t.native_id)}">
        ${esc(t.name)} ${counts(t)}
      </div>`).join('');
    return `
      <div class="dt-branch">
        <div class="dt-node dt-b ${branchActive ? 'active' : ''}" data-cat="${esc(b.category)}" data-native="">
          <button type="button" class="dt-arrow ${open ? 'open' : ''}" data-toggle="${esc(b.category)}"
                  aria-label="expand ${esc(b.category)}">▸</button>
          ${esc(b.category)} ${counts(b)}
        </div>
        <div class="dt-twigs" ${open ? '' : 'hidden'}>${twigs}</div>
      </div>`;
  }).join('');
  return html;
}

/* re-highlight the active node; never clobbers a skeleton / empty / error box */
function renderDealTree() {
  const host = $('#deal-tree-nodes');
  if (!_dealTree || host.dataset.state !== 'ready') return;
  host.innerHTML = dealTreeHtml();
}

let _treeLoaded = false;
function loadDealTree() {
  deal.treeStatus = deal.status;
  const host = $('#deal-tree-nodes');
  const url = '/api/deals/tree?status=' + deal.status + (deal.profile ? '&profile=' + encodeURIComponent(deal.profile) : '');
  if (!_treeLoaded) host.replaceChildren();   // drop the server-shipped twin so load() paints its own (identical) skeleton
  return uiLoad(host, ({signal}) => apiFetch(url, {signal}), {
    keepOld: true, skeleton: 'line', count: 6, timeoutMs: 30000,
    isEmpty: (t) => !t || !(t.branches || []).length,
    empty: {glyph: '⌂', title: 'No categories yet', body: 'The tree fills in once a discover sweep has classified lots.'},
    render: (t) => { _dealTree = t; _treeLoaded = true; return dealTreeHtml(); },
    onError: () => { _dealTree = null; },
    errorMessage: (e) => `Couldn't load the category tree. ${e.status ? 'The server said ' + e.status + '.' : 'The database didn\'t answer in 30 s.'}`,
  });
}

/* ── the table (#44): keepOld — rows stay visible, dimmed, with a "refreshing" badge while re-querying ── */
function dealQuery() {
  const p = new URLSearchParams();
  if (deal.q) p.set('q', deal.q);
  if (deal.category) p.set('category', deal.category);
  if (deal.native) p.set('native', deal.native);
  if (deal.state) p.set('state', deal.state);
  if (deal.maxBids !== '') p.set('max_bids', deal.maxBids);
  if (deal.ending) p.set('ending_within', deal.ending);
  if (deal.minMargin !== '') p.set('min_margin', deal.minMargin);
  if (deal.minPrice !== '') p.set('min_price', deal.minPrice);
  if (deal.maxPrice !== '') p.set('max_price', deal.maxPrice);
  if (deal.maxDist !== '') p.set('max_distance', deal.maxDist);
  if (deal.listId) p.set('list_id', deal.listId);
  if (deal.tag) p.set('tag', deal.tag);
  if (deal.profile) p.set('profile', deal.profile);
  if (deal.mapOn && deal.bbox) p.set('bbox', deal.bbox);
  p.set('status', deal.status);
  p.set('sort', deal.sort);
  if (deal.dir) p.set('dir', deal.dir);
  p.set('limit', deal.limit);
  p.set('offset', deal.offset);
  return p;
}

async function loadDeals() {
  const tbody = $('#deal-rows');
  const stats = $('#deal-stats');
  if (deal.treeStatus !== deal.status) loadDealTree();
  if (!deal.metaLoaded) loadDealMeta();
  writeUrl();
  syncSortHeaders();
  const qs = dealQuery().toString();
  if (!deal.rowsLoaded) tbody.replaceChildren();   // server twin → load()'s identical skeleton (no keepOld badge on first paint)
  const body = await uiLoad(tbody, ({signal}) => apiFetch('/api/deals?' + qs, {signal}), {
    keepOld: true, skeleton: 'tr', count: 12, timeoutMs: 60000,   // /api/deals measured 8-34 s on the pooler
    isEmpty: (b) => !(b.rows || []).length,
    empty: {glyph: '◌', title: 'No lots match', body: 'Loosen a filter, widen the map, or switch status to all.',
            cta: {label: 'Clear filters', onClick: clearAllDealFilters}},
    render: (b) => {
      deal.rows = b.rows;
      // A list/tag-filtered result set is definitive membership knowledge.
      if (deal.listId) b.rows.forEach(r => dealMemb(dealKey(r)).add(Number(deal.listId)));
      if (deal.tag) b.rows.forEach(r => dealLotTags(dealKey(r)).add(deal.tag));
      return dealRowsHtml(b.rows);
    },
    onError: () => { stats.textContent = 'stats unavailable'; stats.dataset.state = 'error'; },
    errorMessage: (e) => `Couldn't load lots. ${e.status ? 'The server said ' + e.status + (e.message ? ': ' + e.message : '') + '.' : 'The database didn\'t answer in 60 s.'}`,
  });
  cellWrap(tbody);
  if (!body) return;   // error, or superseded by a newer query
  deal.rowsLoaded = true;
  if (!body.rows.length) deal.rows = [];
  const s = body.stats || {};
  stats.textContent =
    `${s.total_lots ?? '?'} lots tracked · ${s.candidates ?? '?'} candidates (0-bid <24h) · ${s.ending_24h ?? '?'} ending <24h`;
  stats.dataset.state = 'ready';
  if (!deal.facetsLoaded && body.facets) {
    const fill = (sel, items) => {
      const el = $(sel);
      el.innerHTML = '<option value="">all</option>' + items.map(f =>
        `<option value="${esc(f.value)}">${esc(f.value)} (${f.count})</option>`).join('');
    };
    fill('#deal-category', body.facets.categories || []);
    fill('#deal-state', body.facets.states || []);
    deal.facetsLoaded = true;
    deal._catFacets = body.facets.categories || [];
    syncDealControls();
    renderDealCatPills();
  }
  renderDealPager(body.total);
  if (deal.mapOn) refreshDealMapPoints(body.total);
  syncDealCatPills();
  renderDealActiveChips();
}

/* one-click canonical-category chip row (top 8 by count, from facets) */
function renderDealCatPills() {
  const host = $('#deal-cat-pills');
  if (!host) return;
  const cats = (deal._catFacets || []).slice(0, 8);
  if (!cats.length) { host.hidden = true; host.innerHTML = ''; return; }
  const total = (deal._catFacets || []).reduce((a, f) => a + Number(f.count || 0), 0);
  host.innerHTML =
    `<button type="button" class="chip" data-cat="">all <span class="chip-count">${total}</span></button>` +
    cats.map(f =>
      `<button type="button" class="chip" data-cat="${esc(f.value)}">${esc(f.value)} <span class="chip-count">${f.count}</span></button>`
    ).join('');
  host.hidden = false;
  syncDealCatPills();
}

function syncDealCatPills() {
  $$('#deal-cat-pills .chip').forEach(p => {
    const on = p.dataset.cat === deal.category;
    p.classList.toggle('is-active', on);
    p.setAttribute('aria-pressed', String(on));
  });
}

/* removable "label ×" chips for every active filter (status + bbox excluded) */
function renderDealActiveChips() {
  const host = $('#deal-active-chips');
  if (!host) return;
  const chips = [];
  if (deal.q) chips.push({k: 'q', label: `“${deal.q}”`});
  if (deal.category) chips.push({k: 'category', label: deal.category});
  if (deal.state) chips.push({k: 'state', label: deal.state});
  if (deal.maxBids !== '') chips.push({k: 'bids', label: deal.maxBids === '0' ? '0 bids' : `≤${deal.maxBids} bids`});
  if (deal.ending) chips.push({k: 'ending', label: `< ${deal.ending}h`});
  if (deal.minMargin !== '') chips.push({k: 'margin', label: `margin ≥ ${deal.minMargin}%`});
  if (deal.minPrice !== '' || deal.maxPrice !== '')
    chips.push({k: 'price', label: `$${deal.minPrice || 0}–${deal.maxPrice !== '' ? '$' + deal.maxPrice : '∞'}`});
  if (deal.maxDist !== '') chips.push({k: 'dist', label: `≤ ${deal.maxDist} mi`});
  if (deal.listId) {
    const l = deal.lists.find(l => String(l.id) === String(deal.listId));
    chips.push({k: 'list', label: `list: ${l ? l.name : deal.listId}`});
  }
  if (deal.tag) chips.push({k: 'tag', label: `tag: ${deal.tag}`});
  if (deal.profile) chips.push({k: 'profile', label: 'profile: ' + deal.profile});
  host.hidden = !chips.length;
  host.innerHTML = chips.map(c =>
    `<span class="filter-chip">${esc(c.label)}<button type="button" class="filter-chip-x" data-k="${c.k}" title="clear filter">×</button></span>`
  ).join('') + (chips.length ? '<button type="button" class="clear-all" id="deal-clear-filters">clear all</button>' : '');
}

const ALL_FILTER_KEYS = ['q', 'category', 'state', 'bids', 'ending', 'margin', 'price', 'dist', 'list', 'tag', 'profile'];

/* clear one filter's state AND its control (no reload — callers do that) */
function clearDealFilter(k) {
  switch (k) {
    case 'q': deal.q = ''; $('#deal-q').value = ''; break;
    case 'category':
      deal.category = ''; deal.native = ''; $('#deal-category').value = '';
      renderDealTree(); break;
    case 'state': deal.state = ''; $('#deal-state').value = ''; break;
    case 'bids':
      deal.maxBids = '';
      $$('#deal-bids-filter .seg-btn').forEach(b => b.classList.toggle('is-active', b.dataset.value === ''));
      break;
    case 'ending': deal.ending = ''; $('#deal-ending').value = ''; break;
    case 'margin': deal.minMargin = ''; $('#deal-min-margin').value = ''; break;
    case 'price':
      deal.minPrice = ''; deal.maxPrice = '';
      $('#deal-min-price').value = ''; $('#deal-max-price').value = ''; break;
    case 'dist': deal.maxDist = ''; $('#deal-max-dist').value = ''; break;
    case 'list': deal.listId = ''; $('#deal-list').value = ''; break;
    case 'tag': deal.tag = ''; $('#deal-tag').value = ''; break;
    case 'profile': deal.profile = ''; $('#deal-profile').value = ''; $('#deal-outcomes').hidden = true; break;
  }
}

function clearAllDealFilters() {
  ALL_FILTER_KEYS.forEach(clearDealFilter);
  deal.offset = 0;
  loadDeals();
}

let _dealQTimer;

/* PAST results strip for the chosen profile (#45): /api/profiles/{slug}/outcomes */
function loadDealOutcomes() {
  const host = $('#deal-outcomes');
  if (!deal.profile) { host.hidden = true; deal.outcomes = null; return; }
  host.hidden = false;
  const slug = deal.profile;
  return uiLoad(host, ({signal}) => apiFetch(`/api/profiles/${encodeURIComponent(slug)}/outcomes?days=365`, {signal}), {
    skeleton: 'line', count: 3, isEmpty: () => false,
    render: (o) => {
      deal.outcomes = o;
      const med = o.median_final_bid == null ? '—' : '$' + o.median_final_bid.toLocaleString();
      return `PAST 365d · ${o.closed} closed · ${o.no_bid_pct}% no-bid · median final ${med} · ${o.comps.length} lots with sold comps` +
        (o.comps.length ? ` · <a href="#" id="deal-outcomes-comps">show comps</a>` : '');
    },
    errorMessage: (e) => `Outcomes for “${slug}” didn't load${e.status ? ' (server said ' + e.status + ')' : ''}.`,
  });
}

const DEAL_PAGE_SIZES = [25, 50, 100, 200];
function renderDealPager(total) {
  const page = Math.floor(deal.offset / deal.limit) + 1;
  const pages = Math.max(1, Math.ceil(total / deal.limit));
  const remaining = Math.max(0, total - deal.offset - deal.limit);
  const scope = deal.mapOn && deal.bbox ? ' · in map view' : '';
  const html = `
    <span class="pager-total">page <strong>${page}</strong> / ${pages} · <strong>${total.toLocaleString()}</strong> lots${scope}${remaining ? ` · ${remaining.toLocaleString()} after this page` : ''}</span>
    <button type="button" class="btn btn-small" data-page="1" ${page <= 1 ? 'disabled' : ''}>«</button>
    <button type="button" class="btn btn-small" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>‹ prev</button>
    <button type="button" class="btn btn-small" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>next ›</button>
    <button type="button" class="btn btn-small" data-page="${pages}" ${page >= pages ? 'disabled' : ''}>»</button>
    <input type="number" class="deal-num" min="1" max="${pages}" value="${page}" data-jump title="jump to page" aria-label="jump to page">
    <select data-limit aria-label="rows per page">${DEAL_PAGE_SIZES.map(n => `<option value="${n}" ${n === deal.limit ? 'selected' : ''}>${n}/page</option>`).join('')}</select>`;
  $('#deal-pager-top').innerHTML = html;
  $('#deal-pager-bottom').innerHTML = html;
}
let _dealMarginTimer;
let _dealDistTimer;
let _dealMinPriceTimer;
let _dealMaxPriceTimer;

// ── Deals map (GovAuctions-style: all filtered lots cluster on the map,
//    pan/zoom pushes a bbox into /api/deals so the table follows the viewport) ──

function dealMapPopup(p) {
  const ends = p.end_utc ? new Date(p.end_utc).toLocaleString() : '';
  return `
    <strong><a href="${esc(p.govdeals_url)}" target="_blank" rel="noopener">${esc(p.title)}</a></strong><br>
    ${p.current_bid != null ? '$' + p.current_bid : '—'} · ${p.bid_count ?? 0} bids<br>
    ${esc(p.city || '')}${p.state ? ', ' + esc(p.state) : ''}<br>
    ${ends ? `⏱ ends ${esc(ends)}` : ''}`;
}

function updateDealMapNote(tableTotal) {
  const note = $('#deal-map-note');
  if (!note || !deal.map) return;
  const parts = [
    `${deal.map.visibleCount().toLocaleString()} of ${deal.map.count().toLocaleString()} mapped lots in view`,
  ];
  if (tableTotal != null && deal.bbox) parts.push(`table shows the ${tableTotal.toLocaleString()} in the viewport`);
  if (deal._geoUnmapped) parts.push(`${deal._geoUnmapped.toLocaleString()} lots have no coords (visible with map off)`);
  note.textContent = parts.join(' · ') + ' — pan or zoom to narrow.';
}

// Fetch pins for the current *filter* state (never the bbox — clusters must
// stay visible outside the viewport). Skips the network when filters are
// unchanged; loadDeals calls this on every map-on load. A failed refresh keeps
// the last pins interactive under a "map stale" badge (#46 is Workstream D's).
async function refreshDealMapPoints(tableTotal) {
  if (!deal.map) return;
  const wrap = $('#deal-map-wrap');
  const p = new URLSearchParams();
  if (deal.q) p.set('q', deal.q);
  if (deal.category) p.set('category', deal.category);
  if (deal.native) p.set('native', deal.native);
  if (deal.state) p.set('state', deal.state);
  if (deal.maxBids !== '') p.set('max_bids', deal.maxBids);
  if (deal.ending) p.set('ending_within', deal.ending);
  if (deal.minMargin !== '') p.set('min_margin', deal.minMargin);
  if (deal.minPrice !== '') p.set('min_price', deal.minPrice);
  if (deal.maxPrice !== '') p.set('max_price', deal.maxPrice);
  if (deal.listId) p.set('list_id', deal.listId);
  if (deal.tag) p.set('tag', deal.tag);
  if (deal.profile) p.set('profile', deal.profile);
  p.set('status', deal.status);
  const qs = p.toString();
  if (qs === deal.geoQS) { updateDealMapNote(tableTotal); return; }
  try {
    const body = await apiFetch('/api/deals/geo?' + qs);
    deal.geoQS = qs;
    deal._geoUnmapped = body.unmapped || 0;
    deal.map.setPoints(body.points.map(pt => ({
      lat: pt.lat, lng: pt.lng, title: pt.title, popup: dealMapPopup(pt),
    })));
    clearStale(wrap);
    updateDealMapNote(tableTotal);
  } catch (e) {
    markStale(wrap, {label: 'map stale'});
    toast('Deals map load failed: ' + (e.message || e), 'err');
  }
}

let _dealMapMove;
async function setDealMapOn(on) {
  const btn = $('#deal-map-toggle');
  const wrap = $('#deal-map-wrap');
  deal.mapOn = on;
  btn.classList.toggle('btn-primary', deal.mapOn);
  btn.setAttribute('aria-pressed', String(deal.mapOn));
  wrap.hidden = !deal.mapOn;
  if (!deal.mapOn) {
    deal.bbox = null; deal.offset = 0;
    loadDeals();
    return;
  }
  writeUrl();
  if (!deal.map) {
    try {
      deal.map = await AdminMap.mount($('#deal-map'));
      deal.map.onViewport(() => {
        updateDealMapNote(null);
        clearTimeout(_dealMapMove);
        _dealMapMove = setTimeout(() => {
          deal.bbox = deal.map.bboxParam();
          deal.offset = 0;
          loadDeals();
        }, 350);
      });
    } catch (e) {
      deal.mapOn = false; wrap.hidden = true; btn.classList.remove('btn-primary'); btn.setAttribute('aria-pressed', 'false');
      writeUrl();
      toast('Map failed to load: ' + (e.message || e), 'err');
      return;
    }
  }
  deal.map.invalidateSize();
  await refreshDealMapPoints(null);
  deal.map.fit();  // fit fires moveend → bbox lands → table follows
}

/* ZIP → center the map (opens it first when off) (#47) */
function centerDealMapOnZip() {
  const z = $('#deal-zip').value.trim();
  if (!/^\d{5}$/.test(z)) { if (z) toast('ZIP must be 5 digits', 'err'); return Promise.resolve(); }
  return pending($('#deal-zip-go'), '…', async () => {
    let body;
    try { body = await apiFetch('/api/geo/zip?zip=' + z); }
    catch (e) { toast('ZIP lookup failed: ' + (e.message || e), 'err'); return; }
    if (body.precision == null || body.lat == null) { toast(`ZIP ${z} not found`, 'err'); return; }
    if (!deal.mapOn) await setDealMapOn(true);
    if (!deal.map) return;  // mount failed; toast already shown
    deal.map.leaflet.setView([body.lat, body.lng], 9);
  });
}

/* ── Deal browser chrome: lists / tags / saved searches / comps drawer ── */

/* #48: lists + tags + saved searches in one load on the saved-search strip */
function loadDealMeta() {
  deal.metaLoaded = true;
  const host = $('#deal-searches');
  host.hidden = false;
  return uiLoad(host, ({signal}) => Promise.all([
    apiFetch('/api/deals/lists', {signal}), apiFetch('/api/deals/tags', {signal}), apiFetch('/api/deals/searches', {signal}),
  ]), {
    skeleton: 'pill', count: 3, isEmpty: () => false,
    render: ([lists, tags, searches]) => {
      deal.lists = lists; deal.tagList = tags; deal.searches = searches;
      const fillSel = (sel, items, cur) => {
        const el = $(sel);
        el.innerHTML = '<option value="">all</option>' + items.map(i =>
          `<option value="${esc(i.value)}">${esc(i.text)}</option>`).join('');
        el.value = [...el.options].some(o => o.value === String(cur)) ? String(cur) : '';
      };
      fillSel('#deal-list', deal.lists.map(l => ({value: l.id, text: `${l.name} (${l.count})`})), deal.listId);
      fillSel('#deal-tag', deal.tagList.map(t => ({value: t.tag, text: `${t.tag} (${t.count})`})), deal.tag);
      loadProfiles().catch(e => console.warn('profiles load failed:', e));
      return dealSearchesHtml();
    },
    onError: () => { deal.metaLoaded = false; },
    errorMessage: (e) => `Couldn't load lists, tags, and saved searches${e.status ? ' (server said ' + e.status + ')' : ''}.`,
  }).then(() => { if (host.dataset.state === 'ready') host.hidden = !deal.searches.length; });
}

/* saved-search chips above the filter bar: click = apply, × = delete */
function dealSearchesHtml() {
  return deal.searches.map(s =>
    `<span class="chip chip-mono deal-search-chip" data-id="${s.id}" title="${esc(JSON.stringify(s.params))}">
       ★ ${esc(s.name)}${s.alert ? ' 🔔' : ''}
       <button type="button" class="filter-chip-x deal-search-x" data-id="${s.id}" title="delete saved search">×</button>
     </span>`).join('');
}
function renderDealSearches() {
  const host = $('#deal-searches');
  host.hidden = !deal.searches.length;
  host.innerHTML = dealSearchesHtml();
  host.dataset.state = 'ready';
}

function currentDealParams() {
  const p = {};
  if (deal.q) p.q = deal.q;
  if (deal.category) p.category = deal.category;
  if (deal.native) p.native = deal.native;
  if (deal.state) p.state = deal.state;
  if (deal.maxBids !== '') p.max_bids = Number(deal.maxBids);
  if (deal.ending) p.ending_within = Number(deal.ending);
  if (deal.minMargin !== '') p.min_margin = Number(deal.minMargin);
  if (deal.minPrice !== '') p.min_price = Number(deal.minPrice);
  if (deal.maxPrice !== '') p.max_price = Number(deal.maxPrice);
  if (deal.maxDist !== '') p.max_distance = Number(deal.maxDist);
  if (deal.listId) p.list_id = Number(deal.listId);
  if (deal.tag) p.tag = deal.tag;
  if (deal.profile) p.profile = deal.profile;
  if (deal.mapOn && deal.bbox) p.bbox = deal.bbox;
  p.status = deal.status;
  return p;
}

function applyDealSearch(params) {
  deal.q = params.q || '';
  deal.category = params.category || '';
  deal.native = params.native || '';
  deal.state = params.state || '';
  deal.maxBids = params.max_bids != null ? String(params.max_bids) : '';
  deal.ending = params.ending_within != null ? String(params.ending_within) : '';
  deal.minMargin = params.min_margin != null ? String(params.min_margin) : '';
  deal.minPrice = params.min_price != null ? String(params.min_price) : '';
  deal.maxPrice = params.max_price != null ? String(params.max_price) : '';
  deal.maxDist = params.max_distance != null ? String(params.max_distance) : '';
  deal.listId = params.list_id != null ? String(params.list_id) : '';
  deal.tag = params.tag || '';
  deal.profile = params.profile || '';
  deal.status = STATUSES.includes(params.status) ? params.status : 'active';
  deal.offset = 0;
  syncDealControls();
  loadDealOutcomes();
  // saved map viewport: turn the map on (if needed) and restore its bounds
  if (params.bbox) {
    deal.bbox = params.bbox;
    (async () => {
      if (!deal.mapOn) await setDealMapOn(true);
      if (deal.map) {
        const [s, w, n, e] = params.bbox.split(',').map(Number);
        deal.map.leaflet.fitBounds([[s, w], [n, e]]);
      }
    })();
  }
  renderDealTree();
  loadDeals();
}

/* one shared floating-popover lifecycle: position near anchor, close on outside click */
function openDealPop(pop, anchor, html) {
  closeDealPops();
  pop.innerHTML = html;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${Math.round(r.bottom + 6)}px`;
  pop.style.left = `${Math.round(Math.min(r.left, window.innerWidth - 280))}px`;
  setTimeout(() => document.addEventListener('click', _dealPopOutside), 0);
}
function closeDealPops() {
  document.removeEventListener('click', _dealPopOutside);
  $('#deal-listpop').hidden = true;
  $('#deal-searchpop').hidden = true;
}
function _dealPopOutside(e) {
  if (e.target.closest('.deal-pop')) return;
  closeDealPops();
}

/* ♥ popover: checkbox per list + inline "new list" input (#50 #51 #52) */
function openListPop(heartBtn, key) {
  const memb = dealMemb(key);
  const boxes = deal.lists.map(l => `
    <label class="deal-pop-row">
      <input type="checkbox" data-list="${l.id}" ${memb.has(l.id) ? 'checked' : ''}>
      ${esc(l.name)} <span class="deal-pop-count">${l.count}</span>
    </label>`).join('') || '<div class="deal-pop-empty">no lists yet</div>';
  openDealPop($('#deal-listpop'), heartBtn, `
    <div class="deal-pop-head">SAVE TO LIST</div>
    ${boxes}
    <div class="deal-pop-new">
      <input type="text" id="deal-newlist-name" placeholder="new list…">
      <button type="button" class="btn btn-small" id="deal-newlist-add">+</button>
    </div>`);
  const pop = $('#deal-listpop');
  pop.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', async () => {
      const listId = Number(cb.dataset.list);
      const method = cb.checked ? 'PUT' : 'DELETE';
      cb.disabled = true;
      try {
        await pending(heartBtn, null, async () => {
          try { await apiFetch(`/api/deals/lists/${listId}/items/${key}`, {method}); }
          catch (err) { if (err.status !== 404) throw err; }
        });
        cb.checked ? memb.add(listId) : memb.delete(listId);
        const l = deal.lists.find(l => l.id === listId);
        if (l) l.count = Math.max(0, Number(l.count) + (cb.checked ? 1 : -1));
        heartBtn.classList.toggle('on', memb.size > 0);
      } catch (err) {
        cb.checked = !cb.checked;
        toast(`list update failed: ${err.message || err}`, 'err');
      } finally { cb.disabled = false; }
    });
  });
  const addNew = () => {
    const name = $('#deal-newlist-name').value.trim();
    if (!name) return;
    return pending($('#deal-newlist-add'), '…', async () => {
      try {
        const created = await apiFetch('/api/deals/lists', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name}),
        });
        await apiFetch(`/api/deals/lists/${created.id}/items/${key}`, {method: 'PUT'});
        memb.add(created.id);
        heartBtn.classList.add('on');
        deal.metaLoaded = false;
        await loadDealMeta();
        openListPop(heartBtn, key);   // re-render with the new list visible
      } catch (err) { toast(`create list failed: ${err.message || err}`, 'err'); }
    });
  };
  $('#deal-newlist-add').addEventListener('click', addNew);
  $('#deal-newlist-name').addEventListener('keydown', (e) => { if (e.key === 'Enter') addNew(); });
}

/* comps detail drawer (shared .drawer): verdict summary + each kept comp title/price/url */
function openDealDrawer(key) {
  const row = deal.rows.find(r => dealKey(r) === key);
  const v = row?.verdict;
  if (!v) return;
  const comps = (v.comps || []).map(c => `
    <div class="deal-drawer-comp">
      <a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
      <span class="deal-drawer-price">$${Number(c.price).toFixed(0)}</span>
    </div>`).join('') || '<div class="deal-pop-empty">no kept comps</div>';
  const d = $('#deal-drawer');
  d.innerHTML = `
    <div class="drawer-head">
      <h2>Comps · ${v.comp_count} kept</h2>
      <button type="button" class="btn btn-small" id="deal-drawer-close">× close</button>
    </div>
    <div class="drawer-body">
      <div class="deal-drawer-title">${esc(row.title)}</div>
      <div class="deal-drawer-meta">
        method <b>${esc(v.method)}</b> · est. resale <b>$${Math.round(v.est_resale)}</b>
        · margin <b>${Math.round(v.margin_pct)}%</b> · ${esc(v.confidence)} confidence
      </div>
      ${comps}
    </div>`;
  d.classList.add('is-open');
  d.setAttribute('aria-hidden', 'false');
  $('#deal-drawer-close').addEventListener('click', closeDealDrawer);
  $('#deal-drawer-close').focus();
}
function closeDealDrawer() {
  const d = $('#deal-drawer');
  d.classList.remove('is-open');
  d.setAttribute('aria-hidden', 'true');
}

/* ★ save-search / 🔔 create-alert popover: name + alert checkbox → POST /api/deals/searches (#55) */
function openSaveSearchPop(anchor, {alert = false} = {}) {
  openDealPop($('#deal-searchpop'), anchor, `
    <div class="deal-pop-head">${alert ? 'CREATE ALERT' : 'SAVE THIS SEARCH'}</div>
    <div class="deal-pop-new">
      <input type="text" id="deal-search-name" placeholder="name…">
    </div>
    <label class="deal-pop-row">
      <input type="checkbox" id="deal-search-alert" ${alert ? 'checked' : ''}> Telegram-alert on new matches
    </label>
    <div class="deal-pop-hint">checked hourly → Telegram (deals topic)</div>
    <div class="deal-pop-new">
      <button type="button" class="btn btn-small btn-primary" id="deal-search-save">${alert ? '🔔 create' : '★ save'}</button>
    </div>`);
  $('#deal-search-name').focus();
  const save = () => {
    const name = $('#deal-search-name').value.trim();
    if (!name) { toast('name required', 'err'); return; }
    return pending($('#deal-search-save'), 'saving…', async () => {
      try {
        await apiFetch('/api/deals/searches', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name, params: currentDealParams(),
                                alert: $('#deal-search-alert').checked}),
        });
        closeDealPops();
        deal.metaLoaded = false;
        await loadDealMeta();
        toast(`saved search “${name}”`, 'ok');
      } catch (err) { toast(`save failed: ${err.message || err}`, 'err'); }
    });
  };
  $('#deal-search-save').addEventListener('click', save);
  $('#deal-search-name').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') save(); });
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  $('#deal-tree-nodes').addEventListener('click', (e) => {
    const arrow = e.target.closest('.dt-arrow');
    if (arrow) {
      const cat = arrow.dataset.toggle;
      deal.expanded.has(cat) ? deal.expanded.delete(cat) : deal.expanded.add(cat);
      renderDealTree();
      e.stopPropagation();
      return;
    }
    const node = e.target.closest('.dt-node');
    if (!node) return;
    deal.category = node.dataset.cat;
    deal.native = node.dataset.native;
    if (deal.category && deal.native) deal.expanded.add(deal.category);
    // keep the CATEGORY dropdown honest (it only knows canonical buckets)
    const dd = $('#deal-category');
    dd.value = [...dd.options].some(o => o.value === deal.category) ? deal.category : '';
    deal.offset = 0;
    renderDealTree();
    loadDeals();
  });

  $('#deal-cat-pills').addEventListener('click', (e) => {
    const pill = e.target.closest('.chip');
    if (!pill) return;
    deal.category = pill.dataset.cat;
    deal.native = '';
    const dd = $('#deal-category');
    dd.value = [...dd.options].some(o => o.value === deal.category) ? deal.category : '';
    deal.offset = 0;
    renderDealTree();
    syncDealCatPills();
    loadDeals();
  });

  $('#deal-active-chips').addEventListener('click', (e) => {
    const x = e.target.closest('.filter-chip-x');
    if (x) {
      clearDealFilter(x.dataset.k);
      deal.offset = 0;
      loadDeals();
      return;
    }
    if (e.target.closest('#deal-clear-filters')) {
      e.preventDefault();
      clearAllDealFilters();
    }
  });

  $('#deal-outcomes').addEventListener('click', (e) => {
    const a = e.target.closest('#deal-outcomes-comps');
    if (!a) return;
    e.preventDefault();
    const o = deal.outcomes;
    if (!o) return;
    a.remove();
    $('#deal-outcomes').insertAdjacentHTML('beforeend', '<div class="mono tiny deal-outcomes-comps">' + o.comps.map(c =>
      `${esc(c.title || '')} — ${c.comp_count} comps, $${c.per_unit ?? '—'}/unit, margin ${c.margin_pct ?? '—'}%`).join('<br>') + '</div>');
  });

  $('#deal-q').addEventListener('input', (e) => {
    clearTimeout(_dealQTimer);
    _dealQTimer = setTimeout(() => { deal.q = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 300);
  });

  $('#deal-category').addEventListener('change', (e) => {
    deal.category = e.target.value; deal.native = ''; deal.offset = 0;
    renderDealTree(); loadDeals();
  });

  $('#deal-state').addEventListener('change', (e) => { deal.state = e.target.value; deal.offset = 0; loadDeals(); });

  $('#deal-profile').addEventListener('change', (e) => {
    deal.profile = e.target.value; deal.offset = 0; deal.treeStatus = null;
    loadDeals(); loadDealOutcomes();
  });

  $('#deal-ending').addEventListener('change', (e) => { deal.ending = e.target.value; deal.offset = 0; loadDeals(); });

  $$('#deal-bids-filter .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#deal-bids-filter .seg-btn').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      deal.maxBids = btn.dataset.value; deal.offset = 0; loadDeals();
    });
  });

  $$('#deal-status-filter .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#deal-status-filter .seg-btn').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      deal.status = btn.dataset.value; deal.offset = 0; loadDeals();
    });
  });

  $$('#deal-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (deal.sort === key) {
        deal.dir = (deal.dir ?? (key === 'ends' ? 'asc' : 'desc')) === 'asc' ? 'desc' : 'asc';
      } else { deal.sort = key; deal.dir = null; }
      deal.offset = 0; loadDeals();
    });
  });

  ['#deal-pager-top', '#deal-pager-bottom'].forEach(sel => {
    $(sel).addEventListener('click', (e) => {
      const b = e.target.closest('button[data-page]'); if (!b || b.disabled) return;
      deal.offset = (Number(b.dataset.page) - 1) * deal.limit; loadDeals();
    });
    $(sel).addEventListener('change', (e) => {
      if (e.target.matches('[data-jump]')) { deal.offset = (Math.max(1, Number(e.target.value) || 1) - 1) * deal.limit; loadDeals(); }
      if (e.target.matches('[data-limit]')) { deal.limit = Number(e.target.value); deal.offset = 0; loadDeals(); }
    });
  });

  $('#deal-refresh').addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ refreshing', async () => {
      deal.facetsLoaded = false; deal.metaLoaded = false;
      await Promise.all([loadDealTree(), loadDeals()]);
    });
  });

  $('#deal-min-margin').addEventListener('input', (e) => {
    clearTimeout(_dealMarginTimer);
    _dealMarginTimer = setTimeout(() => { deal.minMargin = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
  });

  $('#deal-max-dist').addEventListener('input', (e) => {
    clearTimeout(_dealDistTimer);
    _dealDistTimer = setTimeout(() => { deal.maxDist = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
  });

  $('#deal-min-price').addEventListener('input', (e) => {
    clearTimeout(_dealMinPriceTimer);
    _dealMinPriceTimer = setTimeout(() => { deal.minPrice = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
  });

  $('#deal-max-price').addEventListener('input', (e) => {
    clearTimeout(_dealMaxPriceTimer);
    _dealMaxPriceTimer = setTimeout(() => { deal.maxPrice = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
  });

  $('#deal-list').addEventListener('change', (e) => { deal.listId = e.target.value; deal.offset = 0; loadDeals(); });
  $('#deal-tag').addEventListener('change', (e) => { deal.tag = e.target.value; deal.offset = 0; loadDeals(); });

  $('#deal-map-toggle').addEventListener('click', () => setDealMapOn(!deal.mapOn));

  $('#deal-zip').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); centerDealMapOnZip(); }
  });
  $('#deal-zip').addEventListener('change', centerDealMapOnZip);
  $('#deal-zip-go').addEventListener('click', centerDealMapOnZip);

  $('#deal-searches').addEventListener('click', async (e) => {
    const x = e.target.closest('.deal-search-x');
    if (x) {
      e.stopPropagation();
      await pending(x, null, async () => {
        try {
          await apiFetch(`/api/deals/searches/${x.dataset.id}`, {method: 'DELETE'});
          deal.searches = deal.searches.filter(s => String(s.id) !== x.dataset.id);
          renderDealSearches();
        } catch (err) { toast(`delete failed: ${err.message || err}`, 'err'); }
      });
      return;
    }
    const chip = e.target.closest('.deal-search-chip');
    if (!chip) return;
    const s = deal.searches.find(s => String(s.id) === chip.dataset.id);
    if (s) applyDealSearch(s.params || {});
  });

  $('#deal-rows').addEventListener('click', async (e) => {
    const heart = e.target.closest('.deal-heart');
    if (heart) {
      e.stopPropagation();
      openListPop(heart, heart.dataset.key);
      return;
    }
    const chipX = e.target.closest('.filter-chip-x[data-tag]');
    if (chipX) {
      const {key, tag} = chipX.dataset;
      await pending(chipX, null, async () => {
        try {
          try { await apiFetch(`/api/deals/tags/${key}/${encodeURIComponent(tag)}`, {method: 'DELETE'}); }
          catch (err) { if (err.status !== 404) throw err; }
          dealLotTags(key).delete(tag);
          chipX.closest('.chip').remove();
          deal.metaLoaded = false;   // tag counts changed
        } catch (err) { toast(`remove tag failed: ${err.message || err}`, 'err'); }
      });
      return;
    }
    const tagAdd = e.target.closest('.deal-tag-add');
    if (tagAdd) {
      const key = tagAdd.dataset.key;
      const tag = (prompt('Tag this lot:') || '').trim().toLowerCase();
      if (!tag) return;
      await pending(tagAdd, '…', async () => {
        try {
          await apiFetch(`/api/deals/tags/${key}/${encodeURIComponent(tag)}`, {method: 'PUT'});
          dealLotTags(key).add(tag);
          tagAdd.insertAdjacentHTML('beforebegin', tagChip(key, tag));
          deal.metaLoaded = false;
        } catch (err) { toast(`add tag failed: ${err.message || err}`, 'err'); }
      });
      return;
    }
    const compsLink = e.target.closest('.deal-comps-link');
    if (compsLink) {
      e.preventDefault();
      openDealDrawer(compsLink.dataset.key);
    }
  });

  $('#deal-save-search').addEventListener('click', (e) => {
    e.stopPropagation();
    openSaveSearchPop(e.currentTarget);
  });

  $('#deal-create-alert').addEventListener('click', (e) => {
    e.stopPropagation();
    openSaveSearchPop(e.currentTarget, {alert: true});
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeDealPops();
    if ($('#deal-drawer').classList.contains('is-open')) closeDealDrawer();
  });
}

/* Tab activation. URL → state on a deep link (page booted on ?tab=deals) and on Back/Forward; otherwise the
   tab's own state is written back to the URL. `?map=1` is the map toggle. */
export function load() {
  const fromUrl = (!_urlSynced && BOOT_TAB === 'deals') || _popped;
  let mapWanted = deal.mapOn;
  if (fromUrl) {
    mapWanted = readUrl();
    syncDealControls();
    renderDealTree();
    if (deal.profile) loadDealOutcomes();
  }
  _popped = false; _urlSynced = true;
  loadDeals();
  if (mapWanted !== deal.mapOn) setDealMapOn(mapWanted);
  else if (deal.mapOn && deal.map) deal.map.invalidateSize();   // pane was hidden while away
}
