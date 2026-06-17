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
  compare:  $('[data-pane="compare"]'),
  auctions: $('[data-pane="auctions"]'),
  inventory: $('[data-pane="inventory"]'),
  inquiries: $('[data-pane="inquiries"]'),
  'listings-db': $('[data-pane="listings-db"]'),
  'test-scrape': $('[data-pane="test-scrape"]'),
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
  if (name === 'compare') loadCompare();
  if (name === 'auctions') loadAuctions();
  if (name === 'inventory') loadInventory();
  if (name === 'inquiries') loadInquiries();
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
  const payload = {
    url: fd.get('url'),
    skip_dewatermark: fd.get('skip_dewatermark') === 'on',
    skip_fb: fd.get('skip_fb') === 'on',
    skip_ebay: fd.get('skip_ebay') === 'on',
    price: fd.get('price') ? parseInt(fd.get('price'), 10) : null,
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

// ───────── compare ─────────

const FIELDS = ['title','location','city','state','zip_code','quantity',
                'chair_type','dimensions','suggested_price_per_chair','style_suffix'];

async function loadCompare() {
  const list = $('#compare-list');
  const summary = $('#compare-summary');
  list.innerHTML = '<div class="drafts-empty">Loading comparisons…</div>';
  const res = await fetch('/api/compare');
  const {entries} = await res.json();

  let nMatch = 0, nWrong = 0, nPending = 0;
  for (const e of entries) {
    if (e.rating === 'match') nMatch++;
    else if (e.rating === 'wrong') nWrong++;
    else nPending++;
  }
  summary.innerHTML = `
    <div><strong>${entries.length}</strong> comparisons logged</div>
    <div class="stat-ok"><strong>${nMatch}</strong> Gemini matched</div>
    <div class="stat-bad"><strong>${nWrong}</strong> Gemini wrong</div>
    <div class="stat-pend"><strong>${nPending}</strong> unrated</div>
  `;

  if (!entries.length) {
    list.innerHTML = '<div class="drafts-empty">No llm_compare logs yet.</div>';
    return;
  }
  list.innerHTML = '';
  for (const e of entries) list.appendChild(renderCompareEntry(e));
}

function renderCompareEntry(e) {
  const wrap = document.createElement('article');
  wrap.className = 'compare-entry';
  const date = new Date(e.timestamp * 1000);
  const dateStr = date.toISOString().replace('T', ' ').slice(0, 19);

  const head = document.createElement('header');
  head.className = 'compare-entry-head';
  head.innerHTML = `
    <span class="ce-id">#${e.id}</span>
    <span class="ce-time">${dateStr} · ${esc(e.filename)}</span>
    <span class="ce-rate">
      <button data-rate="match" class="${e.rating==='match'?'active match':''}">★ Match</button>
      <button data-rate="wrong" class="${e.rating==='wrong'?'active wrong':''}">✕ Wrong</button>
    </span>
  `;
  wrap.appendChild(head);

  const table = document.createElement('div');
  table.className = 'diff-table';
  table.innerHTML = `
    <div class="diff-header">field</div>
    <div class="diff-header">primary · ${esc((e.primary || {}).source || 'claude')}</div>
    <div class="diff-header">secondary · ${esc((e.secondary || {}).source || 'gemini')}</div>
  `;
  for (const f of FIELDS) {
    const a = (e.primary || {})[f];
    const b = (e.secondary || {})[f];
    const same = String(a ?? '') === String(b ?? '');
    const cls = same ? '' : 'diff-mismatch';
    table.insertAdjacentHTML('beforeend', `
      <div>${f}</div>
      <div class="${cls}">${a == null || a === '' ? '<span class="diff-empty">—</span>' : esc(String(a))}</div>
      <div class="${cls}">${b == null || b === '' ? '<span class="diff-empty">—</span>' : esc(String(b))}</div>
    `);
  }
  wrap.appendChild(table);

  head.querySelectorAll('.ce-rate button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const newRate = btn.classList.contains('active') ? null : btn.dataset.rate;
      await withButtonLoading(btn, null, async () => {
        try {
          await apiFetch(`/api/compare/${e.id}/rate`, {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({rating: newRate}),
          });
          e.rating = newRate;
          loadCompare();
        } catch (err) {
          toast('Rating failed: ' + (err.message || err), 'err');
        }
      });
    });
  });
  return wrap;
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
};

function _assetIdFromLink(link) {
  if (!link) return '';
  let m = link.match(/\/asset\/(\d+)\/(\d+)/);
  if (m) return `${m[1]}/${m[2]}`;
  m = link.match(/[?&]auc=(\d+)/);
  if (m) return `ps:${m[1]}`;
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
      ? `Cache is empty for ${auc.source === 'gd' ? 'GovDeals' : 'Public Surplus'}. Hit ⟳ scrape now to populate.`
      : `No listings matched the current filters. See details above.`;
    grid.innerHTML = `<div class="drafts-empty">${msg}</div>`;
    return;
  }
  grid.innerHTML = '';
  for (const it of items) grid.appendChild(renderAuctionCard(it));
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
    const [invRes, statsRes] = await Promise.all([
      fetch('/api/inventory' + (_invStatusFilter ? `?status=${_invStatusFilter}` : '')),
      fetch('/api/inventory-stats'),
    ]);
    const inv = await invRes.json();
    const stats = await statsRes.json();
    _invItems = inv.items || [];
    renderInvStats(stats);
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
        ? `<img src="${item.hero_image_url}" alt="">`
        : '<div class="inv-hero-fallback">◉</div>'}</td>
      <td>
        <div class="inv-title">${escapeHtml(item.title || '—')}</div>
        <div class="inv-sub mono tiny">${escapeHtml((item.city || '') + (item.state ? ', ' + item.state : '') + (item.zip_code ? ' ' + item.zip_code : ''))}${item.chair_type ? ' · ' + escapeHtml(item.chair_type) : ''}</div>
        ${(item.contact_email || item.contact_phone || item.contact_name) ? `
          <div class="inv-sub mono tiny inv-contact">☎ ${escapeHtml([item.contact_name, item.contact_phone, item.contact_email].filter(Boolean).join(' · '))}</div>
        ` : ''}
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
          ${['draft','listed','hidden','sold_out','owned','won_pickup','active_bid','lost'].map(s =>
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
    const srcClass = r.source === 'gd' ? 'src-gd' : (r.source === 'ps' ? 'src-ps' : 'src-other');
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
  const sources = _ts.source === 'both' ? ['gd', 'ps'] : [_ts.source];

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
      const name = sources[i] === 'gd' ? 'GovDeals' : 'Public Surplus';
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
  const srcName = it._source === 'gd' ? 'GovDeals' : 'Public Surplus';

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

// All module-level state (`auc`, etc.) and per-tab loaders are defined above,
// so it is now safe to restore the saved tab and trigger its loader.
restoreLastTab();
