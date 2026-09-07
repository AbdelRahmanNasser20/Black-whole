// static/admin/auctions.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, esc, SOURCE_NAMES, _ageInDays, _fmtAge, _fmtRemaining, queueRuns, hooks} from './shared.js';

let scrapeES;
// `deal` (Deals-tab state) is read by loadProfiles(); bound at mount() via the shared hooks registry.
let deal;

// ───────── auctions ─────────

const auc = {
  source: 'gd',
  profile: '',            // research profile slug (research_profiles); '' = default
  profiles: [],           // rows from /api/profiles
  defaultProfile: 'chairs',
  items: [],
  stats: null,
  loading: false,
  favorites: [],          // list of favorite dicts from /api/auctions/favorites
  favoriteIds: new Set(), // asset_id strings — for fast "is starred?" lookup
  intervals: [],          // alert interval labels in display order
  telegramConfigured: false,
  mapOn: false,           // 🗺 map toggle — cards follow the map viewport
  map: null,              // AdminMap handle (lazy-mounted)
};

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

// Build an eBay sold-listings search URL from an auction row. Used on the
// MEDICAL sub-tab as the profitability-test hook: GovDeals doesn't expose
// final winning bids, so we send the operator straight to the demand side.
// Strips quantity prefixes ("Lot of 3x …") and trailing seller codes that
// would otherwise dilute the eBay match.
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

// ── research profiles (what we're hunting for) — /api/profiles ──
// Picking a profile sets the min-qty slider to its floor (chairs 50, medical 1).
async function loadProfiles() {
  const body = await apiFetch('/api/profiles');
  auc.profiles = body.profiles || [];
  auc.defaultProfile = body.default || 'chairs';
  if (!auc.profile || !auc.profiles.some(p => p.slug === auc.profile)) auc.profile = auc.defaultProfile;
  renderProfileSeg();
  const sel = $('#deal-profile');
  if (sel) {
    sel.innerHTML = '<option value="">any profile</option>' +
      auc.profiles.map(p => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('');
    sel.value = deal.profile || '';
  }
  return auc.profiles;
}

function renderProfileSeg() {
  const seg = $('#auc-profile');
  seg.innerHTML = auc.profiles.map(p =>
    `<button type="button" class="seg-btn ${p.slug === auc.profile ? 'active' : ''}" data-value="${esc(p.slug)}"
       title="${esc((p.keywords || []).join(', '))} · min ${p.min_quantity}">${esc(p.name)}</button>`).join('');
  $('#auc-profile-del').disabled = !!(auc.profiles.find(p => p.slug === auc.profile) || {}).is_default;
}

// ── scrape dropdown ──

const dd = $('#scrape-dropdown');
const ddMenu = $('.dropdown-menu', dd);

async function startScrape(source, test) {
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
    await apiFetch('/api/scrape/start', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({source, test, profile: auc.profile || auc.defaultProfile}),
    });
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
    const label = s.stage_detail
      ? `${s.current_stage} · ${s.stage_detail}`
      : s.current_stage;
    stageEl.textContent = label;
    stageEl.hidden = false;
  } else {
    stageEl.textContent = '';
    stageEl.hidden = true;
  }

  $('#scrape-tail').textContent = (s.last_line || '').slice(-160);
  $('#scrape-cancel').hidden = (s.status !== 'running');
}

function connectScrapeStream() {
  if (scrapeES) scrapeES.close();
  scrapeES = new EventSource('/api/scrape/stream');
  scrapeES.addEventListener('stdout', (e) => handleScrapeLine(e, 'stdout'));
  scrapeES.addEventListener('stderr', (e) => handleScrapeLine(e, 'stderr'));
  scrapeES.addEventListener('system', (e) => handleScrapeLine(e, 'system'));
  scrapeES.addEventListener('event', (e) => {
    try {
      const msg = JSON.parse(e.data);
      const data = msg.data || {};
      if (data.kind === 'scrape') {
        // Refresh full state from server, then reload the card grid.
        fetch('/api/scrape/state').then(r => r.json()).then(setScrapeStrip);
        if (data.status === 'finished' || data.status === 'error' || data.status === 'cancelled') {
          loadAuctions();
        }
      } else if (data.kind === 'scrape_stage') {
        // Live stage update without a full state refetch.
        const stageEl = $('#scrape-stage');
        if (data.stage) {
          const label = data.detail ? `${data.stage} · ${data.detail}` : data.stage;
          stageEl.textContent = label;
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

function renderCacheHeader(stats, maxStaleDays) {
  const host = $('#auction-cache-stats');
  if (!host) return;
  if (!stats || !stats.total) {
    host.hidden = false;
    host.removeAttribute('data-freshness');
    host.innerHTML = `<span class="ch-total">0 lots in cache</span>
      <span class="ch-age">hit <strong>⟳ scrape now</strong> to populate</span>`;
    return;
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

  host.hidden = false;
  host.dataset.freshness = freshness;
  host.innerHTML = `
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
  try {
    const stats = await apiFetch('/api/auctions/cache-stats');
    auc.stats = stats;
    renderCacheHeader(stats, Number($('#auc-stale').value) || 7);
    renderStalenessBanner(stats, Number($('#auc-stale').value) || 7);
    return stats;
  } catch (err) {
    console.warn('cache-stats failed', err);
    return null;
  }
}

async function loadAuctions() {
  if (auc.loading) return;
  auc.loading = true;
  const grid = $('#auction-grid');
  const status = $('#auction-status');
  const summary = $('#auction-filter-summary');
  const useCond = $('#auc-condition').checked;
  const maxStaleDays = Number($('#auc-stale').value) || 7;
  status.innerHTML = useCond
    ? '<span class="pulse">●</span> Loading auctions (condition scoring may take 3–10s)…'
    : '<span class="pulse">●</span> Loading auctions…';
  grid.innerHTML = '<div class="drafts-empty loading"><span class="spinner"></span> fetching listings from cache…</div>';
  if (summary) summary.hidden = true;

  const qs = new URLSearchParams({
    source: auc.source,
    n: $('#auc-n').value,
    min_qty: $('#auc-min-qty').value,
    condition: useCond ? '1' : '0',
    active_only: $('#auc-expired').checked ? '0' : '1',
    max_stale_days: String(maxStaleDays),
  });
  try {
    if (!auc.profiles.length) await loadProfiles();
    qs.set('profile', auc.profile || auc.defaultProfile);
    const [body, _stats, _favs] = await Promise.all([
      apiFetch('/api/auctions?' + qs.toString()),
      fetchCacheStats(),
      loadFavorites(),
    ]);
    auc.items = body.items || [];
    status.textContent = `${auc.items.length} shown · ${body.cached ? `cached ${body.age}s ago` : 'fresh'}`;
    renderAuctions(auc.items, maxStaleDays);
    renderFilterSummary(auc.items.length, maxStaleDays);
    syncAuctionMap();
  } catch (err) {
    status.textContent = '';
    grid.innerHTML = `<div class="drafts-empty">Error loading auctions: ${err.message || err}</div>`;
    toast(`Auction load failed: ${err.message || err}`, 'err');
  } finally {
    auc.loading = false;
  }
}

function renderFilterSummary(shownCount, maxStaleDays) {
  const summary = $('#auction-filter-summary');
  if (!summary) return;
  const stats = auc.stats;
  if (!stats || !stats.total) { summary.hidden = true; return; }

  const srcKey = auc.source; // 'gd' | 'ps'
  const srcCount = stats.by_source?.[srcKey]?.count ?? 0;

  if (shownCount === 0 && srcCount > 0) {
    // Figure out the most likely culprit.
    const ageDays = _ageInDays(stats.by_source?.[srcKey]?.newest_seen_at);
    const activeOnly = !$('#auc-expired').checked;
    const reasons = [];
    if (activeOnly && ageDays != null && ageDays > maxStaleDays) {
      reasons.push(`<span class="fs-bad">staleness</span> (newest ${srcKey} row is ${_fmtAge(ageDays)}, filter hides anything past ${maxStaleDays} days)`);
    }
    if ($('#auc-min-qty').value > 50) {
      reasons.push(`<span class="fs-bad">min-units</span> set to ${$('#auc-min-qty').value}`);
    }
    if (activeOnly) {
      reasons.push(`<span class="fs-hint">“Show ended auctions”</span> is off`);
    }
    const hint = reasons.length
      ? `Likely culprit: ${reasons.join(' · ')}`
      : `Try lowering filters or hit ⟳ scrape now.`;
    summary.hidden = false;
    summary.innerHTML =
      `${srcCount.toLocaleString()} ${srcKey} lots in cache, filters excluded all of them. ${hint}`;
  } else if (shownCount > 0 && shownCount < srcCount) {
    summary.hidden = false;
    summary.innerHTML = `Showing ${shownCount} of ${srcCount.toLocaleString()} ${srcKey} lots (ranked by quantity).`;
  } else {
    summary.hidden = true;
  }
}

function renderAuctions(items, maxStaleDays) {
  const grid = $('#auction-grid');
  if (!items.length) {
    const stats = auc.stats;
    const total = stats?.by_source?.[auc.source]?.count ?? 0;
    const msg = total === 0
      ? `Cache is empty for ${SOURCE_NAMES[auc.source] || auc.source}. Hit ⟳ scrape now to populate.`
      : `No listings matched the current filters. See details above.`;
    grid.innerHTML = `<div class="drafts-empty">${msg}</div>`;
    return;
  }
  grid.innerHTML = '';
  for (const it of items) grid.appendChild(renderAuctionCard(it));
}

// ── Auctions map (GovAuctions-style: pins cluster, cards follow the viewport) ──

function auctionMapPopup(it) {
  const loc = [it.location, it.pickup_zip].filter(Boolean).join(' · ');
  const img = it.image_url
    ? `<img class="amap-popup-img" src="${esc(it.image_url)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`
    : '';
  return `${img}
    <strong>${esc(it.title || it.raw_title || '—')}</strong><br>
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

// Cards follow the viewport: unmapped lots always stay visible (a missing
// zip must never hide a good lot), mapped ones must be inside the bounds.
function applyAuctionViewport() {
  if (!auc.mapOn || !auc.map) return;
  const maxStaleDays = Number($('#auc-stale').value) || 7;
  renderAuctions(
    auc.items.filter(it => it.lat == null || auc.map.inBounds(it)),
    maxStaleDays,
  );
  updateAuctionMapNote();
}

function syncAuctionMap(fit = false) {
  if (!auc.mapOn || !auc.map) return;
  auc.map.setPoints(auc.items.map(it => ({
    lat: it.lat, lng: it.lng,
    title: it.title || it.raw_title || '',
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
    // back to the plain full list
    renderAuctions(auc.items, Number($('#auc-stale').value) || 7);
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

// Map is on by default; only an explicit toggle-off is remembered.
function autoOpenAucMap() {
  let pref = null;
  try { pref = localStorage.getItem('admin.aucMapOn'); } catch (_) {}
  if (pref === 'off') return;
  if (!auc.mapOn) setAucMapOn(true);
  else if (auc.map) auc.map.invalidateSize();  // pane was hidden while away
}

function renderAuctionCard(it) {
  const card = document.createElement('article');
  card.className = 'auction-card';

  const cond = it.condition;
  let condCls = '', condPill = '';
  if (cond != null) {
    condCls = cond >= 7 ? 'good' : (cond >= 5 ? 'ok' : 'bad');
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

  // Compose "Location · ZIP" line. The cached `location` is already
  // "City, State, Country"; we append the ZIP from the asset detail page
  // when present (newer rows only — older cache entries leave it blank).
  const locParts = [];
  if (it.location) locParts.push(it.location);
  if (it.pickup_zip) locParts.push(it.pickup_zip);
  const locLine = locParts.join(' · ');

  const assetId = _assetIdFromLink(it.link);
  const isStarred = assetId && auc.favoriteIds.has(assetId);

  card.innerHTML = `
    <div class="auction-img">
      ${img}
      ${assetId ? `<button class="auction-star ${isStarred ? 'on' : ''}"
        data-asset-id="${esc(assetId)}"
        title="${isStarred ? 'Unstar — stops countdown alerts' : 'Star — get Telegram pings as the auction winds down'}"
        aria-label="${isStarred ? 'Unstar' : 'Star'}">${isStarred ? '★' : '☆'}</button>` : ''}
    </div>
    <div class="auction-body">
      <h3 class="auction-title">${esc(it.title || it.raw_title || '—')}</h3>
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
        <button class="btn btn-small btn-primary auction-launch"
                ${launchDisabled ? 'disabled' : ''}
                title="${esc(launchTitle)}"
                data-url="${esc(it.link)}">▶ launch</button>
      </div>
    </div>
  `;

  const starBtn = card.querySelector('.auction-star');
  if (starBtn) {
    starBtn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      starBtn.disabled = true;
      try {
        await toggleFavorite(assetId, it);
      } finally {
        starBtn.disabled = false;
      }
    });
  }

  const launchBtn = card.querySelector('.auction-launch');
  if (launchBtn && !launchDisabled) {
    launchBtn.addEventListener('click', async () => {
      const orig = launchBtn.textContent;
      launchBtn.disabled = true;
      launchBtn.textContent = '⏱ queuing…';
      launchBtn.classList.add('is-loading');
      try {
        await queueRuns([it.link]);
        // Permanent per-session badge so it's clear the lot is already in.
        launchBtn.textContent = '✓ queued';
        launchBtn.classList.add('queued');
        toast(`Queued: ${it.title || it.link}`, 'ok');
      } catch (err) {
        launchBtn.textContent = orig;
        launchBtn.disabled = false;
        toast('Queue failed: ' + (err.message || err), 'err');
      } finally {
        launchBtn.classList.remove('is-loading');
      }
    });
  }
  return card;
}

// ─────────── auction favorites + countdown alerts ───────────

async function loadFavorites() {
  try {
    const body = await apiFetch('/api/auctions/favorites');
    auc.favorites = body.items || [];
    auc.favoriteIds = new Set(auc.favorites.map(f => f.asset_id));
    auc.intervals = body.intervals || [];
    auc.telegramConfigured = !!body.telegram_configured;
    renderFavoritesStrip();
  } catch (err) {
    console.warn('Favorites load failed:', err);
  }
}

async function toggleFavorite(assetId, sourceItem) {
  if (!assetId) return;
  const wasStarred = auc.favoriteIds.has(assetId);
  try {
    if (wasStarred) {
      await apiFetch(`/api/auctions/favorites/${encodeURIComponent(assetId)}`, {
        method: 'DELETE',
      });
      toast('Unstarred', 'ok');
    } else {
      await apiFetch('/api/auctions/favorites', {
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
  } catch (err) {
    toast('Star toggle failed: ' + (err.message || err), 'err');
    return;
  }
  await loadFavorites();
  // Re-render the auctions grid so the star icon flips state.
  renderAuctions(auc.items, Number($('#auc-stale').value) || 7);
}

function renderFavoritesStrip() {
  let strip = $('#auction-favorites-strip');
  const grid = $('#auction-grid');
  const host = grid?.parentElement;
  if (!host) return;

  if (!auc.favorites.length) {
    if (strip) strip.remove();
    return;
  }

  if (!strip) {
    strip = document.createElement('section');
    strip.id = 'auction-favorites-strip';
    strip.className = 'fav-strip';
    host.insertBefore(strip, grid);
  }

  const tgPill = auc.telegramConfigured
    ? '<span class="fav-tg ok">📡 Telegram alerts ON</span>'
    : '<span class="fav-tg off" title="Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env">⚠ Telegram not configured</span>';

  const cards = auc.favorites.map(_renderFavoriteCard).join('');
  strip.innerHTML = `
    <header class="fav-strip-head">
      <div class="fav-strip-title">★ FAVORITES <span class="fav-count">${auc.favorites.length}</span></div>
      <div class="fav-strip-meta">
        ${tgPill}
        <button class="btn btn-small fav-test-tg" type="button">test ping</button>
      </div>
    </header>
    <div class="fav-strip-grid">${cards}</div>
  `;

  strip.querySelector('.fav-test-tg').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = 'sending…';
    try {
      await apiFetch('/api/auctions/favorites/test-telegram', {method: 'POST'});
      toast('Test message sent — check Telegram', 'ok');
      btn.textContent = '✓ sent';
    } catch (err) {
      toast('Test failed: ' + (err.message || err), 'err');
      btn.textContent = 'test ping';
    } finally {
      setTimeout(() => { btn.disabled = false; btn.textContent = 'test ping'; }, 2000);
    }
  });

  strip.querySelectorAll('.fav-card .auction-star').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault(); e.stopPropagation();
      const assetId = btn.dataset.assetId;
      const fav = auc.favorites.find(f => f.asset_id === assetId);
      if (!fav) return;
      btn.disabled = true;
      try {
        await toggleFavorite(assetId, {
          link: fav.link, title: fav.title, quantity: fav.quantity,
          end_date: fav.end_date_raw, image_url: fav.image_url,
          location: fav.location,
        });
      } finally {
        btn.disabled = false;
      }
    });
  });
}

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
    <article class="fav-card ${stateCls}">
      <div class="fav-card-img">
        ${img}
        <button class="auction-star on" data-asset-id="${esc(fav.asset_id)}"
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
  deal = hooks.deal;
  $$('#auc-source .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#auc-source .seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      auc.source = btn.dataset.value;
      loadAuctions();
    });
  });

  $('#auc-profile').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn'); if (!btn) return;
    auc.profile = btn.dataset.value;
    const p = auc.profiles.find(x => x.slug === auc.profile);
    if (p) { $('#auc-min-qty').value = p.min_quantity; $('#auc-min-qty-out').textContent = p.min_quantity; }
    renderProfileSeg();
    loadAuctions();
  });

  $('#auc-profile-new').addEventListener('click', () => { $('#auc-profile-form').hidden = false; });
  $('#pf-cancel').addEventListener('click', () => { $('#auc-profile-form').hidden = true; });

  $('#pf-save').addEventListener('click', (e) => withButtonLoading(e.currentTarget, 'saving…', async () => {
    const body = {
      slug: $('#pf-slug').value, name: $('#pf-name').value, keywords: $('#pf-keywords').value,
      exclude_terms: $('#pf-exclude').value, search_terms: $('#pf-terms').value,
      native_category_ids: $('#pf-native').value, min_quantity: $('#pf-minqty').value,
      item_noun: $('#pf-noun').value || 'units',
    };
    try {
      const saved = await apiFetch('/api/profiles', {method: 'POST', body: JSON.stringify(body),
                                                     headers: {'content-type': 'application/json'}});
      auc.profile = saved.slug;
      $('#auc-profile-form').hidden = true;
      await loadProfiles();
      toast(`profile ${saved.slug} saved`, 'ok');
      loadAuctions();
    } catch (err) { toast(`save failed: ${err.message || err}`, 'err'); }
  }));

  $('#auc-profile-del').addEventListener('click', async () => {
    if (!auc.profile || !confirm(`Delete profile "${auc.profile}"?`)) return;
    try {
      await apiFetch(`/api/profiles/${encodeURIComponent(auc.profile)}`, {method: 'DELETE'});
      auc.profile = '';
      await loadProfiles();
      loadAuctions();
    } catch (err) { toast(`delete failed: ${err.message || err}`, 'err'); }
  });

  $('#auc-min-qty').addEventListener('input', (e) => {
    $('#auc-min-qty-out').textContent = e.target.value;
  });

  $('#auc-min-qty').addEventListener('change', loadAuctions);
  $('#auc-n').addEventListener('change', loadAuctions);
  $('#auc-condition').addEventListener('change', loadAuctions);
  $('#auc-expired').addEventListener('change', loadAuctions);
  $('#auc-stale').addEventListener('change', loadAuctions);

  $('#auc-refresh').addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '↻ reloading…', async () => {
      try {
        await apiFetch('/api/auctions/refresh', {method: 'POST'});
        await loadAuctions();
      } catch (err) {
        toast(`Reload failed: ${err.message || err}`, 'err');
      }
    });
  });

  $('#staleness-scrape').addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '⟳ starting…', () => startScrape('gd', false));
  });

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
      await startScrape(source, test);
    });
  });

  $('#scrape-cancel').addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '…cancelling', async () => {
      try {
        await apiFetch('/api/scrape/cancel', {method: 'POST'});
        toast('Scrape cancel requested.', 'info');
      } catch (err) {
        toast('Cancel failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#auc-queue-all').addEventListener('click', (e) => {
    const urls = auc.items
      .map(it => it.link)
      .filter(u => typeof u === 'string' && u.includes('govdeals.com'));
    if (!urls.length) {
      toast('Nothing to queue — only GovDeals lots can run through the pipeline.', 'err');
      return;
    }
    withButtonLoading(e.currentTarget, `…queuing ${urls.length}`, async () => {
      try {
        await queueRuns(urls);
        toast(`Queued ${urls.length} lot${urls.length === 1 ? '' : 's'}. Watch Launcher tab.`, 'ok');
      } catch (err) {
        toast('Queue failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#auc-map-toggle').addEventListener('click', () => {
    const on = !auc.mapOn;
    try { localStorage.setItem('admin.aucMapOn', on ? 'on' : 'off'); } catch (_) {}
    setAucMapOn(on);
  });

  // Periodically refresh the strip's countdown numbers without re-fetching the
  // (slow) auctions LLM call. Cheap GET; the dots reflect server-side sent state.
  setInterval(() => {
    if ($('#auction-grid')) loadFavorites();
  }, 30000);
}

export function load() { loadAuctions(); autoOpenAucMap(); }
