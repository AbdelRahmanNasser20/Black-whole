// listing_automation · console
// Vanilla JS. SSE for streaming. No framework, no bundle.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ───────── feedback primitives ─────────

// Non-blocking inline toast. Replaces alert(). kind: info | ok | err.
function toast(message, kind = 'info', ttlMs = 4000) {
  const host = $('#toast-container');
  if (!host) { console.warn('toast host missing:', message); return; }
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  el.textContent = message;
  host.appendChild(el);
  // Kick the slide-in on next frame so CSS transitions engage.
  requestAnimationFrame(() => el.classList.add('toast-in'));
  const dismiss = () => {
    el.classList.remove('toast-in');
    el.classList.add('toast-out');
    el.addEventListener('transitionend', () => el.remove(), {once: true});
  };
  el.addEventListener('click', dismiss);
  setTimeout(dismiss, ttlMs);
}

// Disable + relabel a button while `fn` runs. Always restores label and
// disabled state, even if fn throws. Returns fn's return value.
async function withButtonLoading(btn, loadingText, fn) {
  if (!btn) return fn();
  const orig = btn.textContent;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  if (loadingText) btn.textContent = loadingText;
  btn.classList.add('is-loading');
  try {
    return await fn();
  } finally {
    btn.disabled = origDisabled;
    btn.textContent = orig;
    btn.classList.remove('is-loading');
  }
}

// Fetch wrapper that throws on !ok with a useful message. Callers can
// try/catch and toast the error.
async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

// ───────── tabs ─────────

const panels = {
  launcher: $('[data-pane="launcher"]'),
  drafts:   $('[data-pane="drafts"]'),
  auctions: $('[data-pane="auctions"]'),
  inventory: $('[data-pane="inventory"]'),
  inquiries: $('[data-pane="inquiries"]'),
  'listings-db': $('[data-pane="listings-db"]'),
  'test-scrape': $('[data-pane="test-scrape"]'),
  subscribers: $('[data-pane="subscribers"]'),
  deals: $('[data-pane="deals"]'),
  tracking: $('[data-pane="tracking"]'),
};

const TAB_STORAGE_KEY = 'admin.lastTab';

function activateTab(name, {persist = true} = {}) {
  const btn = $$('.tab').find(t => t.dataset.tab === name);
  if (!btn) return false;
  $$('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  Object.entries(panels).forEach(([k, el]) => {
    if (el) el.hidden = (k !== name);
  });
  if (name === 'drafts') loadDrafts();
  if (name === 'auctions') { loadAuctions(); autoOpenAucMap(); }
  if (name === 'inventory') loadInventory();
  if (name === 'inquiries') loadInquiries();
  if (name === 'subscribers') loadSubscribers();
  if (name === 'deals') { loadDeals(); autoOpenDealMap(); }
  if (name === 'tracking') loadTracking();
  if (name === 'listings-db') loadListingsDb();
  if (name === 'test-scrape') $('#ts-q')?.focus();
  if (persist) {
    try { localStorage.setItem(TAB_STORAGE_KEY, name); } catch (_) {}
  }
  return true;
}

$$('.tab').forEach(btn => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

// Restore the last tab on load. Falls back to whichever tab the markup
// rendered as `.active` (typically 01 Launcher) if storage is empty or the
// saved tab no longer exists in the DOM.
//
// NOTE: this is only *defined* here — it is invoked at the very bottom of the
// file. activateTab() calls per-tab loaders (loadAuctions, loadInventory, …)
// that read module-level `const` state (e.g. `auc`) declared further down. If
// we invoked restoreLastTab here, restoring a saved tab like "auctions" would
// call loadAuctions() before `const auc` is initialized → a temporal-dead-zone
// ReferenceError that silently wedges the tab on its loading placeholder.
function restoreLastTab() {
  let saved = null;
  try { saved = localStorage.getItem(TAB_STORAGE_KEY); } catch (_) {}
  if (saved && saved !== 'launcher') {
    activateTab(saved, {persist: false});
  }
}

// ───────── clock ─────────

setInterval(() => {
  const d = new Date();
  $('#clock').textContent = d.toTimeString().slice(0, 8);
}, 1000);

// ───────── launcher ─────────

const consoleEl = $('#console');
const showEvents = $('#show-events');
const autoscroll = $('#autoscroll');

function _bindToggleIndicator(checkbox, indicatorId) {
  const ind = document.getElementById(indicatorId);
  if (!checkbox || !ind) return;
  const sync = () => {
    ind.dataset.on = checkbox.checked ? '1' : '0';
    ind.textContent = checkbox.checked ? 'ON' : 'OFF';
  };
  checkbox.addEventListener('change', sync);
  sync();
}
_bindToggleIndicator(showEvents, 'show-events-state');
_bindToggleIndicator(autoscroll, 'autoscroll-state');

function appendLine(stream, data) {
  // Hide raw event lines if user toggled off
  if (stream === 'event' && !showEvents.checked) return;

  const line = document.createElement('span');
  line.className = `console-line ${stream}`;
  const ts = new Date().toTimeString().slice(0, 8);

  let body;
  if (stream === 'event') {
    const ev = typeof data === 'string' ? JSON.parse(data) : data;
    body = `${ev.kind}${ev.phase ? ':' + ev.phase : ''} → ${
      Object.entries(ev)
        .filter(([k]) => !['ts','kind','phase'].includes(k))
        .map(([k,v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join(' ')
    }`;
  } else {
    body = data;
  }

  line.innerHTML = `<span class="ts">${ts}</span><span class="tag">${stream}</span><span class="body"></span>`;
  line.querySelector('.body').textContent = body;
  consoleEl.appendChild(line);

  if (autoscroll.checked) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function setPhase(name, status, extras = {}) {
  const card = document.querySelector(`.phase[data-phase="${name}"]`);
  if (!card) return;
  card.dataset.state = status;
  const pill = $('.phase-status', card);
  pill.textContent = status;
  pill.dataset.state = status;

  const body = $('[data-body]', card);
  body.innerHTML = '';
  const dl = document.createElement('dl');
  dl.className = 'kv';
  for (const [k, v] of Object.entries(extras)) {
    if (k === 'status' || v == null || v === '') continue;
    const dt = document.createElement('dt');  dt.textContent = k;
    const dd = document.createElement('dd');
    if ((k === 'url' || k === 'fb_url' || k === 'ebay_url') && typeof v === 'string') {
      const a = document.createElement('a');
      a.href = v; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = v.length > 60 ? v.slice(0, 57) + '…' : v;
      dd.appendChild(a);
    } else if (typeof v === 'object') {
      dd.textContent = JSON.stringify(v).slice(0, 90);
    } else {
      dd.textContent = String(v);
    }
    dl.appendChild(dt); dl.appendChild(dd);
  }
  body.appendChild(dl);
}

function applyState(s) {
  if (s.phases) {
    for (const [name, info] of Object.entries(s.phases)) {
      setPhase(name, info.status || 'pending', info);
    }
  }
  $('#run-btn').disabled = (s.status === 'running');
  $('#cancel-btn').disabled = (s.status !== 'running');
  if (s.suggested_price && s.confirmed_price == null) {
    $('#price-prompt').hidden = false;
    $('#pp-suggested').textContent = `$${s.suggested_price}`;
    $('#pp-input').value = s.suggested_price;
  } else if (s.confirmed_price != null) {
    $('#price-prompt').hidden = true;
  }
  if (s.queue !== undefined) renderQueueStrip(s.queue);
}

function renderQueueStrip(queue) {
  const strip = $('#queue-strip');
  if (!strip) return;
  if (!queue || !queue.length) {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;
  $('#queue-count').textContent = String(queue.length);
  const urlsEl = $('#queue-urls');
  urlsEl.innerHTML = '';
  queue.slice(0, 3).forEach(item => {
    const chip = document.createElement('span');
    chip.className = 'queue-chip';
    chip.title = item.url;
    chip.textContent = shortUrl(item.url);
    urlsEl.appendChild(chip);
  });
  if (queue.length > 3) {
    const more = document.createElement('span');
    more.className = 'queue-chip queue-chip-more';
    more.textContent = `+${queue.length - 3}`;
    urlsEl.appendChild(more);
  }
}

function shortUrl(u) {
  try {
    const url = new URL(u);
    const parts = url.pathname.split('/').filter(Boolean);
    return parts.slice(-2).join('/') || url.host;
  } catch { return u.slice(-40); }
}

// ── form ──
$('#launch-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const pipeline = fd.get('mode_pipeline') === 'on';
  const channels = ['site', 'fb', 'business'].filter(c => fd.get('ch_' + c) === 'on');
  const payload = pipeline ? {
    mode: 'pipeline',
    url: fd.get('url'),
    skip_dewatermark: fd.get('skip_dewatermark') === 'on',
    skip_fb: fd.get('skip_fb') === 'on',
    skip_ebay: fd.get('skip_ebay') === 'on',
    price: fd.get('price') ? parseInt(fd.get('price'), 10) : null,
  } : {
    mode: 'channels',
    url: fd.get('url'),
    price: fd.get('price') ? parseInt(fd.get('price'), 10) : null,
    title: (fd.get('title') || '').trim(),
    blurb: (fd.get('blurb') || '').trim(),
    split: (fd.get('split') || '').trim(),
    channels,
  };
  consoleEl.innerHTML = '';
  $$('.phase').forEach(p => setPhase(p.dataset.phase, 'pending', {}));

  const res = await fetch('/api/runs/start', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({detail: res.statusText}));
    appendLine('stderr', `[start failed] ${err.detail || res.statusText}`);
    return;
  }
  $('#run-btn').disabled = true;
  $('#cancel-btn').disabled = false;
});

// ▶ is "list everywhere" by default; the legacy scrape/eBay pipeline is a checkbox.
const _modeBox = document.querySelector('#launch-form [name="mode_pipeline"]');
if (_modeBox) {
  const sync = () => {
    const on = _modeBox.checked;
    document.querySelectorAll('#launch-form .pipeline-only').forEach(el => el.hidden = !on);
    document.querySelectorAll('#launch-form .opts-copy').forEach(el => el.hidden = on);
    const lbl = $('#run-btn-label');
    if (lbl) lbl.textContent = on ? 'Run pipeline' : 'List everywhere';
  };
  _modeBox.addEventListener('change', sync);
  sync();
}

$('#cancel-btn').addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '…cancelling', async () => {
    try {
      await apiFetch('/api/runs/cancel', {method: 'POST'});
      toast('Cancel requested.', 'info');
    } catch (err) {
      toast('Cancel failed: ' + (err.message || err), 'err');
    }
  });
});

$('#queue-clear').addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '…clearing', async () => {
    try {
      await apiFetch('/api/runs/queue/clear', {method: 'POST'});
      toast('Run queue cleared.', 'ok');
    } catch (err) {
      toast('Clear failed: ' + (err.message || err), 'err');
    }
  });
});

$('#pp-confirm').addEventListener('click', (e) => {
  const raw = $('#pp-input').value.trim();
  const v = parseInt(raw, 10);
  if (!raw || !Number.isFinite(v) || v <= 0) {
    toast('Enter a positive number first.', 'err');
    return;
  }
  withButtonLoading(e.currentTarget, '…sending', async () => {
    try {
      await apiFetch('/api/runs/stdin', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({line: String(v)}),
      });
      $('#price-prompt').hidden = true;
    } catch (err) {
      toast('Failed to send: ' + (err.message || err), 'err');
    }
  });
});

// ── SSE ──
let es;
function connectStream() {
  if (es) es.close();
  es = new EventSource('/api/runs/stream');
  const dot = $('#conn-dot');

  es.addEventListener('open', () => dot.classList.add('live'));
  es.addEventListener('error', () => dot.classList.remove('live'));

  es.addEventListener('queue', (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.data && msg.data.queue !== undefined) renderQueueStrip(msg.data.queue);
    } catch (err) { console.warn(err); }
  });

  for (const stream of ['stdout', 'stderr', 'event', 'system']) {
    es.addEventListener(stream, (e) => {
      try {
        const msg = JSON.parse(e.data);
        appendLine(msg.stream, msg.data);
        if (msg.stream === 'event') {
          const ev = msg.data;
          if (ev.kind === 'phase') {
            setPhase(ev.phase, ev.status || 'pending', ev);
          } else if (ev.kind === 'price' && ev.suggested && ev.confirmed == null) {
            $('#price-prompt').hidden = false;
            $('#pp-suggested').textContent = `$${ev.suggested}`;
            $('#pp-input').value = ev.suggested;
          } else if (ev.kind === 'price' && ev.confirmed != null) {
            // Auto-accept fired or another client confirmed — drop the stale UI.
            $('#price-prompt').hidden = true;
          } else if (ev.kind === 'run' && ev.status === 'finished') {
            $('#run-btn').disabled = false;
            $('#cancel-btn').disabled = true;
          }
        }
      } catch (err) { console.warn(err); }
    });
  }
}

// ── boot ──
let scrapeES;
fetch('/api/runs/state').then(r => r.json()).then(applyState).catch(() => {});
connectStream();
fetch('/api/scrape/state').then(r => r.json()).then(setScrapeStrip).catch(() => {});
connectScrapeStream();

// ───────── drafts ─────────

async function loadDrafts() {
  const grid = $('#drafts-grid');
  grid.innerHTML = '<div class="drafts-empty">Scanning ~/Desktop/Banquet chiars Pictures…</div>';
  const res = await fetch('/api/drafts');
  const {drafts} = await res.json();
  if (!drafts.length) {
    grid.innerHTML = '<div class="drafts-empty">No listing folders yet. Run a pipeline.</div>';
    return;
  }
  grid.innerHTML = '';
  for (const d of drafts) {
    const card = document.createElement('article');
    card.className = 'draft';

    const imgs = (d.images || []).slice(0, 8).map(name =>
      `<img src="/image/${encodeURIComponent(d.folder)}/${encodeURIComponent(name)}" loading="lazy" alt="">`
    ).join('');
    const remaining = Math.max(0, (d.image_count || 0) - 8);
    const moreCell = remaining > 0 ? `<div class="more">+${remaining}</div>` : '';

    card.innerHTML = `
      <header class="draft-head">
        <h2 class="draft-title">${esc(d.title || d.folder)}</h2>
        <div class="draft-loc">${esc(d.location || '— location unknown —')}</div>
        <div class="draft-folder">${esc(d.folder)}</div>
      </header>
      <div class="draft-grid-imgs">${imgs}${moreCell}</div>
      <dl class="draft-meta">
        ${row('qty', d.quantity)}
        ${row('type', d.chair_type)}
        ${row('dimensions', d.dimensions)}
        ${row('price', d.suggested_price ? `<span class="price">$${d.suggested_price}/ea</span>` : null, true)}
        ${row('images', d.image_count)}
      </dl>
      <div class="draft-actions">
        ${d.facebook_url
          ? `<a href="${esc(d.facebook_url)}" target="_blank" rel="noopener">↗ Facebook draft</a>`
          : '<a class="disabled" title="no FB URL — paste one on the Inventory tab or run the pipeline">↗ Facebook draft</a>'}
        ${d.ebay_url
          ? `<a href="${esc(d.ebay_url)}" target="_blank" rel="noopener">↗ eBay draft</a>`
          : '<a class="disabled" title="no eBay URL — paste one on the Inventory tab or run the pipeline">↗ eBay draft</a>'}
      </div>
    `;
    grid.appendChild(card);
  }
}

function row(label, val, html = false) {
  if (val == null || val === '') return '';
  const cell = html ? val : esc(String(val));
  return `<dt>${label}</dt><dd>${cell}</dd>`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// ───────── auctions ─────────

const auc = {
  source: 'gd',
  category: '',           // '' | 'banquet' | 'medical' (on-read keyword classifier)
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

const SOURCE_NAMES = { gd: 'GovDeals', ps: 'Public Surplus', bs: 'BidSpotter' };

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

$$('#auc-source .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#auc-source .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    auc.source = btn.dataset.value;
    loadAuctions();
  });
});

// Category sub-tab — medical defaults min-qty to 1 (singles), banquet to 50.
$$('#auc-category .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#auc-category .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    auc.category = btn.dataset.value;
    const minQty = $('#auc-min-qty');
    if (minQty) {
      const desired = auc.category === 'medical' ? '1' : '50';
      minQty.value = desired;
      const out = $('#auc-min-qty-out');
      if (out) out.textContent = desired;
    }
    loadAuctions();
  });
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

// ── scrape dropdown ──

const dd = $('#scrape-dropdown');
const ddMenu = $('.dropdown-menu', dd);
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
      body: JSON.stringify({source, test}),
    });
  } catch (err) {
    setScrapeStrip({status: 'error', source, last_line: err.message || String(err)});
    toast(`Scrape failed to start: ${err.message || err}`, 'err');
  }
}

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

function _ageInDays(isoStr) {
  if (!isoStr) return null;
  const ms = Date.now() - new Date(isoStr).getTime();
  if (Number.isNaN(ms)) return null;
  return ms / 86400000;
}

function _fmtAge(days) {
  if (days == null) return 'never';
  if (days < 1/24) return 'just now';
  if (days < 1) return `${Math.round(days * 24)}h ago`;
  const d = Math.floor(days);
  return `${d} day${d === 1 ? '' : 's'} ago`;
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
  if (auc.category) qs.set('category', auc.category);
  try {
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
      reasons.push(`<span class="fs-bad">min-chairs</span> set to ${$('#auc-min-qty').value}`);
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
    summary.innerHTML = `Showing ${shownCount} of ${srcCount.toLocaleString()} ${srcKey} lots (ranked by chair count).`;
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
$('#auc-map-toggle').addEventListener('click', () => {
  const on = !auc.mapOn;
  try { localStorage.setItem('admin.aucMapOn', on ? 'on' : 'off'); } catch (_) {}
  setAucMapOn(on);
});

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

function _fmtRemaining(secs) {
  if (secs == null) return 'no end date';
  if (secs <= 0) return 'ended';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return `${h}h ${m}m`;
  }
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  return `${d}d ${h}h`;
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

// Periodically refresh the strip's countdown numbers without re-fetching the
// (slow) auctions LLM call. Cheap GET; the dots reflect server-side sent state.
setInterval(() => {
  if ($('#auction-grid')) loadFavorites();
}, 30000);


async function queueRuns(urls) {
  await apiFetch('/api/runs/queue', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({urls}),
  });
  // Refresh the launcher state so the queue strip updates immediately.
  fetch('/api/runs/state').then(r => r.json()).then(applyState).catch(() => {});
}


// ─────────────────────────── Inventory tab ───────────────────────────

var _invItems = [];
var _invStatusFilter = '';

async function loadInventory() {
  const tbody = $('#inv-tbody');
  tbody.innerHTML = '<tr><td colspan="11" class="drafts-empty">Loading…</td></tr>';
  try {
    const qs = new URLSearchParams({with_stats: '1'});
    if (_invStatusFilter) qs.set('status', _invStatusFilter);
    const res = await fetch('/api/inventory?' + qs.toString());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _invItems = data.items || [];
    renderInvStats(data.stats || {lots: 0, chairs: 0, cities: 0});
    renderInvTable(_invItems);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="11" class="drafts-empty">Load failed: ${e}</td></tr>`;
  }
}

function renderInvStats(stats) {
  const el = $('#inv-stats');
  el.innerHTML = `
    <div class="strip-cell"><div class="strip-num">${stats.lots}</div><div class="strip-lab">ACTIVE LOTS</div></div>
    <div class="strip-cell"><div class="strip-num">${stats.chairs.toLocaleString()}</div><div class="strip-lab">CHAIRS LEFT</div></div>
    <div class="strip-cell"><div class="strip-num">${stats.cities}</div><div class="strip-lab">CITIES</div></div>
  `;
}

function renderInvTable(items) {
  const tbody = $('#inv-tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="drafts-empty">No rows. Click ↓ backfill to import folder listings.</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  for (const item of items) {
    const tr = document.createElement('tr');
    tr.dataset.lotId = item.lot_id;
    tr.innerHTML = `
      <td class="mono tiny">${escapeHtml(item.lot_id)}</td>
      <td class="inv-hero">${item.hero_image_url
        ? `<img src="${item.hero_image_url}" alt="" loading="lazy" decoding="async" width="72" height="54">`
        : '<div class="inv-hero-fallback">◉</div>'}</td>
      <td>
        <div class="inv-title">${escapeHtml(item.title || '—')}</div>
        <div class="inv-sub mono tiny">${escapeHtml((item.city || '') + (item.state ? ', ' + item.state : '') + (item.zip_code ? ' ' + item.zip_code : ''))}${item.chair_type ? ' · ' + escapeHtml(item.chair_type) : ''}</div>
        ${(item.contact_email || item.contact_phone || item.contact_name) ? `
          <div class="inv-sub mono tiny inv-contact">☎ ${escapeHtml([item.contact_name, item.contact_phone, item.contact_email].filter(Boolean).join(' · '))}</div>
        ` : ''}
        <div class="inv-sub inv-locs">
          <input class="inv-loc-input mono tiny" data-field="locations_text"
                 value="${escapeAttr(item.locations_text || '')}"
                 placeholder="extra locations — Baltimore, MD x1200; Orlando, FL"
                 title="Every place this lot sits. Blank = single location (uses the city above).">
        </div>
        <div class="inv-sub inv-extras">
          <button class="btn btn-small btn-ghost inv-acct" data-act="acct" title="Set the GovDeals login that owns this lot">
            🔐 ${item.govdeals_username
                  ? escapeHtml(item.govdeals_username) + (item.govdeals_password_set ? ' ✓' : ' (no pw)')
                  : 'set acct'}
          </button>
          ${item.buyer_cert_url
            ? `<a class="btn btn-small btn-ghost inv-cert-link" href="${escapeAttr(item.buyer_cert_url)}" target="_blank" rel="noopener" title="Open buyer certificate">📎 ${escapeHtml(item.buyer_cert_filename || 'cert')}</a>
               <button class="btn btn-small btn-ghost inv-danger" data-act="cert-clear" title="Remove certificate">✕</button>`
            : `<button class="btn btn-small btn-ghost" data-act="cert-attach" title="Attach winning-bid buyer certificate">📎 attach cert</button>`}
        </div>
      </td>
      <td>
        <input type="number" class="inv-qty" value="${item.quantity_remaining ?? ''}" min="0" data-field="quantity_remaining">
        <div class="inv-sub mono tiny">of <input type="number" class="inv-qty-orig" value="${item.quantity_original ?? ''}" min="0" data-field="quantity_original"></div>
      </td>
      <td>
        <input type="number" class="inv-price" value="${item.price_per_chair ?? ''}" min="0" step="1" data-field="price_per_chair">
      </td>
      <td>
        <select class="inv-status" data-field="status">
          ${['draft','listed','hidden','sold_out','lost_sold_out','owned','won_pickup','active_bid','lost'].map(s =>
            `<option value="${s}" ${s===item.status?'selected':''}>${s}</option>`).join('')}
        </select>
      </td>
      <td>${renderPlatformCell(item, 'facebook')}</td>
      <td>${renderPlatformCell(item, 'ebay')}</td>
      <td>${renderPlatformCell(item, 'fb_business')}</td>
      <td>${renderPlatformCell(item, 'ad')}</td>
      <td class="inv-actions">
        <button class="btn btn-small btn-ghost" data-act="view">view</button>
        <button class="btn btn-small btn-ghost" data-act="republish">republish</button>
        <button class="btn btn-small btn-ghost" data-act="remove-everywhere"
                title="Mark as moved: fake sold-out on the site + business feed, Mark as sold on Marketplace">moved</button>
        <button class="btn btn-small btn-ghost inv-danger" data-act="delete">✕</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  // Wire inline edits
  tbody.querySelectorAll('input[data-field], select[data-field]').forEach(el => {
    el.addEventListener('change', onInvFieldChange);
  });
  tbody.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', onInvAction);
  });
  tbody.querySelectorAll('.plat-set').forEach(btn => btn.addEventListener('click', onPlatformSet));
  tbody.querySelectorAll('.plat-clear').forEach(btn => btn.addEventListener('click', onPlatformClear));
}

function renderPlatformCell(item, platform) {
  const url = item[platform + '_url'];
  const ts = item[platform + '_published_at'];
  if (url) {
    return `
      <div class="plat-ok">
        <a href="${escapeAttr(url)}" target="_blank" rel="noopener">✓ link</a>
        <div class="mono tiny">${ts ? ts.slice(0,10) : ''}</div>
        <button class="btn btn-small plat-clear" data-platform="${platform}" title="Clear URL">✕</button>
      </div>`;
  }
  return `<button class="btn btn-small plat-set" data-platform="${platform}">paste URL</button>`;
}

async function onInvFieldChange(e) {
  const tr = e.target.closest('tr');
  const lotId = tr.dataset.lotId;
  const field = e.target.dataset.field;
  let value = e.target.value;
  if (field === 'quantity_remaining' || field === 'quantity_original' || field === 'price_per_chair') {
    value = value === '' ? null : Number(value);
  }
  const body = {};
  body[field] = value;
  try {
    const res = await fetch(`/api/inventory/${encodeURIComponent(lotId)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const updated = await res.json();
    // If status auto-flipped (qty=0 → sold_out), sync the UI.
    const sel = tr.querySelector('select[data-field="status"]');
    if (sel) sel.value = updated.status;
    flashRow(tr, 'ok');
  } catch (err) {
    flashRow(tr, 'err');
    toast('Update failed: ' + err.message, 'err');
  }
}

async function onInvAction(e) {
  const btn = e.target;
  const tr = btn.closest('tr');
  const lotId = tr.dataset.lotId;
  const act = btn.dataset.act;
  if (act === 'view') {
    window.open(`/listings/${encodeURIComponent(lotId)}`, '_blank');
    return;
  }
  if (act === 'remove-everywhere') {
    if (!confirm(`Mark ${lotId} as MOVED everywhere?\n\nSite + business feed: fake sold-out (shows under ALREADY MOVED).\nMarketplace: Mark as sold on the family account.\n\nWatch the Launcher console.`)) return;
    await withButtonLoading(btn, '…queuing', async () => {
      try {
        await apiFetch(`/api/lots/${encodeURIComponent(lotId)}/remove`, {method: 'POST',
          headers: {'content-type': 'application/json'}, body: '{}'});
        toast(`${lotId} queued as moved — see Launcher tab.`, 'ok');
        setTimeout(loadInventory, 4000);
      } catch (err) {
        toast('Remove failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'delete') {
    if (!confirm(`Delete lot ${lotId} from the ledger? (Folder on disk is untouched.)`)) return;
    await withButtonLoading(btn, '…deleting', async () => {
      try {
        await apiFetch(`/api/inventory/${encodeURIComponent(lotId)}`, {method: 'DELETE'});
        toast(`Lot ${lotId} deleted from ledger.`, 'ok');
        loadInventory();
      } catch (err) {
        toast('Delete failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'acct') {
    const item = _invItems.find(x => x.lot_id === lotId);
    const curUser = item?.govdeals_username || '';
    const username = prompt(
      `GovDeals username/email for lot ${lotId}\n(leave blank to clear):`,
      curUser,
    );
    if (username === null) return;
    let password = null;
    if (username.trim()) {
      // Don't prefill the password — we never return it from the API. An empty
      // submit keeps the existing password; "-" explicitly clears it.
      const pwPrompt = prompt(
        `GovDeals password for ${username.trim()}\n` +
        `(empty = keep current, "-" = clear, anything else = replace):`,
        '',
      );
      if (pwPrompt === null) return;
      password = pwPrompt;
    }
    const body = {govdeals_username: username.trim() || null};
    if (!username.trim()) {
      body.govdeals_password = null;
    } else if (password === '-') {
      body.govdeals_password = null;
    } else if (password !== '' && password !== null) {
      body.govdeals_password = password;
    }
    await withButtonLoading(btn, '…saving', async () => {
      try {
        const res = await fetch(`/api/inventory/${encodeURIComponent(lotId)}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        toast(`Account saved for ${lotId}.`, 'ok');
        loadInventory();
      } catch (err) {
        toast('Save failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'cert-attach') {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf,.png,.jpg,.jpeg,.webp,image/*,application/pdf';
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      await withButtonLoading(btn, '…uploading', async () => {
        try {
          const fd = new FormData();
          fd.append('file', file);
          const res = await fetch(
            `/api/inventory/${encodeURIComponent(lotId)}/buyer-cert`,
            {method: 'POST', body: fd},
          );
          if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
          toast(`Certificate attached to ${lotId}.`, 'ok');
          loadInventory();
        } catch (err) {
          toast('Upload failed: ' + (err.message || err), 'err');
        }
      });
    });
    input.click();
    return;
  }
  if (act === 'cert-clear') {
    if (!confirm(`Remove the buyer certificate for ${lotId}?`)) return;
    await withButtonLoading(btn, '…', async () => {
      try {
        const res = await fetch(
          `/api/inventory/${encodeURIComponent(lotId)}/buyer-cert`,
          {method: 'DELETE'},
        );
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        toast(`Certificate removed for ${lotId}.`, 'ok');
        loadInventory();
      } catch (err) {
        toast('Remove failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'republish') {
    const item = _invItems.find(x => x.lot_id === lotId);
    if (!item) return;
    const url = item.govdeals_url;
    if (!url) {
      toast('This row has no GovDeals URL — republish only works for scraped lots.', 'err');
      return;
    }
    if (!confirm(`Republish ${lotId}? This ignores the dedup check and spends API tokens.`)) return;
    await withButtonLoading(btn, '…queuing', async () => {
      try {
        await apiFetch('/api/runs/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, force_republish: true}),
        });
        toast(`Lot ${lotId} queued. Switch to Launcher to watch.`, 'ok');
      } catch (err) {
        toast('Republish failed: ' + (err.message || err), 'err');
      }
    });
  }
}

const PLATFORM_LABELS = {
  facebook: 'Facebook Marketplace',
  ebay: 'eBay',
  fb_business: 'Facebook Business post',
  ad: 'Ad',
};

async function onPlatformSet(e) {
  const btn = e.target;
  const tr = btn.closest('tr');
  const lotId = tr.dataset.lotId;
  const platform = btn.dataset.platform;
  const label = PLATFORM_LABELS[platform] || platform.toUpperCase();
  const url = prompt(`Paste the ${label} URL for lot ${lotId}:`);
  if (!url) return;
  await withButtonLoading(btn, '…saving', async () => {
    try {
      await apiFetch(`/api/inventory/${encodeURIComponent(lotId)}/platform`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({platform, url: url.trim()}),
      });
      toast(`${label} URL saved for ${lotId}.`, 'ok');
      loadInventory();
    } catch (err) {
      toast(`Save failed: ${err.message || err}`, 'err');
    }
  });
}

async function onPlatformClear(e) {
  const btn = e.target;
  const tr = btn.closest('tr');
  const lotId = tr.dataset.lotId;
  const platform = btn.dataset.platform;
  const label = PLATFORM_LABELS[platform] || platform.toUpperCase();
  if (!confirm(`Clear the ${label} URL for lot ${lotId}?`)) return;
  await withButtonLoading(btn, '…', async () => {
    try {
      await apiFetch(`/api/inventory/${encodeURIComponent(lotId)}/platform`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({platform, url: null}),
      });
      toast(`${label} URL cleared for ${lotId}.`, 'ok');
      loadInventory();
    } catch (err) {
      toast(`Clear failed: ${err.message || err}`, 'err');
    }
  });
}

function flashRow(tr, kind) {
  tr.classList.remove('flash-ok', 'flash-err');
  void tr.offsetWidth;  // reflow
  tr.classList.add(kind === 'ok' ? 'flash-ok' : 'flash-err');
}

$('#inv-refresh')?.addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '↻ loading…', loadInventory);
});
$('#inv-backfill')?.addEventListener('click', async (e) => {
  if (!confirm('Walk the listings folder and import any missing rows as drafts?')) return;
  await withButtonLoading(e.currentTarget, '…backfilling', async () => {
    try {
      const data = await apiFetch('/api/inventory/backfill', {method: 'POST'});
      toast(`Backfill done · +${data.counts.added} added · ${data.counts.updated} updated · ${data.counts.skipped} skipped`, 'ok');
      loadInventory();
    } catch (err) {
      toast('Backfill failed: ' + (err.message || err), 'err');
    }
  });
});

// add-listing form: toggle + submit
$('#inv-add')?.addEventListener('click', () => {
  const f = $('#inv-add-form');
  if (!f) return;
  f.hidden = !f.hidden;
  if (!f.hidden) f.querySelector('input[name="lot_id"]')?.focus();
});
$('#inv-add-cancel')?.addEventListener('click', () => {
  const f = $('#inv-add-form');
  if (f) { f.reset(); f.hidden = true; }
});
$('#inv-add-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const fd = new FormData(f);
  const payload = {};
  fd.forEach((v, k) => { v = (v || '').toString().trim(); if (v) payload[k] = v; });
  if (!payload.lot_id || !payload.title || !payload.quantity) {
    toast('Lot ID, title, and quantity are required.', 'err');
    return;
  }
  const btn = f.querySelector('button[type="submit"]');
  await withButtonLoading(btn, '…creating', async () => {
    try {
      await apiFetch('/api/inventory', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      toast(`Listing ${payload.lot_id} created.`, 'ok');
      f.reset(); f.hidden = true;
      loadInventory();
    } catch (err) {
      toast('Create failed: ' + (err.message || err), 'err');
    }
  });
});

// status-filter segmented control
$$('#inv-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#inv-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _invStatusFilter = b.dataset.value;
  loadInventory();
}));


// ─────────────────────────── Inquiries tab ───────────────────────────

var _inqStatusFilter = '';

async function loadInquiries() {
  const el = $('#inq-list');
  el.innerHTML = '<div class="drafts-empty">Loading…</div>';
  try {
    const r = await fetch('/api/inquiries' + (_inqStatusFilter ? `?status=${_inqStatusFilter}` : ''));
    const data = await r.json();
    renderInquiries(data.items || []);
  } catch (e) {
    el.innerHTML = `<div class="drafts-empty">Load failed: ${e}</div>`;
  }
}

function renderInquiries(items) {
  const el = $('#inq-list');
  if (!items.length) {
    el.innerHTML = '<div class="drafts-empty">No inquiries yet. Share /listings with customers to collect leads.</div>';
    return;
  }
  el.innerHTML = '';
  for (const q of items) {
    const card = document.createElement('article');
    card.className = `inq-card inq-${q.status}`;
    card.dataset.id = q.id;
    const when = q.created_at ? q.created_at.replace('T', ' ').slice(0, 16) : '';
    const lotLink = q.lot_id
      ? `<a href="/listings/${encodeURIComponent(q.lot_id)}" target="_blank">LOT #${escapeHtml(q.lot_id)}</a>`
      : '<span class="inq-unlinked mono tiny">UNLINKED</span>';
    card.innerHTML = `
      <header class="inq-head">
        <span class="inq-kind inq-kind--${q.kind}">${q.kind === 'buy' ? 'BUY' : 'SELL'}</span>
        <span class="inq-lot">${lotLink}</span>
        <span class="inq-when mono tiny">${when}</span>
        <span class="inq-status-pill inq-status-${q.status}">${q.status}</span>
      </header>
      <div class="inq-body">
        <div class="inq-name">${escapeHtml(q.name)}</div>
        <div class="inq-contact mono tiny">
          ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
          ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
          ${q.quantity_interested ? `· qty ${q.quantity_interested}` : ''}
        </div>
        ${q.message ? `<blockquote class="inq-msg">${escapeHtml(q.message)}</blockquote>` : ''}
      </div>
      <footer class="inq-foot">
        ${['new','contacted','closed'].filter(s => s !== q.status)
          .map(s => `<button class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
        <button class="btn btn-small btn-ghost inv-danger" data-delete>✕ delete</button>
      </footer>
    `;
    card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', async () => {
      const status = b.dataset.setStatus;
      await withButtonLoading(b, '…', async () => {
        try {
          await apiFetch(`/api/inquiries/${q.id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status}),
          });
          toast(`Inquiry #${q.id} → ${status}`, 'ok');
          loadInquiries();
        } catch (err) {
          toast('Status change failed: ' + (err.message || err), 'err');
        }
      });
    }));
    card.querySelector('[data-delete]').addEventListener('click', async (e) => {
      if (!confirm(`Delete inquiry #${q.id}?`)) return;
      await withButtonLoading(e.currentTarget, '…', async () => {
        try {
          await apiFetch(`/api/inquiries/${q.id}`, {method: 'DELETE'});
          toast(`Inquiry #${q.id} deleted.`, 'ok');
          loadInquiries();
        } catch (err) {
          toast('Delete failed: ' + (err.message || err), 'err');
        }
      });
    });
    el.appendChild(card);
  }
}

$('#inq-refresh')?.addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '↻ loading…', loadInquiries);
});
$$('#inq-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#inq-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _inqStatusFilter = b.dataset.value;
  loadInquiries();
}));


// ─────────────────────────── Subscribers tab ───────────────────────────

var _subStatusFilter = '';

const SUB_LABELS = {
  use_case: {church: 'church', event_venue: 'event venue', wedding_rental: 'wedding rental',
             restaurant: 'restaurant', school: 'school', reseller: 'reseller', other: 'other'},
  timeline: {asap: 'ASAP', month: 'within a month', flexible: 'flexible'},
  budget_per_chair: {under_5: '<$5/chair', '5_10': '$5–10/chair', '10_20': '$10–20/chair', '20_plus': '$20+/chair'},
  delivery: {pickup: 'pickup', delivery: 'ship', either: 'either'},
};

async function loadSubscribers() {
  const el = $('#sub-list');
  el.innerHTML = '<div class="drafts-empty">Loading…</div>';
  try {
    const r = await fetch('/api/subscribers' + (_subStatusFilter ? `?status=${_subStatusFilter}` : ''));
    const data = await r.json();
    renderSubscribers(data.items || []);
  } catch (e) {
    el.innerHTML = `<div class="drafts-empty">Load failed: ${e}</div>`;
  }
}

function renderSubscribers(items) {
  const el = $('#sub-list');
  if (!items.length) {
    el.innerHTML = '<div class="drafts-empty">No alert signups yet. The form lives at /listings#alerts.</div>';
    return;
  }
  el.innerHTML = '';
  for (const q of items) {
    const card = document.createElement('article');
    card.className = `inq-card inq-${q.status}`;
    card.dataset.id = q.id;
    const when = q.created_at ? String(q.created_at).replace('T', ' ').slice(0, 16) : '';
    const geo = [q.city, q.state, q.zip_code].filter(Boolean).join(' ');
    const prefs = [
      q.quantity_wanted ? `qty ${q.quantity_wanted}` : '',
      geo,
      SUB_LABELS.use_case[q.use_case] || q.use_case || '',
      q.chair_type || '',
      SUB_LABELS.timeline[q.timeline] || q.timeline || '',
      SUB_LABELS.budget_per_chair[q.budget_per_chair] || q.budget_per_chair || '',
      SUB_LABELS.delivery[q.delivery] || q.delivery || '',
    ].filter(Boolean).join(' · ');
    card.innerHTML = `
      <header class="inq-head">
        <span class="inq-kind inq-kind--buy">ALERT</span>
        <span class="inq-lot mono tiny">${escapeHtml(q.source || '')}</span>
        <span class="inq-when mono tiny">${when}</span>
        <span class="inq-status-pill inq-status-${q.status}">${q.status}</span>
      </header>
      <div class="inq-body">
        <div class="inq-name">${escapeHtml(q.name || '—')}</div>
        <div class="inq-contact mono tiny">
          ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
          ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
        </div>
        ${prefs ? `<div class="inq-contact mono tiny">${escapeHtml(prefs)}</div>` : ''}
        ${q.notes ? `<blockquote class="inq-msg">${escapeHtml(q.notes)}</blockquote>` : ''}
      </div>
      <footer class="inq-foot">
        ${['new','contacted','matched','unsubscribed'].filter(s => s !== q.status)
          .map(s => `<button class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
        <button class="btn btn-small btn-ghost inv-danger" data-delete>✕ delete</button>
      </footer>
    `;
    card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', async () => {
      const status = b.dataset.setStatus;
      await withButtonLoading(b, '…', async () => {
        try {
          await apiFetch(`/api/subscribers/${q.id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status}),
          });
          toast(`Subscriber #${q.id} → ${status}`, 'ok');
          loadSubscribers();
        } catch (err) {
          toast('Status change failed: ' + (err.message || err), 'err');
        }
      });
    }));
    card.querySelector('[data-delete]').addEventListener('click', async (e) => {
      if (!confirm(`Delete subscriber #${q.id}?`)) return;
      await withButtonLoading(e.currentTarget, '…', async () => {
        try {
          await apiFetch(`/api/subscribers/${q.id}`, {method: 'DELETE'});
          toast(`Subscriber #${q.id} deleted.`, 'ok');
          loadSubscribers();
        } catch (err) {
          toast('Delete failed: ' + (err.message || err), 'err');
        }
      });
    });
    el.appendChild(card);
  }
}

$('#sub-refresh')?.addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '↻ loading…', loadSubscribers);
});
$$('#sub-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#sub-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _subStatusFilter = b.dataset.value;
  loadSubscribers();
}));


// ─────────────────────────── Listings DB tab ───────────────────────────

const _ldb = {
  source: 'all',
  status: 'all',
  offset: 0,
  total: 0,
  limit: 50,
};

function _ldbQuery() {
  return new URLSearchParams({
    source: _ldb.source,
    status: _ldb.status,
    q: $('#ldb-q').value.trim(),
    min_qty: $('#ldb-min-qty').value || '0',
    max_qty: $('#ldb-max-qty').value || '99999',
    seen_within_days: $('#ldb-seen').value,
    sort: $('#ldb-sort').value,
    limit: $('#ldb-limit').value,
    offset: String(_ldb.offset),
  });
}

async function loadListingsDb() {
  const tbody = $('#ldb-tbody');
  const statusBar = $('#ldb-status-bar');
  const pager = $('#ldb-pager');
  _ldb.limit = Number($('#ldb-limit').value) || 50;
  tbody.innerHTML = '<tr><td colspan="8" class="drafts-empty loading"><span class="spinner"></span> querying listings.db…</td></tr>';
  statusBar.innerHTML = '<span class="pulse">●</span> loading…';
  try {
    const data = await apiFetch('/api/listings?' + _ldbQuery().toString());
    _ldb.total = data.total;
    renderListingsDb(data.items);
    const shownFrom = data.total === 0 ? 0 : _ldb.offset + 1;
    const shownTo = Math.min(_ldb.offset + data.items.length, data.total);
    statusBar.textContent = data.total === 0
      ? 'No rows match these filters.'
      : `Showing ${shownFrom}–${shownTo} of ${data.total.toLocaleString()} rows`;
    pager.hidden = data.total <= _ldb.limit;
    $('#ldb-page-info').textContent = `page ${Math.floor(_ldb.offset / _ldb.limit) + 1} of ${Math.max(1, Math.ceil(data.total / _ldb.limit))}`;
    $('#ldb-prev').disabled = _ldb.offset === 0;
    $('#ldb-next').disabled = _ldb.offset + _ldb.limit >= data.total;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="drafts-empty">Query failed: ${escapeHtml(err.message || String(err))}</td></tr>`;
    statusBar.textContent = '';
    toast('Listings DB query failed: ' + (err.message || err), 'err');
  }
}

function _fmtEndDate(row) {
  // GovDeals rows populate end_date; Public Surplus populates time_left only.
  if (row.end_date) return row.end_date;
  if (row.time_left) return row.time_left;
  return '—';
}

function _fmtLastSeen(iso) {
  if (!iso) return '—';
  const ageD = _ageInDays(iso);
  if (ageD == null) return iso.slice(0, 10);
  return _fmtAge(ageD);
}

function renderListingsDb(items) {
  const tbody = $('#ldb-tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="drafts-empty">No rows match these filters.</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  for (const r of items) {
    const tr = document.createElement('tr');
    tr.className = 'ldb-row';
    const srcClass = ['gd', 'ps', 'bs'].includes(r.source) ? `src-${r.source}` : 'src-other';
    const qty = r.quantity == null ? '—' : r.quantity.toLocaleString();
    const price = r.price || '—';
    const loc = r.location || '—';
    const title = r.title || '(untitled)';
    const endStr = _fmtEndDate(r);
    const isExpired = r.end_date && new Date(r.end_date) < new Date();
    tr.innerHTML = `
      <td><span class="src-pill ${srcClass}">${r.source.toUpperCase()}</span></td>
      <td class="ldb-qty">${qty}</td>
      <td class="ldb-title">
        <div class="ldb-title-main">${escapeHtml(title)}</div>
        <div class="ldb-asset mono tiny">${escapeHtml(r.asset_id)}${r.quantity_source ? ` · qty via <em>${escapeHtml(r.quantity_source)}</em>` : ''}${r.quantity_confidence ? ` <span class="ldb-conf">${escapeHtml(r.quantity_confidence)}</span>` : ''}</div>
      </td>
      <td class="ldb-price">${escapeHtml(price)}</td>
      <td class="ldb-loc">${escapeHtml(loc)}</td>
      <td class="ldb-end ${isExpired ? 'expired' : ''}">${escapeHtml(endStr)}</td>
      <td class="ldb-seen">${escapeHtml(_fmtLastSeen(r.last_seen_at))}</td>
      <td class="ldb-act">
        <a href="${escapeAttr(r.link || '#')}" target="_blank" rel="noopener" class="btn btn-small" title="Open source listing">↗</a>
        ${r.source === 'gd' ? `<button type="button" class="btn btn-small btn-primary ldb-launch" data-url="${escapeAttr(r.link)}" title="Queue this lot for the pipeline">▶</button>` : ''}
      </td>
    `;
    const launchBtn = tr.querySelector('.ldb-launch');
    if (launchBtn) {
      launchBtn.addEventListener('click', async () => {
        await withButtonLoading(launchBtn, '⏱', async () => {
          try {
            await queueRuns([launchBtn.dataset.url]);
            launchBtn.textContent = '✓';
            launchBtn.disabled = true;
            launchBtn.classList.add('queued');
            toast(`Queued: ${title}`, 'ok');
          } catch (err) {
            toast('Queue failed: ' + (err.message || err), 'err');
          }
        });
      });
    }
    tbody.appendChild(tr);
  }
}

// Filter wiring — any change resets offset to 0 and re-queries.
function _ldbReload() { _ldb.offset = 0; loadListingsDb(); }

$$('#ldb-source .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#ldb-source .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _ldb.source = b.dataset.value;
  _ldbReload();
}));

$$('#ldb-status .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#ldb-status .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _ldb.status = b.dataset.value;
  _ldbReload();
}));

// Debounced search
let _ldbSearchTimer;
$('#ldb-q')?.addEventListener('input', () => {
  clearTimeout(_ldbSearchTimer);
  _ldbSearchTimer = setTimeout(_ldbReload, 350);
});

['#ldb-min-qty', '#ldb-max-qty', '#ldb-seen', '#ldb-sort', '#ldb-limit']
  .forEach(sel => $(sel)?.addEventListener('change', _ldbReload));

$('#ldb-refresh')?.addEventListener('click', (e) => {
  withButtonLoading(e.currentTarget, '↻ loading…', loadListingsDb);
});

$('#ldb-reset')?.addEventListener('click', () => {
  $$('#ldb-source .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.value === 'all'));
  $$('#ldb-status .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.value === 'all'));
  _ldb.source = 'all'; _ldb.status = 'all'; _ldb.offset = 0;
  $('#ldb-q').value = '';
  $('#ldb-min-qty').value = '0';
  $('#ldb-max-qty').value = '99999';
  $('#ldb-seen').value = '7';
  $('#ldb-sort').value = 'qty_desc';
  $('#ldb-limit').value = '50';
  loadListingsDb();
});

$('#ldb-prev')?.addEventListener('click', () => {
  _ldb.offset = Math.max(0, _ldb.offset - _ldb.limit);
  loadListingsDb();
});
$('#ldb-next')?.addEventListener('click', () => {
  if (_ldb.offset + _ldb.limit < _ldb.total) {
    _ldb.offset += _ldb.limit;
    loadListingsDb();
  }
});


// ─────────────────────────── helpers ───────────────────────────

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}
function escapeAttr(s) { return escapeHtml(s); }


// ─────────────────────────── test scrape (08) ───────────────────────────
// Live keyword probe against /api/test-scrape. Read-only — exists so a new
// category ("desks", "lockers") can be eyeballed for relevance + images
// before its search term is committed to the scrapers' SEARCH_TERMS.

const _ts = {source: 'both'};

$$('#ts-source .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#ts-source .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _ts.source = btn.dataset.value;
  });
});

$('#ts-run')?.addEventListener('click', runTestScrape);
$('#ts-q')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); runTestScrape(); }
});

async function runTestScrape() {
  const q = ($('#ts-q').value || '').trim();
  if (!q) { toast('Enter a keyword to test', 'err'); $('#ts-q').focus(); return; }
  const pages = Math.max(1, Math.min(Number($('#ts-pages').value) || 1, 5));
  const sources = _ts.source === 'both' ? ['gd', 'ps', 'bs'] : [_ts.source];

  const grid = $('#ts-grid');
  const status = $('#ts-status');
  const btn = $('#ts-run');
  grid.innerHTML = `<div class="drafts-empty loading"><span class="spinner"></span> live-searching ${sources.join(' + ')} for “${escapeHtml(q)}”…</div>`;
  status.textContent = '';
  btn.disabled = true;
  btn.classList.add('is-loading');
  try {
    const results = await Promise.allSettled(sources.map(s =>
      apiFetch(`/api/test-scrape?source=${s}&q=${encodeURIComponent(q)}&pages=${pages}`)));
    const items = [];
    const errs = [];
    const perSource = [];
    results.forEach((r, i) => {
      const name = SOURCE_NAMES[sources[i]] || sources[i];
      if (r.status === 'fulfilled') {
        perSource.push(`${name}: ${r.value.count}`);
        items.push(...r.value.items.map(it => ({...it, _source: sources[i]})));
      } else {
        errs.push(`${name}: ${r.reason?.message || r.reason}`);
      }
    });
    renderTestScrape(items, q, perSource, errs);
  } finally {
    btn.disabled = false;
    btn.classList.remove('is-loading');
  }
}

function renderTestScrape(items, q, perSource, errs) {
  const grid = $('#ts-grid');
  const status = $('#ts-status');
  for (const e of errs) toast(`Test scrape failed — ${e}`, 'err');

  if (!items.length) {
    grid.innerHTML = `<div class="drafts-empty">0 listings for “${escapeHtml(q)}”${errs.length ? ' (a source errored — see toast)' : ''}.</div>`;
    status.textContent = '';
    return;
  }

  // Keyword-in-title check: crude singular stem, same idea as the API's
  // _singularize_term. Flags off-keyword cards rather than hiding them —
  // the noise level IS the signal this tab exists to measure.
  const stem = q.toLowerCase().replace(/s$/, '');
  let offKeyword = 0;
  grid.innerHTML = '';
  for (const it of items) {
    const match = (it.title || '').toLowerCase().includes(stem);
    if (!match) offKeyword++;
    grid.appendChild(renderTestScrapeCard(it, match));
  }
  const bits = [`${items.length} listings (${perSource.join(' · ')})`,
                `${items.length - offKeyword} title-match “${q}”`];
  if (offKeyword) bits.push(`${offKeyword} ⚠ off-keyword`);
  status.textContent = bits.join(' · ');
}

function renderTestScrapeCard(it, match) {
  const card = document.createElement('article');
  card.className = 'auction-card' + (match ? '' : ' ts-miss');

  const img = it.image_url
    ? `<img src="${esc(it.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'auction-img-fallback',textContent:'📦'}))">`
    : `<div class="auction-img-fallback">📦</div>`;

  const ends = it.end_date || it.time_left || '';
  const srcName = SOURCE_NAMES[it._source] || it._source;

  card.innerHTML = `
    <div class="auction-img">${img}</div>
    <div class="auction-body">
      <h3 class="auction-title">${esc(it.title || '—')}</h3>
      <div class="auction-meta">
        <span class="ts-source-pill" data-source="${esc(it._source)}">${srcName}</span>
        ${it.quantity > 1 ? `<span class="auction-qty" title="Title-regex guess — the LLM does not run here">${it.quantity.toLocaleString()} ×</span>` : ''}
        ${it.price ? `<span class="auction-price">${esc(it.price)}</span>` : ''}
        ${match ? '' : '<span class="ts-miss-pill" title="Keyword not found in the title — likely an off-category match">⚠ off-keyword</span>'}
      </div>
      ${it.location ? `<div class="auction-loc">📍 ${esc(it.location)}</div>` : ''}
      ${ends ? `<div class="auction-ends">⏱ ${esc(ends)}</div>` : ''}
      <div class="auction-actions">
        <a href="${esc(it.link)}" target="_blank" rel="noopener" class="auction-link">↗ source</a>
      </div>
    </div>
  `;
  return card;
}

/* ── Deals tab ─────────────────────────────────────────────── */
const deal = {q: '', category: '', native: '', state: '', maxBids: '', ending: '',
              status: 'active', sort: 'ends', dir: null, offset: 0, limit: 50,
              minMargin: '', maxDist: '', minPrice: '', maxPrice: '', listId: '', tag: '',
              facetsLoaded: false, treeStatus: null, expanded: new Set(),
              // Deal-browser chrome (lists / tags / saved searches).
              // rows: last page fetched (drawer lookup). memb/tags: client-side
              // membership knowledge, seeded by list/tag-filtered fetches and
              // kept current by the user's own PUT/DELETEs — the /api/deals
              // rows themselves don't carry per-lot membership in v1.
              rows: [], lists: [], tagList: [], searches: [],
              metaLoaded: false, memb: new Map(), lotTags: new Map(),
              // 🗺 map view: bbox = "s,w,n,e" viewport filter pushed into SQL,
              // map = AdminMap handle, geoQS = last /api/deals/geo query string.
              mapOn: false, map: null, bbox: null, geoQS: null};

const dealKey = (r) => `${r.asset_id}/${r.account_id}/${r.auction_id}`;
const dealMemb = (key) => deal.memb.get(key) || (deal.memb.set(key, new Set()), deal.memb.get(key));
const dealLotTags = (key) => deal.lotTags.get(key) || (deal.lotTags.set(key, new Set()), deal.lotTags.get(key));

function dealEndsCell(iso) {
  if (!iso) return '<td>—</td>';
  const ms = new Date(iso) - Date.now();
  const h = ms / 3.6e6;
  const cls = h < 2 ? 'deal-ends-red' : (h < 24 ? 'deal-ends-yellow' : '');
  const label = ms <= 0 ? 'ended'
    : h < 1 ? `${Math.round(ms / 6e4)}m`
    : h < 48 ? `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`
    : `${Math.floor(h / 24)}d ${Math.floor(h % 24)}h`;
  return `<td class="${cls}" title="${iso}">${label}</td>`;
}

/* Verdict columns: est. resale / margin % (loudest cell) / conf / comps / rank.
   Un-analyzed lots render em-dashes. */
function dealVerdictCells(r, key) {
  const v = r.verdict;
  if (!v) return '<td>—</td><td class="deal-margin">—</td><td>—</td><td>—</td><td>—</td>';
  const m = v.margin_pct;
  const mCls = m == null ? '' : m >= 100 ? 'm-hot' : m >= 25 ? 'm-good' : m >= 0 ? 'm-flat' : 'm-neg';
  const margin = m == null ? '—' : `${m > 0 ? '+' : ''}${Math.round(m)}%`;
  const conf = `<span class="deal-conf deal-conf-${v.confidence}" title="method: ${v.method}">` +
               `${v.confidence}${v.method !== 'comps' ? ' · est' : ''}</span>`;
  const comps = v.comp_count > 0
    ? `<a href="#" class="deal-comps-link" data-key="${key}" title="open comp detail">${v.comp_count}</a>`
    : '0';
  return `<td>${v.est_resale != null ? '$' + Math.round(v.est_resale) : '—'}</td>
    <td class="deal-margin ${mCls}">${margin}</td>
    <td>${conf}</td>
    <td>${comps}</td>
    <td>${v.rank_score != null ? Math.round(v.rank_score) : '—'}</td>`;
}

/* Category tree: branch = canonical bucket, twig = native GovDeals category.
   Clicking a node filters the table; the arrow only expands/collapses. */
let _dealTree = null;

function renderDealTree() {
  const host = $('#deal-tree-nodes');
  if (!_dealTree) { host.innerHTML = '<div class="drafts-empty">no data</div>'; return; }
  const esc = (t) => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
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
  host.innerHTML = html;
}

async function loadDealTree() {
  deal.treeStatus = deal.status;
  try {
    const r = await fetch('/api/deals/tree?status=' + deal.status);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _dealTree = await r.json();
  } catch (e) {
    _dealTree = null;
    $('#deal-tree-nodes').innerHTML = `<div class="drafts-empty">tree error: ${e}</div>`;
    return;
  }
  renderDealTree();
}

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

async function loadDeals() {
  const tbody = $('#deal-rows');
  if (deal.treeStatus !== deal.status) loadDealTree();
  if (!deal.metaLoaded) loadDealMeta();
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
  if (deal.mapOn && deal.bbox) p.set('bbox', deal.bbox);
  p.set('status', deal.status);
  p.set('sort', deal.sort);
  if (deal.dir) p.set('dir', deal.dir);
  p.set('limit', deal.limit);
  p.set('offset', deal.offset);
  let body;
  try {
    const r = await fetch('/api/deals?' + p.toString());
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    body = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="18" class="drafts-empty">deals API error: ${e}</td></tr>`;
    return;
  }
  deal.rows = body.rows;
  // A list/tag-filtered result set is definitive membership knowledge.
  if (deal.listId) body.rows.forEach(r => dealMemb(dealKey(r)).add(Number(deal.listId)));
  if (deal.tag) body.rows.forEach(r => dealLotTags(dealKey(r)).add(deal.tag));
  const s = body.stats || {};
  $('#deal-stats').textContent =
    `${s.total_lots ?? '?'} lots tracked · ${s.candidates ?? '?'} candidates (0-bid <24h) · ${s.ending_24h ?? '?'} ending <24h`;
  if (!deal.facetsLoaded && body.facets) {
    const fill = (sel, items) => {
      const el = $(sel);
      items.forEach(f => {
        const o = document.createElement('option');
        o.value = f.value; o.textContent = `${f.value} (${f.count})`;
        el.appendChild(o);
      });
    };
    fill('#deal-category', body.facets.categories || []);
    fill('#deal-state', body.facets.states || []);
    deal.facetsLoaded = true;
    deal._catFacets = body.facets.categories || [];
    renderDealCatPills();
  }
  const esc = (t) => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  if (!body.rows.length) {
    tbody.innerHTML = '<tr><td colspan="18" class="drafts-empty">no lots match</td></tr>';
  } else {
    tbody.innerHTML = body.rows.map(r => {
      const key = dealKey(r);
      const img = r.archived_hero_url || r.hero_image_url;
      const thumb = img
        ? `<img class="deal-thumb" src="${esc(img)}" loading="lazy" alt="">`
        : '<span class="deal-thumb deal-thumb-empty">🪑</span>';
      const outcome = r.outcome_complete
        ? `<span class="lex ${r.outcome === 'no_bid' ? 'pending' : 'done'}">${esc(r.outcome) || 'closed'}${r.final_bid != null ? ` $${r.final_bid}` : ''}</span>`
        : '<span class="lex running">open</span>';
      const saved = deal.memb.get(key)?.size > 0;
      const chips = [...(deal.lotTags.get(key) || [])].map(t =>
        `<span class="deal-chip">${esc(t)}<button type="button" class="deal-chip-x" data-key="${key}" data-tag="${esc(t)}" title="remove tag">×</button></span>`
      ).join('');
      return `<tr>
        <td>${thumb}</td>
        <td><button type="button" class="deal-heart ${saved ? 'on' : ''}" data-key="${key}" title="save to a list">♥</button></td>
        <td><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
            <a class="deal-viewer-link" href="${esc(r.viewer_url)}" target="_blank" rel="noopener" title="archived copy">⧉</a>
            <span class="deal-tags">${chips}<button type="button" class="deal-tag-add" data-key="${key}" title="add tag">+</button></span></td>
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
  renderDealPager(body.total);
  if (deal.mapOn) refreshDealMapPoints(body.total);
  syncDealCatPills();
  renderDealActiveChips();
}

/* one-click canonical-category pill row (top 8 by count, from facets) */
function renderDealCatPills() {
  const host = $('#deal-cat-pills');
  if (!host) return;
  const cats = (deal._catFacets || []).slice(0, 8);
  if (!cats.length) { host.hidden = true; host.innerHTML = ''; return; }
  const total = (deal._catFacets || []).reduce((a, f) => a + Number(f.count || 0), 0);
  host.innerHTML =
    `<span class="cat-pill" data-cat="">all <span class="cp-n">${total}</span></span>` +
    cats.map(f =>
      `<span class="cat-pill" data-cat="${_dealEsc(f.value)}">${_dealEsc(f.value)} <span class="cp-n">${f.count}</span></span>`
    ).join('');
  host.hidden = false;
  syncDealCatPills();
}

function syncDealCatPills() {
  $$('#deal-cat-pills .cat-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.cat === deal.category));
}

$('#deal-cat-pills').addEventListener('click', (e) => {
  const pill = e.target.closest('.cat-pill');
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
  host.hidden = !chips.length;
  host.innerHTML = chips.map(c =>
    `<span class="deal-chip deal-af-chip">${_dealEsc(c.label)}<button type="button" class="deal-chip-x" data-k="${c.k}" title="clear filter">×</button></span>`
  ).join('') + (chips.length ? '<a href="#" id="deal-clear-filters">clear all</a>' : '');
}

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
      $$('#deal-bids-filter .seg-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.value === ''));
      break;
    case 'ending': deal.ending = ''; $('#deal-ending').value = ''; break;
    case 'margin': deal.minMargin = ''; $('#deal-min-margin').value = ''; break;
    case 'price':
      deal.minPrice = ''; deal.maxPrice = '';
      $('#deal-min-price').value = ''; $('#deal-max-price').value = ''; break;
    case 'dist': deal.maxDist = ''; $('#deal-max-dist').value = ''; break;
    case 'list': deal.listId = ''; $('#deal-list').value = ''; break;
    case 'tag': deal.tag = ''; $('#deal-tag').value = ''; break;
  }
}

$('#deal-active-chips').addEventListener('click', (e) => {
  const x = e.target.closest('.deal-chip-x');
  if (x) {
    clearDealFilter(x.dataset.k);
    deal.offset = 0;
    loadDeals();
    return;
  }
  if (e.target.closest('#deal-clear-filters')) {
    e.preventDefault();
    ['q', 'category', 'state', 'bids', 'ending', 'margin', 'price', 'dist', 'list', 'tag']
      .forEach(clearDealFilter);
    deal.offset = 0;
    loadDeals();
  }
});

let _dealQTimer;
$('#deal-q').addEventListener('input', (e) => {
  clearTimeout(_dealQTimer);
  _dealQTimer = setTimeout(() => { deal.q = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 300);
});
$('#deal-category').addEventListener('change', (e) => {
  deal.category = e.target.value; deal.native = ''; deal.offset = 0;
  renderDealTree(); loadDeals();
});
$('#deal-state').addEventListener('change', (e) => { deal.state = e.target.value; deal.offset = 0; loadDeals(); });
$('#deal-ending').addEventListener('change', (e) => { deal.ending = e.target.value; deal.offset = 0; loadDeals(); });
$$('#deal-bids-filter .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#deal-bids-filter .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    deal.maxBids = btn.dataset.value; deal.offset = 0; loadDeals();
  });
});
$$('#deal-status-filter .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#deal-status-filter .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
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
const DEAL_PAGE_SIZES = [25, 50, 100, 200];
function renderDealPager(total) {
  const page = Math.floor(deal.offset / deal.limit) + 1;
  const pages = Math.max(1, Math.ceil(total / deal.limit));
  const scope = deal.mapOn && deal.bbox ? ' · in map view' : '';
  const html = `
    <span class="deal-pager-total">page ${page} / ${pages} · ${total.toLocaleString()} lots${scope}</span>
    <button type="button" class="btn btn-small" data-page="1" ${page <= 1 ? 'disabled' : ''}>«</button>
    <button type="button" class="btn btn-small" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>‹ prev</button>
    <button type="button" class="btn btn-small" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>next ›</button>
    <button type="button" class="btn btn-small" data-page="${pages}" ${page >= pages ? 'disabled' : ''}>»</button>
    <input type="number" class="deal-num" min="1" max="${pages}" value="${page}" data-jump title="jump to page">
    <select data-limit>${DEAL_PAGE_SIZES.map(n => `<option value="${n}" ${n === deal.limit ? 'selected' : ''}>${n}/page</option>`).join('')}</select>`;
  $('#deal-pager-top').innerHTML = html;
  $('#deal-pager-bottom').innerHTML = html;
}
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
$('#deal-refresh').addEventListener('click', () => {
  deal.facetsLoaded = false; deal.metaLoaded = false;
  loadDealTree(); loadDeals();
});
let _dealMarginTimer;
$('#deal-min-margin').addEventListener('input', (e) => {
  clearTimeout(_dealMarginTimer);
  _dealMarginTimer = setTimeout(() => { deal.minMargin = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
});
let _dealDistTimer;
$('#deal-max-dist').addEventListener('input', (e) => {
  clearTimeout(_dealDistTimer);
  _dealDistTimer = setTimeout(() => { deal.maxDist = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
});
let _dealMinPriceTimer;
$('#deal-min-price').addEventListener('input', (e) => {
  clearTimeout(_dealMinPriceTimer);
  _dealMinPriceTimer = setTimeout(() => { deal.minPrice = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
});
let _dealMaxPriceTimer;
$('#deal-max-price').addEventListener('input', (e) => {
  clearTimeout(_dealMaxPriceTimer);
  _dealMaxPriceTimer = setTimeout(() => { deal.maxPrice = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 400);
});
$('#deal-list').addEventListener('change', (e) => { deal.listId = e.target.value; deal.offset = 0; loadDeals(); });
$('#deal-tag').addEventListener('change', (e) => { deal.tag = e.target.value; deal.offset = 0; loadDeals(); });

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
// unchanged; loadDeals calls this on every map-on load.
async function refreshDealMapPoints(tableTotal) {
  if (!deal.map) return;
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
    updateDealMapNote(tableTotal);
  } catch (e) {
    toast('Deals map load failed: ' + (e.message || e), 'err');
  }
}

let _dealMapMove;
async function setDealMapOn(on) {
  const btn = $('#deal-map-toggle');
  const wrap = $('#deal-map-wrap');
  deal.mapOn = on;
  btn.classList.toggle('btn-primary', deal.mapOn);
  wrap.hidden = !deal.mapOn;
  if (!deal.mapOn) {
    deal.bbox = null; deal.offset = 0;
    loadDeals();
    return;
  }
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
      deal.mapOn = false; wrap.hidden = true; btn.classList.remove('btn-primary');
      toast('Map failed to load: ' + (e.message || e), 'err');
      return;
    }
  }
  deal.map.invalidateSize();
  await refreshDealMapPoints(null);
  deal.map.fit();  // fit fires moveend → bbox lands → table follows
}
$('#deal-map-toggle').addEventListener('click', () => {
  const on = !deal.mapOn;
  try { localStorage.setItem('admin.dealMapOn', on ? 'on' : 'off'); } catch (_) {}
  setDealMapOn(on);
});

// Map is on by default; only an explicit toggle-off is remembered.
function autoOpenDealMap() {
  let pref = null;
  try { pref = localStorage.getItem('admin.dealMapOn'); } catch (_) {}
  if (pref === 'off') return;
  if (!deal.mapOn) setDealMapOn(true);
  else if (deal.map) deal.map.invalidateSize();  // pane was hidden while away
}

/* ZIP → center the map (opens it first when off) */
async function centerDealMapOnZip() {
  const z = $('#deal-zip').value.trim();
  if (!/^\d{5}$/.test(z)) { if (z) toast('ZIP must be 5 digits', 'err'); return; }
  let body;
  try { body = await apiFetch('/api/geo/zip?zip=' + z); }
  catch (e) { toast('ZIP lookup failed: ' + (e.message || e), 'err'); return; }
  if (body.precision == null || body.lat == null) { toast(`ZIP ${z} not found`, 'err'); return; }
  if (!deal.mapOn) await setDealMapOn(true);
  if (!deal.map) return;  // mount failed; toast already shown
  deal.map.leaflet.setView([body.lat, body.lng], 9);
}
$('#deal-zip').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); centerDealMapOnZip(); }
});
$('#deal-zip').addEventListener('change', centerDealMapOnZip);

/* ── Deal browser chrome: lists / tags / saved searches / comps drawer ── */

const _dealEsc = (t) => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

async function loadDealMeta() {
  deal.metaLoaded = true;
  const get = async (url) => {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };
  try {
    [deal.lists, deal.tagList, deal.searches] = await Promise.all([
      get('/api/deals/lists'), get('/api/deals/tags'), get('/api/deals/searches'),
    ]);
  } catch (e) {
    deal.metaLoaded = false;
    console.warn('deal meta load failed:', e);
    return;
  }
  const fillSel = (sel, items, cur) => {
    const el = $(sel);
    el.innerHTML = '<option value="">all</option>' + items.map(i =>
      `<option value="${_dealEsc(i.value)}">${_dealEsc(i.text)}</option>`).join('');
    el.value = [...el.options].some(o => o.value === String(cur)) ? String(cur) : '';
  };
  fillSel('#deal-list', deal.lists.map(l => ({value: l.id, text: `${l.name} (${l.count})`})), deal.listId);
  fillSel('#deal-tag', deal.tagList.map(t => ({value: t.tag, text: `${t.tag} (${t.count})`})), deal.tag);
  renderDealSearches();
}

/* saved-search chips above the filter bar: click = apply, × = delete */
function renderDealSearches() {
  const host = $('#deal-searches');
  host.hidden = !deal.searches.length;
  host.innerHTML = deal.searches.map(s =>
    `<span class="deal-search-chip" data-id="${s.id}" title="${_dealEsc(JSON.stringify(s.params))}">
       ★ ${_dealEsc(s.name)}${s.alert ? ' 🔔' : ''}
       <button type="button" class="deal-search-x" data-id="${s.id}" title="delete saved search">×</button>
     </span>`).join('');
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
  deal.status = params.status || 'active';
  deal.offset = 0;
  // sync controls back to the restored state
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
  $$('#deal-bids-filter .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.value === deal.maxBids));
  $('#deal-min-margin').value = deal.minMargin;
  $('#deal-min-price').value = deal.minPrice;
  $('#deal-max-price').value = deal.maxPrice;
  $('#deal-max-dist').value = deal.maxDist;
  $$('#deal-status-filter .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.value === deal.status));
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

$('#deal-searches').addEventListener('click', async (e) => {
  const x = e.target.closest('.deal-search-x');
  if (x) {
    try {
      const r = await fetch(`/api/deals/searches/${x.dataset.id}`, {method: 'DELETE'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      deal.searches = deal.searches.filter(s => String(s.id) !== x.dataset.id);
      renderDealSearches();
    } catch (err) { toast(`delete failed: ${err}`, 'err'); }
    return;
  }
  const chip = e.target.closest('.deal-search-chip');
  if (!chip) return;
  const s = deal.searches.find(s => String(s.id) === chip.dataset.id);
  if (s) applyDealSearch(s.params || {});
});

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

/* ♥ popover: checkbox per list + inline "new list" input */
function openListPop(heartBtn, key) {
  const memb = dealMemb(key);
  const boxes = deal.lists.map(l => `
    <label class="deal-pop-row">
      <input type="checkbox" data-list="${l.id}" ${memb.has(l.id) ? 'checked' : ''}>
      ${_dealEsc(l.name)} <span class="deal-pop-count">${l.count}</span>
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
      try {
        const r = await fetch(`/api/deals/lists/${listId}/items/${key}`, {method});
        if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
        cb.checked ? memb.add(listId) : memb.delete(listId);
        const l = deal.lists.find(l => l.id === listId);
        if (l) l.count = Math.max(0, Number(l.count) + (cb.checked ? 1 : -1));
        heartBtn.classList.toggle('on', memb.size > 0);
      } catch (err) {
        cb.checked = !cb.checked;
        toast(`list update failed: ${err}`, 'err');
      }
    });
  });
  const addNew = async () => {
    const name = $('#deal-newlist-name').value.trim();
    if (!name) return;
    try {
      let r = await fetch('/api/deals/lists', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const created = await r.json();
      r = await fetch(`/api/deals/lists/${created.id}/items/${key}`, {method: 'PUT'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      memb.add(created.id);
      heartBtn.classList.add('on');
      deal.metaLoaded = false;
      await loadDealMeta();
      openListPop(heartBtn, key);   // re-render with the new list visible
    } catch (err) { toast(`create list failed: ${err}`, 'err'); }
  };
  $('#deal-newlist-add').addEventListener('click', addNew);
  $('#deal-newlist-name').addEventListener('keydown', (e) => { if (e.key === 'Enter') addNew(); });
}

/* comps detail drawer: verdict summary + each kept comp title/price/url */
function openDealDrawer(key) {
  const row = deal.rows.find(r => dealKey(r) === key);
  const v = row?.verdict;
  if (!v) return;
  const comps = (v.comps || []).map(c => `
    <div class="deal-drawer-comp">
      <a href="${_dealEsc(c.url)}" target="_blank" rel="noopener">${_dealEsc(c.title)}</a>
      <span class="deal-drawer-price">$${Number(c.price).toFixed(0)}</span>
    </div>`).join('') || '<div class="deal-pop-empty">no kept comps</div>';
  const d = $('#deal-drawer');
  d.innerHTML = `
    <div class="deal-drawer-head">
      <span>COMPS · ${v.comp_count} kept</span>
      <button type="button" class="btn btn-small" id="deal-drawer-close">× close</button>
    </div>
    <div class="deal-drawer-title">${_dealEsc(row.title)}</div>
    <div class="deal-drawer-meta">
      method <b>${_dealEsc(v.method)}</b> · est. resale <b>$${Math.round(v.est_resale)}</b>
      · margin <b>${Math.round(v.margin_pct)}%</b> · ${_dealEsc(v.confidence)} confidence
    </div>
    ${comps}`;
  d.hidden = false;
  $('#deal-drawer-close').addEventListener('click', () => { d.hidden = true; });
}

/* row-level delegation: hearts, tag chips, comps links */
$('#deal-rows').addEventListener('click', async (e) => {
  const heart = e.target.closest('.deal-heart');
  if (heart) {
    e.stopPropagation();
    openListPop(heart, heart.dataset.key);
    return;
  }
  const chipX = e.target.closest('.deal-chip-x');
  if (chipX) {
    const {key, tag} = chipX.dataset;
    try {
      const r = await fetch(`/api/deals/tags/${key}/${encodeURIComponent(tag)}`, {method: 'DELETE'});
      if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
      dealLotTags(key).delete(tag);
      chipX.closest('.deal-chip').remove();
      deal.metaLoaded = false;   // tag counts changed
    } catch (err) { toast(`remove tag failed: ${err}`, 'err'); }
    return;
  }
  const tagAdd = e.target.closest('.deal-tag-add');
  if (tagAdd) {
    const key = tagAdd.dataset.key;
    const tag = (prompt('Tag this lot:') || '').trim().toLowerCase();
    if (!tag) return;
    try {
      const r = await fetch(`/api/deals/tags/${key}/${encodeURIComponent(tag)}`, {method: 'PUT'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      dealLotTags(key).add(tag);
      const chip = document.createElement('span');
      chip.className = 'deal-chip';
      chip.innerHTML = `${_dealEsc(tag)}<button type="button" class="deal-chip-x" data-key="${key}" data-tag="${_dealEsc(tag)}" title="remove tag">×</button>`;
      tagAdd.before(chip);
      deal.metaLoaded = false;
    } catch (err) { toast(`add tag failed: ${err}`, 'err'); }
    return;
  }
  const compsLink = e.target.closest('.deal-comps-link');
  if (compsLink) {
    e.preventDefault();
    openDealDrawer(compsLink.dataset.key);
  }
});

/* ★ save-search / 🔔 create-alert popover: name + alert checkbox → POST /api/deals/searches */
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
  const save = async () => {
    const name = $('#deal-search-name').value.trim();
    if (!name) { toast('name required', 'err'); return; }
    try {
      const r = await fetch('/api/deals/searches', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, params: currentDealParams(),
                              alert: $('#deal-search-alert').checked}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      closeDealPops();
      deal.metaLoaded = false;
      await loadDealMeta();
      toast(`saved search “${name}”`, 'ok');
    } catch (err) { toast(`save failed: ${err}`, 'err'); }
  };
  $('#deal-search-save').addEventListener('click', save);
  $('#deal-search-name').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') save(); });
}
$('#deal-save-search').addEventListener('click', (e) => {
  e.stopPropagation();
  openSaveSearchPop(e.currentTarget);
});
$('#deal-create-alert').addEventListener('click', (e) => {
  e.stopPropagation();
  openSaveSearchPop(e.currentTarget, {alert: true});
});

// ───────── 11 Tracking: bid history for chosen lots ─────────
// Membership + latest state come from /api/tracking; the per-lot timeline
// (every observed change of price / bids / leader) from …/history. The web
// process polls on its own scheduler tick, so this tab is read-mostly.

const trk = {
  items: [], labels: [], labelFilter: '',
  open: new Set(),        // "asset/account" keys with the history drawer open
  history: {},            // key -> /history payload (cached until refresh)
};

function _trkKey(r) { return `${r.asset_id}/${r.account_id}`; }
function _trkMoney(v, cur) {
  if (v == null) return '—';
  return `${cur && cur !== 'USD' ? cur + ' ' : '$'}${Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}
function _trkAgo(iso) {
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function _trkWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

async function loadTracking() {
  try {
    const body = await apiFetch('/api/tracking' + (trk.labelFilter ? `?label=${encodeURIComponent(trk.labelFilter)}` : ''));
    trk.items = body.items || [];
    trk.labels = body.labels || [];
  } catch (err) {
    $('#trk-rows').innerHTML = `<tr><td colspan="8" class="drafts-empty">Failed to load: ${esc(String(err))}</td></tr>`;
    return;
  }
  renderTrackingLabels();
  renderTrackingRows();
}

function renderTrackingLabels() {
  const seg = $('#trk-label-filter');
  const opts = $('#trk-label-options');
  seg.innerHTML = [`<button type="button" class="seg-btn ${trk.labelFilter ? '' : 'active'}" data-value="">all</button>`]
    .concat(trk.labels.map(l =>
      `<button type="button" class="seg-btn ${trk.labelFilter === l.label ? 'active' : ''}" data-value="${esc(l.label)}">${esc(l.label)} <span class="fav-count">${l.open}/${l.n}</span></button>`))
    .join('');
  opts.innerHTML = trk.labels.map(l => `<option value="${esc(l.label)}">`).join('');
  seg.querySelectorAll('.seg-btn').forEach(b => b.addEventListener('click', () => {
    trk.labelFilter = b.dataset.value;
    loadTracking();
  }));
  const open = trk.items.filter(r => !r.closed_at).length;
  const closed = trk.items.length - open;
  const sold = trk.items.filter(r => r.closed_at && r.final_bid != null);
  const total = sold.reduce((a, r) => a + Number(r.final_bid || 0), 0);
  $('#trk-summary').innerHTML =
    `<span class="ac-label">${open} open · ${closed} closed${sold.length ? ` · ${_trkMoney(total)} realized across ${sold.length}` : ''}</span>`;
}

function _trkStatus(r) {
  if (r.closed_at) {
    const tag = r.final_bid_count === 0 ? 'no bids' : (r.final_bid_count === 1 ? 'low bid' : 'sold');
    return `<span class="lex ${r.final_bid_count > 1 ? 'done' : 'pending'}">${tag}</span> <span class="tiny">${_trkWhen(r.closed_at)}</span>`;
  }
  if (!r.end_utc) return `<span class="lex running">open</span> <span class="tiny">${r.poll_error ? esc(r.poll_error) : 'end unknown'}</span>`;
  const secs = (new Date(r.end_utc).getTime() - Date.now()) / 1000;
  const hot = secs < 1800;
  return `<span class="lex ${hot ? 'error' : 'running'}">${secs <= 0 ? 'closing' : 'open'}</span> <span class="tiny ${hot ? 'trk-hot' : ''}">${esc(_fmtRemaining(secs))}</span>`;
}

function renderTrackingRows() {
  const tb = $('#trk-rows');
  if (!trk.items.length) {
    tb.innerHTML = `<tr><td colspan="8" class="drafts-empty">Nothing tracked yet — paste a GovDeals lot URL above, or star one on 04 Auctions.</td></tr>`;
    return;
  }
  tb.innerHTML = trk.items.map(r => {
    const key = _trkKey(r);
    const closed = !!r.closed_at;
    const bids = closed ? r.final_bid_count : r.bid_count;
    const price = closed ? r.final_bid : r.current_bid;
    const who = closed ? r.final_bidder_username : r.high_bidder_username;
    const whoId = closed ? r.final_bidder : r.high_bidder;
    const traffic = !closed && r.visitors != null
      ? `<span class="tiny" title="visitors / hits / watchers">${r.visitors}v · ${r.hits ?? '–'}h · ${r.watcher_count ?? '–'}w</span>` : '';
    const isOpen = trk.open.has(key);
    return `
      <tr class="trk-row ${closed ? 'trk-closed' : ''}" data-key="${key}">
        <td><input class="trk-label-cell" value="${esc(r.label)}" data-key="${key}" title="Rename list (Enter)"></td>
        <td class="trk-lot">
          <a href="${esc(r.url || `https://www.govdeals.com/en/asset/${r.asset_id}/${r.account_id}`)}" target="_blank" rel="noopener">${esc(r.title || key)}</a>
          <div class="tiny mono">${key}${r.auction_id ? ` · auction ${r.auction_id}` : ''}${r.source === 'favorite' ? ' · ★' : ''}</div>
        </td>
        <td>${_trkStatus(r)}</td>
        <td class="num mono">${bids ?? '—'}</td>
        <td class="num mono">${_trkMoney(price, r.currency_code)}</td>
        <td><span class="mono trk-handle">${esc(who || '—')}</span>${whoId ? `<span class="tiny"> ${whoId}</span>` : ''} ${traffic}</td>
        <td class="tiny">${_trkAgo(r.last_polled_at)}${r.poll_error && !closed ? `<div class="trk-err" title="${esc(r.poll_error)}">⚠ ${esc(r.poll_error.slice(0, 40))}</div>` : ''}</td>
        <td class="trk-actions">
          <button type="button" class="btn btn-small trk-hist" data-key="${key}">${isOpen ? '▾' : '▸'} history</button>
          <button type="button" class="btn btn-small btn-danger trk-del" data-key="${key}" title="Stop tracking (keeps observations)">✕</button>
        </td>
      </tr>
      ${isOpen ? `<tr class="trk-drawer" data-key="${key}"><td colspan="8">${_trkDrawer(key)}</td></tr>` : ''}`;
  }).join('');

  tb.querySelectorAll('.trk-hist').forEach(b => b.addEventListener('click', async () => {
    const key = b.dataset.key;
    if (trk.open.has(key)) { trk.open.delete(key); renderTrackingRows(); return; }
    trk.open.add(key);
    renderTrackingRows();
    if (!trk.history[key]) {
      try {
        trk.history[key] = await apiFetch(`/api/tracking/${key}/history`);
      } catch (err) {
        trk.history[key] = {error: String(err)};
      }
      renderTrackingRows();
    }
  }));
  tb.querySelectorAll('.trk-del').forEach(b => b.addEventListener('click', async () => {
    const key = b.dataset.key;
    b.disabled = true;
    try {
      await apiFetch(`/api/tracking/${key}`, {method: 'DELETE'});
      toast(`stopped tracking ${key}`);
      await loadTracking();
    } catch (err) { toast(`remove failed: ${err}`, 'err'); b.disabled = false; }
  }));
  tb.querySelectorAll('.trk-label-cell').forEach(inp => {
    const commit = async () => {
      const key = inp.dataset.key;
      const r = trk.items.find(x => _trkKey(x) === key);
      const label = inp.value.trim() || 'default';
      if (!r || r.label === label) return;
      try {
        await apiFetch(`/api/tracking/${key}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({label}),
        });
        toast(`${key} → ${label}`);
        await loadTracking();
      } catch (err) { toast(`rename failed: ${err}`, 'err'); }
    };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } });
    inp.addEventListener('blur', commit);
  });
}

function _trkDrawer(key) {
  const h = trk.history[key];
  if (!h) return `<div class="drafts-empty">Loading history…</div>`;
  if (h.error) return `<div class="drafts-empty">Failed: ${esc(h.error)}</div>`;
  const obs = h.observations || [];
  if (!obs.length) {
    return `<div class="drafts-empty">No observations yet — the first poll lands within a minute of adding (or hit ⟳ poll now).</div>`;
  }
  const bidders = (h.bidders || []).map(b => `
    <div class="trk-bidder">
      <span class="mono trk-handle">${esc(b.handle || '—')}</span>
      <span class="tiny">id ${b.bidder_id}</span>
      <span class="tiny">led ${b.times_led}× · high point ${_trkMoney(b.max_bid)}</span>
      <span class="tiny">${_trkWhen(b.first_led_at)} → ${_trkWhen(b.last_led_at)}</span>
    </div>`).join('');
  const rivalsByBidder = {};
  (h.rivals || []).forEach(x => { (rivalsByBidder[x.bidder_id] ||= []).push(x); });
  const rivals = Object.entries(rivalsByBidder).map(([id, lots]) => `
    <div class="trk-rival">
      <div class="tiny"><span class="mono trk-handle">${esc(lots[0].handle || '—')}</span> also led:</div>
      ${lots.map(l => `
        <div class="trk-rival-lot">
          <a href="https://www.govdeals.com/en/asset/${l.asset_id}/${l.account_id}" target="_blank" rel="noopener">${esc(l.title || `${l.asset_id}/${l.account_id}`)}</a>
          <span class="tiny">${_trkMoney(l.max_bid)}${l.outcome ? ` · ${esc(l.outcome)}${l.final_bid != null ? ` @ ${_trkMoney(l.final_bid)}` : ''}` : ''}${l.won ? ' · <b>won</b>' : ''}</span>
        </div>`).join('')}
    </div>`).join('');
  let prev = null;
  const timeline = obs.slice().reverse().map(o => {
    const dPrice = prev && prev.current_bid != null && o.current_bid != null ? Number(prev.current_bid) - Number(o.current_bid) : null;
    const leadChange = prev && prev.high_bidder !== o.high_bidder;
    const row = `
      <div class="trk-obs ${leadChange ? 'trk-lead' : ''}">
        <span class="tiny mono">${_trkWhen(o.observed_at)}</span>
        <span class="mono">a${o.auction_id}</span>
        <span class="mono num">${o.bid_count ?? 0} bids</span>
        <span class="mono num">${_trkMoney(o.current_bid, o.currency_code)}</span>
        <span class="mono trk-handle">${esc(o.high_bidder_username || '—')}</span>
        <span class="tiny">${o.visitors != null ? `${o.visitors}v · ${o.hits ?? '–'}h · ${o.watcher_count ?? '–'}w` : ''}${o.status && o.status !== 'STA' ? ` · ${esc(o.status)}` : ''}</span>
      </div>`;
    prev = o;
    return row;
  });
  return `
    <div class="trk-drawer-grid">
      <div>
        <div class="tiny trk-h">BIDDERS SEEN LEADING</div>
        ${bidders || '<div class="tiny">nobody has bid</div>'}
        ${rivals ? `<div class="tiny trk-h">SAME BIDDERS ELSEWHERE</div>${rivals}` : ''}
      </div>
      <div>
        <div class="tiny trk-h">TIMELINE (newest first · highlighted = lead changed)</div>
        ${timeline.join('')}
      </div>
    </div>`;
}

(() => {
  const add = async () => {
    const ref = $('#trk-ref').value.trim();
    const label = $('#trk-label').value.trim() || 'default';
    if (!ref) { toast('paste a GovDeals lot URL first', 'err'); return; }
    const btn = $('#trk-add');
    btn.disabled = true;
    try {
      const row = await apiFetch('/api/tracking', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ref, label}),
      });
      toast(`tracking ${row.title || _trkKey(row)} under "${row.label}"`);
      $('#trk-ref').value = '';
      await loadTracking();
    } catch (err) { toast(`add failed: ${err}`, 'err'); }
    finally { btn.disabled = false; }
  };
  $('#trk-add')?.addEventListener('click', add);
  $('#trk-ref')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
  $('#trk-refresh')?.addEventListener('click', () => { trk.history = {}; loadTracking(); });
  $('#trk-sync')?.addEventListener('click', async () => {
    const btn = $('#trk-sync');
    btn.disabled = true; btn.textContent = '⟳ polling…';
    try {
      const rep = await apiFetch('/api/tracking/sync', {method: 'POST'});
      toast(`polled ${rep.polled} · ${rep.recorded} changes · ${rep.closed} closed${rep.errors ? ` · ${rep.errors} errors` : ''}`);
      trk.history = {};
      await loadTracking();
    } catch (err) { toast(`poll failed: ${err}`, 'err'); }
    finally { btn.disabled = false; btn.textContent = '⟳ poll now'; }
  });
})();

// All module-level state (`auc`, etc.) and per-tab loaders are defined above,
// so it is now safe to restore the saved tab and trigger its loader.
restoreLastTab();
