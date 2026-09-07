// static/admin/auctions.js — Auctions tab (plan §10 E-auctions).
// Reads go through UI.load (skeleton → ready|empty|error, Retry re-runs the same fetcher), mutations through
// UI.pending, the 30 s favorites poll keeps old content and marks it stale on failure, the scrape SSE marks the
// strip stale on `error` and clears on `open`. Filter state lives in the URL (source, q, profile, map) via shell.js.
import {$, $$, toast, esc, SOURCE_NAMES, _ageInDays, _fmtAge, _fmtRemaining, queueRuns, hooks} from './shared.js';
import {api, load as uiLoad, pending, markStale, clearStale, renderEmpty} from '../ui/state.js';
import {getParams, setParams} from './shell.js';

let scrapeES;

// ───────── state ─────────

const SOURCES = ['gd', 'ps', 'bs'];
const LEGACY_MAP_KEY = 'admin.aucMapOn';   // pre-E-auctions localStorage toggle — read once, moved into ?map=, deleted

const auc = {
  source: 'gd',
  q: '',                  // client-side title filter (URL `q`)
  profile: '',            // research profile slug (research_profiles); '' = default
  profiles: [],           // rows from /api/profiles
  defaultProfile: 'chairs',
  items: [],
  stats: null,
  favorites: [],          // list of favorite dicts from /api/auctions/favorites
  favoriteIds: new Set(), // asset_id strings — for fast "is starred?" lookup
  favoritesOkAt: null,    // last successful favorites read (the stale badge counts from here)
  intervals: [],          // alert interval labels in display order
  telegramConfigured: false,
  mapOn: true,            // 🗺 map — on unless ?map=0; cards follow the map viewport
  map: null,              // AdminMap handle (lazy-mounted)
};

// ───────── URL params (source, q, profile, map) ─────────

function readParams() {
  const p = getParams();
  auc.source = SOURCES.includes(p.source) ? p.source : 'gd';
  auc.q = (p.q || '').trim();
  auc.profile = p.profile || auc.profile;
  if (auc.profiles.length && !auc.profiles.some(x => x.slug === auc.profile)) auc.profile = auc.defaultProfile;
  auc.mapOn = p.map !== '0';
}

function writeParams() {
  setParams({source: auc.source, q: auc.q || null, profile: auc.profile || null, map: auc.mapOn ? '1' : '0'});
}

function syncControls() {
  $$('#auc-source .seg-btn').forEach(b => b.classList.toggle('is-active', b.dataset.value === auc.source));
  const q = $('#auc-q');
  if (q && q.value !== auc.q) q.value = auc.q;
  if (auc.profiles.length) renderProfileSeg();
}

function _assetIdFromLink(link) {
  if (!link) return '';
  let m = link.match(/\/asset\/(\d+)\/(\d+)/);
  if (m) return `${m[1]}/${m[2]}`;
  m = link.match(/[?&]auc=(\d+)/);
  if (m) return `ps:${m[1]}`;
  m = link.match(/bidspotter\.com\/.*\/lot-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/);
  if (m) return `bs:${m[1]}`;
  return '';
}

// Build an eBay sold-listings search URL from an auction row (medical profile: demand-side comps).
function _ebaySoldUrl(it) {
  const raw = (it.title || it.raw_title || '').trim();
  const cleaned = raw
    .replace(/^lot of \d+x?\s*/i, '')
    .replace(/\(\d+[^)]*\)\s*$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const q = encodeURIComponent(cleaned || raw);
  return `https://www.ebay.com/sch/i.html?_nkw=${q}&LH_Sold=1&LH_Complete=1`;
}

// ── research profiles (what we're hunting for) — /api/profiles (#8) ──

function profileSegHtml() {
  return auc.profiles.map(p =>
    `<button type="button" class="seg-btn ${p.slug === auc.profile ? 'is-active' : ''}" data-value="${esc(p.slug)}"
       title="${esc((p.keywords || []).join(', '))} · min ${p.min_quantity}">${esc(p.name)}</button>`).join('');
}

function renderProfileSeg() {
  const seg = $('#auc-profile');
  seg.innerHTML = profileSegHtml();
  seg.dataset.state = 'ready';
  $('#auc-profile-del').disabled = !!(auc.profiles.find(p => p.slug === auc.profile) || {}).is_default;
}

async function loadProfiles() {
  const seg = $('#auc-profile');
  const body = await uiLoad(seg, ({signal}) => api('/api/profiles', {signal}), {
    skeleton: 'pill', count: 3, keepOld: auc.profiles.length > 0,
    isEmpty: () => false,
    render: (body) => {
      auc.profiles = body.profiles || [];
      auc.defaultProfile = body.default || 'chairs';
      if (!auc.profile || !auc.profiles.some(p => p.slug === auc.profile)) auc.profile = auc.defaultProfile;
      return profileSegHtml();
    },
    errorMessage: () => "Couldn't load research profiles.",
  });
  if (!body) return auc.profiles;   // error rendered in the seg; Retry re-runs the same fetch
  $('#auc-profile-del').disabled = !!(auc.profiles.find(p => p.slug === auc.profile) || {}).is_default;
  const sel = $('#deal-profile');   // Deals tab's profile picker is fed from here (hooks.loadProfiles)
  if (sel) {
    sel.innerHTML = '<option value="">any profile</option>' +
      auc.profiles.map(p => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('');
    sel.value = (hooks.deal && hooks.deal.profile) || '';
  }
  return auc.profiles;
}

// ── scrape dropdown + strip ──

const dd = $('#scrape-dropdown');
const ddMenu = $('.dropdown-menu', dd);

async function startScrape(source, test, btn) {
  // Immediate optimistic feedback — don't wait for POST round-trip.
  setScrapeStrip({
    status: 'running',
    source,
    current_step: source === 'both' ? 'gd' : source,
    current_stage: 'starting',
    stage_detail: null,
    last_line: 'starting scraper…',
    test_mode: test,
  });
  try {
    await pending(btn, '⟳ starting…', () => api('/api/scrape/start', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({source, test, profile: auc.profile || auc.defaultProfile}),
    }));
  } catch (err) {
    setScrapeStrip({status: 'error', source, last_line: err.message || String(err)});
    toast(`Scrape failed to start: ${err.message || err}`, 'err');
  }
}

function setScrapeStrip(s) {
  const strip = $('#scrape-strip');
  if (!strip) return;
  // Hide entirely when idle and never-started (avoids a permanent strip).
  if (s.status === 'idle' && !s.finished_at && !s.started_at) {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;
  const pill = $('#scrape-pill');
  pill.textContent = s.status;
  pill.dataset.state = s.status;

  const src = s.source ? (s.current_step && s.source === 'both'
    ? `${s.source} · ${s.current_step}` : s.source) : '';
  $('#scrape-source').textContent = src ? `[${src}${s.test_mode ? ' · test' : ''}]` : '';

  const stageEl = $('#scrape-stage');
  if (s.status === 'running' && s.current_stage) {
    stageEl.textContent = s.stage_detail ? `${s.current_stage} · ${s.stage_detail}` : s.current_stage;
    stageEl.hidden = false;
  } else {
    stageEl.textContent = '';
    stageEl.hidden = true;
  }

  $('#scrape-tail').textContent = (s.last_line || '').slice(-160);
  $('#scrape-cancel').hidden = (s.status !== 'running');
}

// #14 — the strip is a fixed set of children, so a failed refetch marks it stale instead of replacing it.
function refreshScrapeState() {
  const strip = $('#scrape-strip');
  api('/api/scrape/state')
    .then(s => { clearStale(strip); setScrapeStrip(s); })
    .catch(() => markStale(strip));
}

function connectScrapeStream() {
  if (scrapeES) scrapeES.close();
  scrapeES = new EventSource('/api/scrape/stream');
  scrapeES.onopen = () => clearStale($('#scrape-strip'));
  scrapeES.onerror = () => markStale($('#scrape-strip'), {label: 'stream lost'});   // #63
  scrapeES.addEventListener('stdout', (e) => handleScrapeLine(e, 'stdout'));
  scrapeES.addEventListener('stderr', (e) => handleScrapeLine(e, 'stderr'));
  scrapeES.addEventListener('system', (e) => handleScrapeLine(e, 'system'));
  scrapeES.addEventListener('event', (e) => {
    try {
      const msg = JSON.parse(e.data);
      const data = msg.data || {};
      if (data.kind === 'scrape') {
        // Refresh full state from server, then reload the card grid.
        refreshScrapeState();
        if (data.status === 'finished' || data.status === 'error' || data.status === 'cancelled') {
          loadAuctions();
        }
      } else if (data.kind === 'scrape_stage') {
        // Live stage update without a full state refetch.
        const stageEl = $('#scrape-stage');
        if (data.stage) {
          stageEl.textContent = data.detail ? `${data.stage} · ${data.detail}` : data.stage;
          stageEl.hidden = false;
        } else {
          stageEl.hidden = true;
        }
      }
    } catch (err) { console.warn(err); }
  });
}

function handleScrapeLine(e, kind) {
  try {
    const msg = JSON.parse(e.data);
    const line = typeof msg.data === 'string' ? msg.data : JSON.stringify(msg.data);
    // Update the tail live without a full state refetch.
    const strip = $('#scrape-strip');
    if (strip && strip.hidden === false) {
      $('#scrape-tail').textContent = line.slice(-160);
    }
  } catch (err) { console.warn(err); }
}

// ── cache header + staleness banner — /api/auctions/cache-stats (#15) ──

function _maxStaleDays() { return Number($('#auc-stale').value) || 7; }

function cacheHeaderHtml(host, stats, maxStaleDays) {
  if (!stats || !stats.total) {
    host.removeAttribute('data-freshness');
    return `<span class="ch-total">0 lots in cache</span>
      <span class="ch-age">hit <strong>⟳ scrape now</strong> to populate</span>`;
  }
  const ageDays = _ageInDays(stats.newest_seen_at);
  let freshness = 'fresh';
  if (ageDays == null) freshness = 'stale';
  else if (ageDays > maxStaleDays) freshness = 'stale';
  else if (ageDays > maxStaleDays * 0.6) freshness = 'aging';

  const sources = Object.entries(stats.by_source || {})
    .filter(([s]) => s !== 'other')
    .map(([s, v]) => `${s}: ${v.count.toLocaleString()}`)
    .join(' · ');

  host.dataset.freshness = freshness;
  return `
    <span class="ch-total">📦 ${stats.total.toLocaleString()} lots in cache</span>
    <span class="ch-age">newest scraped <span class="ch-age-val">${_fmtAge(ageDays)}</span></span>
    ${sources ? `<span class="ch-sources">${sources}</span>` : ''}
  `;
}

function renderStalenessBanner(stats, maxStaleDays) {
  const banner = $('#staleness-banner');
  if (!banner) return;
  const ageDays = _ageInDays(stats?.newest_seen_at);
  const shouldShow = stats && stats.total > 0 && ageDays != null && ageDays > Math.max(2, maxStaleDays);
  if (!shouldShow) { banner.hidden = true; return; }
  banner.hidden = false;
  $('#staleness-message').innerHTML =
    `Auction cache is <strong>${_fmtAge(ageDays)}</strong>. Re-scrape to refresh.`;
}

async function fetchCacheStats() {
  const host = $('#auction-cache-stats');
  host.hidden = false;
  const maxStaleDays = _maxStaleDays();
  const stats = await uiLoad(host, ({signal}) => api('/api/auctions/cache-stats', {signal}), {
    skeleton: 'line', count: 2, keepOld: !!auc.stats,
    isEmpty: () => false,
    render: (stats) => {
      auc.stats = stats;
      renderStalenessBanner(stats, maxStaleDays);
      return cacheHeaderHtml(host, stats, maxStaleDays);
    },
    errorMessage: () => "Couldn't read the auction cache stats.",
  });
  return stats || null;
}

// ── the grid — /api/auctions (#16) ──

function _titleOf(it) { return (it.title || it.raw_title || ''); }

// What the grid shows: the fetched lots, narrowed by the title search and (map on) the viewport.
// Unmapped lots always stay visible — a missing zip must never hide a good lot.
function visibleItems() {
  let items = auc.items;
  if (auc.q) {
    const needle = auc.q.toLowerCase();
    items = items.filter(it => _titleOf(it).toLowerCase().includes(needle));
  }
  if (auc.mapOn && auc.map) items = items.filter(it => it.lat == null || auc.map.inBounds(it));
  return items;
}

function gridFragment(items) {
  const frag = document.createDocumentFragment();
  for (const it of items) frag.appendChild(renderAuctionCard(it));
  return frag;
}

// Plain-text reasons the current filters could be hiding everything (feeds the summary line and the empty state).
function filterReasons(maxStaleDays) {
  const srcKey = auc.source;
  const ageDays = _ageInDays(auc.stats?.by_source?.[srcKey]?.newest_seen_at);
  const activeOnly = !$('#auc-expired').checked;
  const reasons = [];
  if (activeOnly && ageDays != null && ageDays > maxStaleDays) {
    reasons.push(`staleness (newest ${srcKey} row is ${_fmtAge(ageDays)}, filter hides anything past ${maxStaleDays} days)`);
  }
  if (Number($('#auc-min-qty').value) > 50) reasons.push(`min-units set to ${$('#auc-min-qty').value}`);
  if (activeOnly) reasons.push('“Show ended auctions” is off');
  return reasons;
}

// Empty-state copy for "the API returned no lots" (cache empty vs filters too tight).
function cacheEmptyArgs(maxStaleDays) {
  const srcName = SOURCE_NAMES[auc.source] || auc.source;
  const total = auc.stats?.by_source?.[auc.source]?.count ?? 0;
  if (!auc.stats || total === 0) {
    return {glyph: '◌', title: `Cache is empty for ${srcName}`,
      body: 'Run the scraper to fill it, then the ranked lots show up here.',
      cta: {label: 'Scrape now', onClick: (e) => startScrape(auc.source, false, e.currentTarget)}};
  }
  const reasons = filterReasons(maxStaleDays);
  const activeOnly = !$('#auc-expired').checked;
  return {glyph: '⌀', title: `No ${srcName} lots match these filters`,
    body: `${total.toLocaleString()} ${auc.source} lots in cache, filters excluded all of them.`
      + (reasons.length ? ` Likely culprit: ${reasons.join(' · ')}.` : ' Try lowering the filters.'),
    cta: activeOnly
      ? {label: 'Show ended auctions', onClick: () => { $('#auc-expired').checked = true; loadAuctions(); }}
      : {label: 'Reset filters', onClick: () => { $('#auc-min-qty').value = 1; $('#auc-min-qty-out').textContent = '1';
                                                  $('#auc-stale').value = 365; loadAuctions(); }}};
}

// Empty-state copy for "lots exist but the search / map viewport hides them all".
function narrowedEmptyArgs() {
  if (auc.q) {
    return {glyph: '⌕', title: `No lot titles contain “${auc.q}”`, body: `${auc.items.length} lots loaded — the search is client-side.`,
      cta: {label: 'Clear search', onClick: () => setSearch('')}};
  }
  return {glyph: '◎', title: 'No lots in this map view', body: 'Pan or zoom the map — lots without a location always stay listed.',
    cta: {label: 'Show all on map', onClick: () => { if (auc.map) auc.map.fit(); }}};
}

async function loadAuctions() {
  const grid = $('#auction-grid');
  const status = $('#auction-status');
  const summary = $('#auction-filter-summary');
  const useCond = $('#auc-condition').checked;
  const maxStaleDays = _maxStaleDays();
  status.textContent = useCond ? 'condition scoring adds 3–10 s' : '';
  if (summary) summary.hidden = true;

  if (!auc.profiles.length) await loadProfiles();
  const qs = new URLSearchParams({
    source: auc.source,
    n: $('#auc-n').value,
    min_qty: $('#auc-min-qty').value,
    condition: useCond ? '1' : '0',
    active_only: $('#auc-expired').checked ? '0' : '1',
    max_stale_days: String(maxStaleDays),
    profile: auc.profile || auc.defaultProfile,
  });

  await fetchCacheStats();   // cheap count; the empty-state copy below needs it
  const [body] = await Promise.all([
    uiLoad(grid, async ({signal}) => {
      const body = await api('/api/auctions?' + qs.toString(), {signal});
      auc.items = body.items || [];
      return body;
    }, {
      skeleton: 'card', count: 8, keepOld: auc.items.length > 0,
      timeoutMs: useCond ? 60000 : 20000,
      isEmpty: (body) => !(body.items || []).length,
      empty: cacheEmptyArgs(maxStaleDays),
      render: () => { const items = visibleItems(); return items.length ? gridFragment(items) : ''; },
      errorMessage: (err) => `Couldn't load lots from the cache. ${err.status ? `The server said ${err.status}: ${err.message}.` : (err.message || 'No answer.')}`,
    }),
    loadFavorites(),
  ]);
  if (!body) { status.textContent = ''; return; }   // error box + Retry are in the grid; toast would be noise
  status.textContent = `${auc.items.length} shown · ${body.cached ? `cached ${body.age}s ago` : 'fresh'}`;
  renderFilterSummary(auc.items.length, maxStaleDays);
  if (auc.items.length && !visibleItems().length) renderEmpty(grid, narrowedEmptyArgs());
  syncAuctionMap();
}

function renderFilterSummary(shownCount, maxStaleDays) {
  const summary = $('#auction-filter-summary');
  if (!summary) return;
  const stats = auc.stats;
  if (!stats || !stats.total) { summary.hidden = true; return; }
  const srcKey = auc.source;
  const srcCount = stats.by_source?.[srcKey]?.count ?? 0;
  if (shownCount > 0 && shownCount < srcCount) {
    summary.hidden = false;
    summary.textContent = `Showing ${shownCount} of ${srcCount.toLocaleString()} ${srcKey} lots (ranked by quantity).`;
  } else {
    summary.hidden = true;   // the zero case is the grid's empty state
  }
}

// Re-paint the grid from state (star flips, search, map viewport) — no fetch.
function renderAuctions() {
  const grid = $('#auction-grid');
  if (!auc.items.length) { renderEmpty(grid, cacheEmptyArgs(_maxStaleDays())); return; }
  const items = visibleItems();
  if (!items.length) { renderEmpty(grid, narrowedEmptyArgs()); return; }
  grid.innerHTML = '';
  grid.appendChild(gridFragment(items));
  grid.dataset.state = 'ready';
}

function setSearch(q) {
  auc.q = (q || '').trim();
  const input = $('#auc-q');
  if (input && input.value !== auc.q) input.value = auc.q;
  writeParams();
  if (auc.items.length) renderAuctions();
}

// ── Auctions map (GovAuctions-style: pins cluster, cards follow the viewport) ──

function auctionMapPopup(it) {
  const loc = [it.location, it.pickup_zip].filter(Boolean).join(' · ');
  const img = it.image_url
    ? `<img class="amap-popup-img" src="${esc(it.image_url)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`
    : '';
  return `${img}
    <strong>${esc(_titleOf(it) || '—')}</strong><br>
    ${(it.quantity || 0).toLocaleString()} × ${it.price ? esc(it.price) : ''}<br>
    ${loc ? `📍 ${esc(loc)}${it.geo_precision === 'state' ? ' <em>(state-level pin)</em>' : ''}<br>` : ''}
    <a href="${esc(it.link)}" target="_blank" rel="noopener">↗ view auction</a>`;
}

function updateAuctionMapNote() {
  const note = $('#auction-map-note');
  if (!auc.map || !note) return;
  const mapped = auc.map.count();
  const unmapped = auc.items.length - auc.items.filter(i => i.lat != null).length;
  const inView = auc.map.visibleCount();
  note.textContent =
    `${inView} of ${mapped} lots in view — pan/zoom to filter the cards below.` +
    (unmapped ? ` ${unmapped} lot${unmapped > 1 ? 's have' : ' has'} no location and stays listed.` : '');
}

function applyAuctionViewport() {
  if (!auc.mapOn || !auc.map) return;
  if (auc.items.length) renderAuctions();
  updateAuctionMapNote();
}

function syncAuctionMap(fit = false) {
  if (!auc.mapOn || !auc.map) return;
  auc.map.setPoints(auc.items.map(it => ({
    lat: it.lat, lng: it.lng,
    title: _titleOf(it),
    approx: it.geo_precision === 'state',
    popup: auctionMapPopup(it),
  })));
  if (fit) auc.map.fit();
  applyAuctionViewport();
}

async function setAucMapOn(on) {
  const btn = $('#auc-map-toggle');
  const wrap = $('#auction-map-wrap');
  auc.mapOn = on;
  btn.classList.toggle('btn-primary', auc.mapOn);
  wrap.hidden = !auc.mapOn;
  if (!auc.mapOn) {
    if (auc.items.length) renderAuctions();   // back to the plain full list
    return;
  }
  if (!auc.map) {
    try {
      auc.map = await AdminMap.mount($('#auction-map'));
      auc.map.onViewport(() => applyAuctionViewport());
    } catch (e) {
      auc.mapOn = false; wrap.hidden = true; btn.classList.remove('btn-primary');
      toast('Map failed to load: ' + (e.message || e), 'err');
      return;
    }
  }
  auc.map.invalidateSize();
  syncAuctionMap(true);
}

// Map follows ?map= (absent = on). Pane was hidden while away → the map needs a resize.
function applyMapParam() {
  if (auc.mapOn) {
    if (!auc.map || $('#auction-map-wrap').hidden) setAucMapOn(true);
    else auc.map.invalidateSize();
  } else if (!$('#auction-map-wrap').hidden) {
    setAucMapOn(false);
  }
}

function renderAuctionCard(it) {
  const card = document.createElement('article');
  card.className = 'card card-auction';

  const cond = it.condition;
  let condPill = '';
  if (cond != null) {
    const condCls = cond >= 7 ? 'good' : (cond >= 5 ? 'ok' : 'bad');
    condPill = `<span class="auction-cond ${condCls}">${cond}/10</span>`;
  }

  const img = it.image_url
    ? `<img src="${esc(it.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'auction-img-fallback',textContent:'🪑'}))">`
    : `<div class="auction-img-fallback">🪑</div>`;

  const ends = it.end_date || it.time_left || '';
  const isGovDeals = (it.link || '').includes('govdeals.com');
  const launchDisabled = !isGovDeals;
  const launchTitle = isGovDeals
    ? 'Queue this listing for the pipeline'
    : 'Pipeline only supports GovDeals URLs';

  // "Location · ZIP" line; the cached `location` is already "City, State, Country".
  const locLine = [it.location, it.pickup_zip].filter(Boolean).join(' · ');

  const assetId = _assetIdFromLink(it.link);
  const isStarred = assetId && auc.favoriteIds.has(assetId);

  card.innerHTML = `
    <div class="auction-img">
      ${img}
      ${assetId ? `<button type="button" class="auction-star ${isStarred ? 'on' : ''}"
        data-asset-id="${esc(assetId)}"
        title="${isStarred ? 'Unstar — stops countdown alerts' : 'Star — get Telegram pings as the auction winds down'}"
        aria-label="${isStarred ? 'Unstar' : 'Star'}">${isStarred ? '★' : '☆'}</button>` : ''}
    </div>
    <div class="auction-body">
      <h3 class="auction-title">${esc(_titleOf(it) || '—')}</h3>
      <div class="auction-meta">
        <span class="auction-qty">${(it.quantity||0).toLocaleString()} ×</span>
        ${it.price ? `<span class="auction-price">${esc(it.price)}</span>` : ''}
        ${condPill}
      </div>
      ${locLine ? `<div class="auction-loc">📍 ${esc(locLine)}</div>` : ''}
      ${(it.contact_phone || it.contact_email) ? `<div class="auction-contact">☎ ${esc([it.contact_phone, it.contact_email].filter(Boolean).join(' · '))}</div>` : ''}
      ${ends ? `<div class="auction-ends">⏱ ${esc(ends)}</div>` : ''}
      ${it.condition_note ? `<div class="auction-note">${esc(it.condition_note)}</div>` : ''}
      <div class="auction-actions">
        <a href="${esc(it.link)}" target="_blank" rel="noopener" class="auction-link">↗ source</a>
        ${it.category === 'medical' ? `<a href="${esc(_ebaySoldUrl(it))}" target="_blank" rel="noopener" class="auction-link" title="eBay sold-listings search — demand-side comps for this model">📊 sold comps</a>` : ''}
        <button type="button" class="btn btn-small btn-primary auction-launch"
                ${launchDisabled ? 'disabled' : ''}
                title="${esc(launchTitle)}"
                data-url="${esc(it.link)}">▶ launch</button>
      </div>
    </div>
  `;

  const starBtn = card.querySelector('.auction-star');
  if (starBtn) {
    starBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleFavorite(assetId, it, starBtn);
    });
  }

  const launchBtn = card.querySelector('.auction-launch');
  if (launchBtn && !launchDisabled) {
    launchBtn.addEventListener('click', async () => {
      try {
        await pending(launchBtn, '⏱ queuing…', () => queueRuns([it.link]));
        // Permanent per-session badge so it's clear the lot is already in.
        launchBtn.textContent = '✓ queued';
        launchBtn.disabled = true;
        launchBtn.classList.add('queued');
        toast(`Queued: ${it.title || it.link}`, 'ok');
      } catch (err) {
        toast('Queue failed: ' + (err.message || err), 'err');
      }
    });
  }
  return card;
}

// ─────────── auction favorites + countdown alerts — /api/auctions/favorites (#17–#20) ───────────

function applyFavorites(body) {
  auc.favorites = body.items || [];
  auc.favoriteIds = new Set(auc.favorites.map(f => f.asset_id));
  auc.intervals = body.intervals || [];
  auc.telegramConfigured = !!body.telegram_configured;
  auc.favoritesOkAt = Date.now();
  renderFavoritesHead();
}

// First paint / after a star: skeleton → cards (keepOld when cards are already up). Poll: old cards stay,
// a failed read only adds a `stale · N min` badge (#17 — this poll is the one that wedged the server).
async function loadFavorites({poll = false} = {}) {
  const grid = $('#auction-favorites-grid');
  if (!grid) return;
  if (poll) {
    try {
      applyFavorites(await api('/api/auctions/favorites'));
      clearStale(grid);
      if (auc.favorites.length) { grid.innerHTML = favCardsHtml(); grid.dataset.state = 'ready'; }
    } catch (err) {
      markStale(grid, {since: auc.favoritesOkAt || Date.now()});
    }
    return;
  }
  await uiLoad(grid, async ({signal}) => {
    const body = await api('/api/auctions/favorites', {signal});
    applyFavorites(body);
    return body;
  }, {
    skeleton: 'row', count: 1, keepOld: auc.favorites.length > 0,
    isEmpty: (body) => !(body.items || []).length,
    empty: {glyph: '☆', title: 'No starred lots', body: 'Star a card to get Telegram pings as its auction winds down.'},
    render: () => favCardsHtml(),
    errorMessage: () => "Couldn't load your starred lots.",
    onError: () => { $('#auction-favorites-strip').hidden = false; },
  });
}

async function toggleFavorite(assetId, sourceItem, btn) {
  if (!assetId) return;
  const wasStarred = auc.favoriteIds.has(assetId);
  try {
    await pending(btn, '…', async () => {
      if (wasStarred) {
        await api(`/api/auctions/favorites/${encodeURIComponent(assetId)}`, {method: 'DELETE'});
        toast('Unstarred', 'ok');
      } else {
        await api('/api/auctions/favorites', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({
            asset_id: assetId,
            link: sourceItem.link,
            title: sourceItem.title || sourceItem.raw_title,
            quantity: sourceItem.quantity,
            end_date: sourceItem.end_date || sourceItem.time_left || '',
            image_url: sourceItem.image_url,
            location: sourceItem.location,
          }),
        });
        toast(
          auc.telegramConfigured
            ? 'Starred — alerts armed'
            : 'Starred — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to receive pings',
          'ok'
        );
      }
    });
  } catch (err) {
    toast('Star toggle failed: ' + (err.message || err), 'err');
    return;
  }
  await loadFavorites();
  if (auc.items.length) renderAuctions();   // flip the star on the grid card
}

function renderFavoritesHead() {
  const strip = $('#auction-favorites-strip');
  if (!strip) return;
  strip.hidden = !auc.favorites.length;
  $('#auction-favorites-count').textContent = String(auc.favorites.length);
  const tg = $('#auction-favorites-tg');
  tg.className = 'fav-tg ' + (auc.telegramConfigured ? 'ok' : 'off');
  tg.title = auc.telegramConfigured ? '' : 'Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env';
  tg.textContent = auc.telegramConfigured ? '📡 Telegram alerts ON' : '⚠ Telegram not configured';
}

function favCardsHtml() { return auc.favorites.map(_renderFavoriteCard).join(''); }

function _renderFavoriteCard(fav) {
  const remaining = _fmtRemaining(fav.seconds_until_end);
  const isExpired = fav.seconds_until_end != null && fav.seconds_until_end <= 0;
  const noEnd = fav.seconds_until_end == null;
  const stateCls = isExpired ? 'expired' : (noEnd ? 'no-end' : 'live');

  const sentSet = new Set(fav.sent_intervals || []);
  const dots = (auc.intervals || []).map(label => {
    const fired = sentSet.has(label);
    return `<span class="fav-dot ${fired ? 'fired' : ''}" title="${label} alert${fired ? ' fired' : ' pending'}">${label}</span>`;
  }).join('');

  const img = fav.image_url
    ? `<img src="${esc(fav.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'auction-img-fallback',textContent:'🪑'}))">`
    : `<div class="auction-img-fallback">🪑</div>`;

  return `
    <article class="card card-fav ${stateCls}">
      <div class="fav-card-img">
        ${img}
        <button type="button" class="auction-star on" data-asset-id="${esc(fav.asset_id)}"
          title="Unstar — stops countdown alerts" aria-label="Unstar">★</button>
      </div>
      <div class="fav-card-body">
        <a href="${esc(fav.link)}" target="_blank" rel="noopener" class="fav-card-title">${esc(fav.title || '—')}</a>
        <div class="fav-card-meta">
          <span class="fav-qty">${(fav.quantity || 0).toLocaleString()} ×</span>
          <span class="fav-remaining ${stateCls}">${esc(remaining)}</span>
        </div>
        ${fav.location ? `<div class="auction-loc">📍 ${esc(fav.location)}</div>` : ''}
        <div class="fav-dots" title="Alert schedule (filled = sent)">${dots}</div>
      </div>
    </article>
  `;
}

hooks.loadProfiles = loadProfiles;
export {setScrapeStrip, connectScrapeStream};

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;

  // One-time migration: the old localStorage map toggle becomes ?map=0 (only "off" was ever remembered).
  try {
    const legacy = localStorage.getItem(LEGACY_MAP_KEY);
    if (legacy !== null) {
      localStorage.removeItem(LEGACY_MAP_KEY);
      if (legacy === 'off' && getParams().map == null) setParams({map: '0'});
    }
  } catch (_) {}

  $$('#auc-source .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      auc.source = btn.dataset.value;
      syncControls();
      writeParams();
      loadAuctions();
    });
  });

  $('#auc-profile').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn'); if (!btn) return;
    auc.profile = btn.dataset.value;
    const p = auc.profiles.find(x => x.slug === auc.profile);
    if (p) { $('#auc-min-qty').value = p.min_quantity; $('#auc-min-qty-out').textContent = p.min_quantity; }
    renderProfileSeg();
    writeParams();
    loadAuctions();
  });

  $('#auc-profile-new').addEventListener('click', () => { $('#auc-profile-form').hidden = false; });
  $('#pf-cancel').addEventListener('click', () => { $('#auc-profile-form').hidden = true; });

  $('#pf-save').addEventListener('click', (e) => pending(e.currentTarget, 'saving…', async () => {   // #9
    const body = {
      slug: $('#pf-slug').value, name: $('#pf-name').value, keywords: $('#pf-keywords').value,
      exclude_terms: $('#pf-exclude').value, search_terms: $('#pf-terms').value,
      native_category_ids: $('#pf-native').value, min_quantity: $('#pf-minqty').value,
      item_noun: $('#pf-noun').value || 'units',
    };
    try {
      const saved = await api('/api/profiles', {method: 'POST', body: JSON.stringify(body),
                                                headers: {'content-type': 'application/json'}});
      auc.profile = saved.slug;
      $('#auc-profile-form').hidden = true;
      await loadProfiles();
      toast(`profile ${saved.slug} saved`, 'ok');
      writeParams();
      loadAuctions();
    } catch (err) { toast(`save failed: ${err.message || err}`, 'err'); }
  }));

  $('#auc-profile-del').addEventListener('click', (e) => {   // #10
    if (!auc.profile || !confirm(`Delete profile "${auc.profile}"?`)) return;
    pending(e.currentTarget, '…', async () => {
      try {
        await api(`/api/profiles/${encodeURIComponent(auc.profile)}`, {method: 'DELETE'});
        auc.profile = '';
        await loadProfiles();
        writeParams();
        loadAuctions();
      } catch (err) { toast(`delete failed: ${err.message || err}`, 'err'); }
    });
  });

  let qTimer;
  $('#auc-q').addEventListener('input', (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => setSearch(e.target.value), 150);
  });

  $('#auc-min-qty').addEventListener('input', (e) => {
    $('#auc-min-qty-out').textContent = e.target.value;
  });

  $('#auc-min-qty').addEventListener('change', loadAuctions);
  $('#auc-n').addEventListener('change', loadAuctions);
  $('#auc-condition').addEventListener('change', loadAuctions);
  $('#auc-expired').addEventListener('change', loadAuctions);
  $('#auc-stale').addEventListener('change', loadAuctions);

  $('#auc-refresh').addEventListener('click', (e) => {   // #11
    pending(e.currentTarget, '↻ reloading…', async () => {
      try {
        await api('/api/auctions/refresh', {method: 'POST'});
        await loadAuctions();
      } catch (err) {
        toast(`Reload failed: ${err.message || err}`, 'err');
      }
    });
  });

  $('#staleness-scrape').addEventListener('click', (e) => startScrape('gd', false, e.currentTarget));   // #12

  $('#scrape-toggle').addEventListener('click', (e) => {
    e.stopPropagation();
    ddMenu.hidden = !ddMenu.hidden;
  });

  document.addEventListener('click', (e) => {
    if (!dd.contains(e.target)) ddMenu.hidden = true;
  });

  $$('.dropdown-menu button', dd).forEach(btn => {
    btn.addEventListener('click', async () => {
      ddMenu.hidden = true;
      const raw = btn.dataset.scrape;
      let source = raw, test = false;
      if (raw === 'ps-test') { source = 'ps'; test = true; }
      await startScrape(source, test, $('#scrape-toggle'));
    });
  });

  $('#scrape-cancel').addEventListener('click', (e) => {   // #13
    pending(e.currentTarget, '…cancelling', async () => {
      try {
        await api('/api/scrape/cancel', {method: 'POST'});
        toast('Scrape cancel requested.', 'info');
      } catch (err) {
        toast('Cancel failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#auc-queue-all').addEventListener('click', (e) => {
    const urls = visibleItems()
      .map(it => it.link)
      .filter(u => typeof u === 'string' && u.includes('govdeals.com'));
    if (!urls.length) {
      toast('Nothing to queue — only GovDeals lots can run through the pipeline.', 'err');
      return;
    }
    pending(e.currentTarget, `…queuing ${urls.length}`, async () => {
      try {
        await queueRuns(urls);
        toast(`Queued ${urls.length} lot${urls.length === 1 ? '' : 's'}. Watch Launcher tab.`, 'ok');
      } catch (err) {
        toast('Queue failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#auc-map-toggle').addEventListener('click', () => {
    setAucMapOn(!auc.mapOn);
    writeParams();
  });

  // Favorites strip: star (unstar) is delegated because the cards are re-rendered by UI.load; test ping (#20).
  $('#auction-favorites-grid').addEventListener('click', (e) => {
    const btn = e.target.closest('.auction-star'); if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    const fav = auc.favorites.find(f => f.asset_id === btn.dataset.assetId);
    if (!fav) return;
    toggleFavorite(fav.asset_id, {
      link: fav.link, title: fav.title, quantity: fav.quantity,
      end_date: fav.end_date_raw, image_url: fav.image_url, location: fav.location,
    }, btn);
  });
  $('#auction-favorites-test').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    try {
      await pending(btn, 'sending…', () => api('/api/auctions/favorites/test-telegram', {method: 'POST'}));
      toast('Test message sent — check Telegram', 'ok');
      btn.textContent = '✓ sent';
      setTimeout(() => { btn.textContent = 'test ping'; }, 2000);
    } catch (err) {
      toast('Test failed: ' + (err.message || err), 'err');
    }
  });

  // Refresh the strip's countdown numbers without re-fetching the (slow) auctions call. Cheap GET, 30 s,
  // only while the pane is on screen; the dots reflect server-side sent state. Failure → stale badge (#17).
  setInterval(() => {
    const pane = $('[data-pane="auctions"]');
    if (pane && !pane.hidden && document.visibilityState === 'visible') loadFavorites({poll: true});
  }, 30000);
}

export function load() {
  readParams();
  syncControls();
  loadAuctions();
  applyMapParam();
}
