// static/admin/test_scrape.js — Test Scrape tab (plan §10 E-test-scrape; §12: skeletons + empty/error only, no redesign).
// Live keyword probe against /api/test-scrape. Read-only — exists so a new category ("desks", "lockers") can be
// eyeballed for relevance + images before its search term is committed to the scrapers' SEARCH_TERMS.
//
// One read site (#42, fan-out over the chosen sources → UI.load on #ts-grid: card skeleton → ready | empty | error
// with Retry) and the ▶ button runs under UI.pending. Nothing fires on tab activation or popstate — the "read" is a
// live scrape of three auction sites, so it only ever starts from the button, Enter, or a CTA the operator clicks.
// No URL params on purpose (a shareable ?q= would re-scrape on every activation).
// Cards keep the auctions tab's `.auction-card` markup (app.css) — the `.ts-*` accents live in static/admin/test-scrape.css.
import {$, $$, toast, esc, SOURCE_NAMES} from './shared.js';
import {load as uiLoad, pending, api, renderEmpty} from '../ui/state.js';

const _ts = {source: 'both'};
// Each source page is a 30 s upstream HTTP call and a probe may walk 5 pages × 3 sites — UI.load's 15 s default would
// abort real runs, so the tab waits up to 2 min before calling it an error.
const PROBE_TIMEOUT_MS = 120000;
let probed = false;   // once a probe has run, its result / empty / error stays put across tab switches
const grid = () => $('#ts-grid');
const status = () => $('#ts-status');

function sourcesFor(sel) { return sel === 'both' ? ['gd', 'ps', 'bs'] : [sel]; }

function setSource(value) {
  _ts.source = value;
  $$('#ts-source .seg-btn').forEach(b => {
    const on = b.dataset.value === value;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

// ───────── fetch (one UI.load, N sources) ─────────

// Fan out per source so one dead site doesn't blank the others: partial failures toast and the rest render;
// only "every source failed" is an error state (Retry re-runs the same probe).
async function probe(q, pages, sources, signal) {
  const results = await Promise.allSettled(sources.map(s =>
    api(`/api/test-scrape?source=${s}&q=${encodeURIComponent(q)}&pages=${pages}`, {signal})));
  const items = [], errs = [], perSource = [];
  results.forEach((r, i) => {
    const name = SOURCE_NAMES[sources[i]] || sources[i];
    if (r.status === 'fulfilled') {
      perSource.push(`${name}: ${r.value.count}`);
      items.push(...r.value.items.map(it => ({...it, _source: sources[i]})));
    } else {
      errs.push(`${name}: ${r.reason?.message || r.reason}`);
    }
  });
  if (signal.aborted) throw Object.assign(new Error('aborted'), {aborted: true});
  if (!perSource.length) {
    const err = new Error(errs.join(' · '));
    err.status = results[0]?.reason?.status;
    throw err;
  }
  for (const e of errs) toast(`Test scrape failed — ${e}`, 'err');
  return {q, items, perSource, errs};
}

async function runTestScrape() {
  const q = ($('#ts-q').value || '').trim();
  if (!q) { toast('Enter a keyword to test', 'err'); $('#ts-q').focus(); return; }
  const pages = Math.max(1, Math.min(Number($('#ts-pages').value) || 1, 5));
  const sources = sourcesFor(_ts.source);
  const el = grid();
  status().textContent = '';
  probed = true;
  return pending($('#ts-run'), `▶ searching ${sources.join(' + ')}…`, () =>
    uiLoad(el, ({signal}) => probe(q, pages, sources, signal), {
      skeleton: 'card',
      count: 4,
      timeoutMs: PROBE_TIMEOUT_MS,
      render: renderTestScrape,
      isEmpty: (data) => !data.items.length,
      empty: emptyState(q, sources),
      errorMessage: (err) => `Couldn't live-search ${sources.map(s => SOURCE_NAMES[s] || s).join(' + ')} for “${q}”. `
        + (err.status ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
                      : err.message && err.message !== 'aborted' ? err.message : `The sites didn't answer in ${Math.round(PROBE_TIMEOUT_MS / 1000)} s.`),
    }));
}

// ───────── states ─────────

function emptyState(q, sources) {
  const state = {glyph: '◌', title: `No listings for “${q}”`, body: `${sources.map(s => SOURCE_NAMES[s] || s).join(' + ')} returned nothing for that keyword.`};
  if (_ts.source !== 'both') {
    state.cta = {label: 'Search all sources', onClick: () => { setSource('both'); runTestScrape(); }};
  }
  return state;
}

/** Idle prompt — the tab's resting state before the first probe. Nothing is fetched here. */
function renderIdle() {
  const el = grid();
  if (!el || probed) return;
  renderEmpty(el, {
    glyph: '⌕',
    title: 'Test a keyword',
    body: 'Pick a source, type a keyword and hit ▶ run test scrape. Nothing is cached or written.',
    cta: {label: 'Focus keyword', onClick: () => $('#ts-q')?.focus()},
  });
  el.dataset.state = 'idle';
}

function renderTestScrape({q, items, perSource}) {
  // Keyword-in-title check: crude singular stem, same idea as the API's
  // _singularize_term. Flags off-keyword cards rather than hiding them —
  // the noise level IS the signal this tab exists to measure.
  const stem = q.toLowerCase().replace(/s$/, '');
  let offKeyword = 0;
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const match = (it.title || '').toLowerCase().includes(stem);
    if (!match) offKeyword++;
    frag.appendChild(renderTestScrapeCard(it, match));
  }
  const bits = [`${items.length} listings (${perSource.join(' · ')})`,
                `${items.length - offKeyword} title-match “${q}”`];
  if (offKeyword) bits.push(`${offKeyword} ⚠ off-keyword`);
  status().textContent = bits.join(' · ');
  return frag;
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

// ───────── mount / activation ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading" (same markup UI.load paints during a probe).
  // No read happens until the operator runs one, so the twin gives way to the idle prompt right away.
  renderIdle();

  $$('#ts-source .seg-btn').forEach(btn => btn.addEventListener('click', () => setSource(btn.dataset.value)));
  $('#ts-run')?.addEventListener('click', runTestScrape);
  $('#ts-q')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runTestScrape(); }
  });
}

/** Tab activation (and popstate via the shell): focus the keyword box. Never starts a scrape. */
export function load() {
  renderIdle();
  $('#ts-q')?.focus();
}
