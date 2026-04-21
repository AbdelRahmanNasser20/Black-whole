// listing_automation · console
// Vanilla JS. SSE for streaming. No framework, no bundle.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ───────── tabs ─────────

const panels = {
  launcher: $('[data-pane="launcher"]'),
  drafts:   $('[data-pane="drafts"]'),
  compare:  $('[data-pane="compare"]'),
  auctions: $('[data-pane="auctions"]'),
  inventory: $('[data-pane="inventory"]'),
  inquiries: $('[data-pane="inquiries"]'),
};

$$('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    Object.entries(panels).forEach(([k, el]) => {
      if (el) el.hidden = (k !== btn.dataset.tab);
    });
    if (btn.dataset.tab === 'drafts') loadDrafts();
    if (btn.dataset.tab === 'compare') loadCompare();
    if (btn.dataset.tab === 'auctions') loadAuctions();
    if (btn.dataset.tab === 'inventory') loadInventory();
    if (btn.dataset.tab === 'inquiries') loadInquiries();
  });
});

// ───────── clock ─────────

setInterval(() => {
  const d = new Date();
  $('#clock').textContent = d.toTimeString().slice(0, 8);
}, 1000);

// ───────── launcher ─────────

const consoleEl = $('#console');
const showEvents = $('#show-events');
const autoscroll = $('#autoscroll');

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

$('#cancel-btn').addEventListener('click', async () => {
  await fetch('/api/runs/cancel', {method: 'POST'});
});

$('#queue-clear').addEventListener('click', async () => {
  await fetch('/api/runs/queue/clear', {method: 'POST'});
});

$('#pp-confirm').addEventListener('click', async () => {
  const v = parseInt($('#pp-input').value, 10);
  if (!v) return;
  await fetch('/api/runs/stdin', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({line: String(v)}),
  });
  $('#price-prompt').hidden = true;
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
        <a class="disabled" title="run pipeline to populate">↗ Facebook draft</a>
        <a class="disabled" title="run pipeline to populate">↗ eBay draft</a>
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

const FIELDS = ['title','location','city','state','quantity',
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
      await fetch(`/api/compare/${e.id}/rate`, {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({rating: newRate}),
      });
      e.rating = newRate;
      loadCompare();
    });
  });
  return wrap;
}

// ───────── auctions ─────────

const auc = {
  source: 'gd',
  items: [],
  loading: false,
};

$$('#auc-source .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#auc-source .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    auc.source = btn.dataset.value;
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

$('#auc-refresh').addEventListener('click', async () => {
  await fetch('/api/auctions/refresh', {method: 'POST'});
  loadAuctions();
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
  const res = await fetch('/api/scrape/start', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({source, test}),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({detail: res.statusText}));
    setScrapeStrip({status: 'error', source, last_line: err.detail || res.statusText});
    return;
  }
  // Immediately show "running" in the UI; SSE will refine with live lines.
  setScrapeStrip({
    status: 'running',
    source,
    current_step: source === 'both' ? 'gd' : source,
    current_stage: 'starting',
    stage_detail: null,
    last_line: 'starting…',
    test_mode: test,
  });
}

$('#scrape-cancel').addEventListener('click', async () => {
  await fetch('/api/scrape/cancel', {method: 'POST'});
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

let scrapeES;
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

$('#auc-queue-all').addEventListener('click', async () => {
  const urls = auc.items.map(it => it.link).filter(Boolean);
  if (!urls.length) return;
  await queueRuns(urls);
});

async function loadAuctions() {
  if (auc.loading) return;
  auc.loading = true;
  const grid = $('#auction-grid');
  const status = $('#auction-status');
  const useCond = $('#auc-condition').checked;
  status.textContent = useCond
    ? 'Loading auctions (condition scoring may take 3–10s)…'
    : 'Loading auctions…';
  grid.innerHTML = '<div class="drafts-empty">Loading…</div>';

  const qs = new URLSearchParams({
    source: auc.source,
    n: $('#auc-n').value,
    min_qty: $('#auc-min-qty').value,
    condition: useCond ? '1' : '0',
    active_only: $('#auc-expired').checked ? '0' : '1',
    max_stale_days: $('#auc-stale').value,
  });
  try {
    const res = await fetch('/api/auctions?' + qs.toString());
    const body = await res.json();
    if (!res.ok) {
      status.textContent = `Error: ${body.detail || res.statusText}`;
      grid.innerHTML = '';
      return;
    }
    auc.items = body.items || [];
    status.textContent = `${auc.items.length} listings · ${body.cached ? `cached ${body.age}s ago` : 'fresh'}`;
    renderAuctions(auc.items);
  } catch (err) {
    status.textContent = `Network error: ${err}`;
    grid.innerHTML = '';
  } finally {
    auc.loading = false;
  }
}

function renderAuctions(items) {
  const grid = $('#auction-grid');
  if (!items.length) {
    grid.innerHTML = '<div class="drafts-empty">No listings matched. Try lowering min qty or include expired.</div>';
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

  card.innerHTML = `
    <div class="auction-img">${img}</div>
    <div class="auction-body">
      <h3 class="auction-title">${esc(it.title || it.raw_title || '—')}</h3>
      <div class="auction-meta">
        <span class="auction-qty">${(it.quantity||0).toLocaleString()} ×</span>
        ${it.price ? `<span class="auction-price">${esc(it.price)}</span>` : ''}
        ${condPill}
      </div>
      ${ends ? `<div class="auction-ends">⏱ ${esc(ends)}</div>` : ''}
      ${it.condition_note ? `<div class="auction-note">${esc(it.condition_note)}</div>` : ''}
      <div class="auction-actions">
        <a href="${esc(it.link)}" target="_blank" rel="noopener" class="auction-link">↗ source</a>
        <button class="btn btn-small btn-primary auction-launch"
                ${launchDisabled ? 'disabled' : ''}
                title="${esc(launchTitle)}"
                data-url="${esc(it.link)}">▶ launch</button>
      </div>
    </div>
  `;

  const launchBtn = card.querySelector('.auction-launch');
  if (launchBtn && !launchDisabled) {
    launchBtn.addEventListener('click', async () => {
      launchBtn.disabled = true;
      launchBtn.textContent = '⏱ queued';
      await queueRuns([it.link]);
      setTimeout(() => {
        launchBtn.disabled = false;
        launchBtn.textContent = '▶ launch';
      }, 2000);
    });
  }
  return card;
}

async function queueRuns(urls) {
  const res = await fetch('/api/runs/queue', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({urls}),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({detail: res.statusText}));
    appendLine('stderr', `[queue failed] ${err.detail || res.statusText}`);
    return;
  }
  // Refresh the launcher state so the queue strip updates immediately.
  fetch('/api/runs/state').then(r => r.json()).then(applyState).catch(() => {});
}


// ─────────────────────────── Inventory tab ───────────────────────────

let _invItems = [];
let _invStatusFilter = '';

async function loadInventory() {
  const tbody = $('#inv-tbody');
  tbody.innerHTML = '<tr><td colspan="9" class="drafts-empty">Loading…</td></tr>';
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
    tbody.innerHTML = `<tr><td colspan="9" class="drafts-empty">Load failed: ${e}</td></tr>`;
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
    tbody.innerHTML = '<tr><td colspan="9" class="drafts-empty">No rows. Click ↓ backfill to import folder listings.</td></tr>';
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
        <div class="inv-sub mono tiny">${escapeHtml((item.city || '') + (item.state ? ', ' + item.state : ''))}${item.chair_type ? ' · ' + escapeHtml(item.chair_type) : ''}</div>
      </td>
      <td>
        <input type="number" class="inv-qty" value="${item.quantity_remaining ?? ''}" min="0" data-field="quantity_remaining">
        <div class="inv-sub mono tiny">of ${item.quantity_original ?? '—'}</div>
      </td>
      <td>
        <input type="number" class="inv-price" value="${item.price_per_chair ?? ''}" min="0" step="1" data-field="price_per_chair">
      </td>
      <td>
        <select class="inv-status" data-field="status">
          ${['draft','listed','hidden','sold_out'].map(s =>
            `<option value="${s}" ${s===item.status?'selected':''}>${s}</option>`).join('')}
        </select>
      </td>
      <td>${renderPlatformCell(item, 'facebook')}</td>
      <td>${renderPlatformCell(item, 'ebay')}</td>
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
        <button class="plat-clear" data-platform="${platform}" title="Clear URL">✕</button>
      </div>`;
  }
  return `<button class="btn btn-small plat-set" data-platform="${platform}">paste URL</button>`;
}

async function onInvFieldChange(e) {
  const tr = e.target.closest('tr');
  const lotId = tr.dataset.lotId;
  const field = e.target.dataset.field;
  let value = e.target.value;
  if (field === 'quantity_remaining' || field === 'price_per_chair') {
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
    alert('Update failed: ' + err.message);
  }
}

async function onInvAction(e) {
  const tr = e.target.closest('tr');
  const lotId = tr.dataset.lotId;
  const act = e.target.dataset.act;
  if (act === 'view') {
    window.open(`/listings/${encodeURIComponent(lotId)}`, '_blank');
    return;
  }
  if (act === 'delete') {
    if (!confirm(`Delete lot ${lotId} from the ledger? (Folder on disk is untouched.)`)) return;
    const r = await fetch(`/api/inventory/${encodeURIComponent(lotId)}`, {method: 'DELETE'});
    if (r.ok) loadInventory();
    else alert('Delete failed');
    return;
  }
  if (act === 'republish') {
    const item = _invItems.find(x => x.lot_id === lotId);
    if (!item) return;
    const url = item.govdeals_url;
    if (!url) {
      alert('This row has no GovDeals URL — republish only works for scraped lots.');
      return;
    }
    if (!confirm(`Republish ${lotId}? This ignores the dedup check and spends API tokens.`)) return;
    const res = await fetch('/api/runs/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, force_republish: true}),
    });
    if (res.ok) {
      alert('Queued. Switch to the Launcher tab to watch.');
    } else {
      const err = await res.json().catch(() => ({}));
      alert('Republish failed: ' + (err.detail || res.statusText));
    }
  }
}

async function onPlatformSet(e) {
  const tr = e.target.closest('tr');
  const lotId = tr.dataset.lotId;
  const platform = e.target.dataset.platform;
  const url = prompt(`Paste the ${platform.toUpperCase()} URL for lot ${lotId}:`);
  if (!url) return;
  const r = await fetch(`/api/inventory/${encodeURIComponent(lotId)}/platform`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({platform, url: url.trim()}),
  });
  if (r.ok) loadInventory();
  else alert('Failed: ' + ((await r.json().catch(()=>({}))).detail || r.statusText));
}

async function onPlatformClear(e) {
  const tr = e.target.closest('tr');
  const lotId = tr.dataset.lotId;
  const platform = e.target.dataset.platform;
  if (!confirm(`Clear the ${platform.toUpperCase()} URL for lot ${lotId}?`)) return;
  const r = await fetch(`/api/inventory/${encodeURIComponent(lotId)}/platform`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({platform, url: null}),
  });
  if (r.ok) loadInventory();
}

function flashRow(tr, kind) {
  tr.classList.remove('flash-ok', 'flash-err');
  void tr.offsetWidth;  // reflow
  tr.classList.add(kind === 'ok' ? 'flash-ok' : 'flash-err');
}

$('#inv-refresh')?.addEventListener('click', loadInventory);
$('#inv-backfill')?.addEventListener('click', async () => {
  if (!confirm('Walk the listings folder and import any missing rows as drafts?')) return;
  const btn = $('#inv-backfill');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…backfilling';
  try {
    const r = await fetch('/api/inventory/backfill', {method: 'POST'});
    const data = await r.json();
    alert(`Backfill done.\nAdded ${data.counts.added}\nUpdated ${data.counts.updated}\nSkipped ${data.counts.skipped}`);
    loadInventory();
  } catch (e) {
    alert('Backfill failed: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
});

// status-filter segmented control
$$('#inv-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#inv-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _invStatusFilter = b.dataset.value;
  loadInventory();
}));


// ─────────────────────────── Inquiries tab ───────────────────────────

let _inqStatusFilter = '';

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
      const r = await fetch(`/api/inquiries/${q.id}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status}),
      });
      if (r.ok) loadInquiries();
    }));
    card.querySelector('[data-delete]').addEventListener('click', async () => {
      if (!confirm(`Delete inquiry #${q.id}?`)) return;
      const r = await fetch(`/api/inquiries/${q.id}`, {method: 'DELETE'});
      if (r.ok) loadInquiries();
    });
    el.appendChild(card);
  }
}

$('#inq-refresh')?.addEventListener('click', loadInquiries);
$$('#inq-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
  $$('#inq-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  _inqStatusFilter = b.dataset.value;
  loadInquiries();
}));


// ─────────────────────────── helpers ───────────────────────────

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}
function escapeAttr(s) { return escapeHtml(s); }
