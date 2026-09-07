// static/admin/test_scrape.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, apiFetch, esc, escapeHtml, SOURCE_NAMES} from './shared.js';

// ─────────────────────────── test scrape (08) ───────────────────────────
// Live keyword probe against /api/test-scrape. Read-only — exists so a new
// category ("desks", "lockers") can be eyeballed for relevance + images
// before its search term is committed to the scrapers' SEARCH_TERMS.

const _ts = {source: 'both'};

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

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
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
}

export function load() { $('#ts-q')?.focus(); }
