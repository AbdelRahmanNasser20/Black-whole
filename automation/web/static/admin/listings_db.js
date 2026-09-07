// static/admin/listings_db.js — Listings DB tab (plan §10 E-listings-db): states only, no redesign.
// The one read (/api/listings, #41) goes through UI.load (skeleton → ready | empty | error, keepOld on refresh
// and on filter changes so the old rows stay dimmed while re-querying); the two mutations (queue a lot, reload)
// and "Load more" go through UI.pending. `q`, `source`, `offset` live in the URL via shell.js-style params; the
// other filters (status, qty range, seen-within, sort, page size) stay in the form.
// auction_extractors/state/listings.db is read-only — this tab only ever GETs.
import {$, $$, toast, escapeHtml, escapeAttr, _ageInDays, _fmtAge, queueRuns, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const SOURCES = ['all', 'gd', 'ps', 'bs'];
const Q_DEBOUNCE_MS = 350;

const _ldb = {
  source: 'all',   // mirrors ?source=
  status: 'all',
  offset: 0,       // mirrors ?offset= — the offset of the last page fetched
  total: 0,
  limit: 50,
  shownFrom: 0,    // 1-based first row on screen (Load more appends, so this stays put)
  shownTo: 0,
};

const wrap = () => $('#ldb-wrap');

// ───────── URL ↔ controls ─────────

function readParams() {
  const p = getParams();
  _ldb.source = SOURCES.includes(p.source || '') ? p.source : 'all';
  const off = Number(p.offset);
  _ldb.offset = Number.isFinite(off) && off > 0 ? Math.floor(off) : 0;
  const q = $('#ldb-q');
  if (q && (p.q || '') !== q.value) q.value = p.q || '';
  syncControls();
}

function syncControls() {
  $$('#ldb-source .seg-btn').forEach(b => setActive(b, b.dataset.value === _ldb.source));
  $$('#ldb-status .seg-btn').forEach(b => setActive(b, b.dataset.value === _ldb.status));
}

function setActive(btn, on) {
  btn.classList.toggle('is-active', on);
  if (on) btn.setAttribute('aria-pressed', 'true'); else btn.removeAttribute('aria-pressed');
}

// ───────── data ─────────

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

function emptyArgs() {
  const q = $('#ldb-q').value.trim();
  return {
    glyph: '⌕',
    title: q ? `No rows match “${q}”` : 'No rows match these filters',
    body: 'Widen the quantity range, allow older rows under SEEN WITHIN, or clear the filters.',
    cta: {label: 'Clear filters', onClick: resetFilters},
  };
}

/** Fetch one page. `append` = Load more (rows are added under the existing ones); otherwise the page replaces. */
async function loadListingsDb({keepOld = false, append = false} = {}) {
  const el = wrap();
  if (!el) return undefined;
  _ldb.limit = Number($('#ldb-limit').value) || 50;
  const data = await uiLoad(el, ({signal}) => api('/api/listings?' + _ldbQuery().toString(), {signal}), {
    skeleton: 'row', count: 10, keepOld: keepOld || append,
    isEmpty: d => !append && !(d.items || []).length,
    empty: emptyArgs(),
    render(d) {
      const html = d.items.map(rowHtml).join('');
      if (append) { $('#ldb-tbody', el)?.insertAdjacentHTML('beforeend', html); return null; }
      return tableHtml(html);
    },
    errorMessage: (err) => err.status ? `Couldn't query listings.db. The server said ${err.status}.`
                         : err.name === 'AbortError' ? "Couldn't query listings.db. The server didn't answer in 15 s."
                                                     : "Couldn't query listings.db. The server didn't answer at all.",
    onError: () => { $('#ldb-status-bar').textContent = ''; $('#ldb-more').hidden = true; },
  });
  if (!data) return undefined;
  _ldb.total = data.total;
  const n = data.items.length;
  if (append) { _ldb.shownTo = Math.min(_ldb.offset + n, data.total); }
  else { _ldb.shownFrom = data.total === 0 ? 0 : _ldb.offset + 1; _ldb.shownTo = Math.min(_ldb.offset + n, data.total); }
  renderStatus();
  renderMore();
  return data;
}

function renderStatus() {
  const bar = $('#ldb-status-bar');
  if (!bar) return;
  bar.textContent = _ldb.total === 0
    ? 'No rows match these filters.'
    : `Showing ${_ldb.shownFrom.toLocaleString()}–${_ldb.shownTo.toLocaleString()} of ${_ldb.total.toLocaleString()} rows`;
}

function renderMore() {
  const btn = $('#ldb-more');
  if (!btn) return;
  const remaining = Math.max(0, _ldb.total - _ldb.shownTo);
  btn.hidden = !(remaining > 0);
  if (remaining > 0) btn.textContent = `Load more (${remaining.toLocaleString()} remaining)`;
}

// ───────── render ─────────

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

function tableHtml(rows) {
  return `<div class="table-wrap"><table class="table" id="ldb-table">
    <thead><tr>
      <th class="ldb-col-src">SRC</th>
      <th class="ldb-col-qty">QTY</th>
      <th class="ldb-col-title">TITLE</th>
      <th class="ldb-col-price">PRICE</th>
      <th class="ldb-col-loc">LOCATION</th>
      <th class="ldb-col-end">ENDS</th>
      <th class="ldb-col-seen">LAST SEEN</th>
      <th class="ldb-col-act"></th>
    </tr></thead>
    <tbody id="ldb-tbody">${rows}</tbody>
  </table></div>`;
}

function rowHtml(r) {
  const srcClass = ['gd', 'ps', 'bs'].includes(r.source) ? `src-${r.source}` : 'src-other';
  const qty = r.quantity == null ? '—' : r.quantity.toLocaleString();
  const title = r.title || '(untitled)';
  const endStr = _fmtEndDate(r);
  const isExpired = r.end_date && new Date(r.end_date) < new Date();
  return `<tr class="ldb-row" data-title="${escapeAttr(title)}">
      <td><span class="src-pill ${srcClass}">${escapeHtml(String(r.source || '').toUpperCase())}</span></td>
      <td class="ldb-qty num">${qty}</td>
      <td class="ldb-title">
        <div class="ldb-title-main">${escapeHtml(title)}</div>
        <div class="ldb-asset mono">${escapeHtml(r.asset_id)}${r.quantity_source ? ` · qty via <em>${escapeHtml(r.quantity_source)}</em>` : ''}${r.quantity_confidence ? ` <span class="ldb-conf">${escapeHtml(r.quantity_confidence)}</span>` : ''}</div>
      </td>
      <td class="ldb-price">${escapeHtml(r.price || '—')}</td>
      <td class="ldb-loc">${escapeHtml(r.location || '—')}</td>
      <td class="ldb-end ${isExpired ? 'expired' : ''}">${escapeHtml(endStr)}</td>
      <td class="ldb-seen">${escapeHtml(_fmtLastSeen(r.last_seen_at))}</td>
      <td class="ldb-act">
        <a href="${escapeAttr(r.link || '#')}" target="_blank" rel="noopener" class="btn btn-small" title="Open source listing">↗</a>
        ${r.source === 'gd' ? `<button type="button" class="btn btn-small btn-primary ldb-launch" data-url="${escapeAttr(r.link)}" title="Queue this lot for the pipeline">▶</button>` : ''}
      </td>
    </tr>`;
}

// ───────── actions ─────────

/** Delegated: rows are re-rendered on every load, so one listener on the wrap handles every ▶ button. */
async function onRowClick(e) {
  const btn = e.target.closest('.ldb-launch');
  if (!btn || !wrap().contains(btn)) return;
  const title = btn.closest('tr')?.dataset.title || btn.dataset.url;
  await pending(btn, '⏱', async () => {
    try {
      await queueRuns([btn.dataset.url]);
      toast(`Queued: ${title}`, 'ok');
      btn.__queued = true;
    } catch (err) {
      toast('Queue failed: ' + (err.message || err), 'err');
    }
  });
  if (btn.__queued) { btn.textContent = '✓'; btn.disabled = true; btn.classList.add('queued'); }
}

// Filter wiring — any change resets offset to 0 and re-queries with the old rows dimmed.
function _ldbReload() {
  _ldb.offset = 0;
  setParams({offset: null});
  return loadListingsDb({keepOld: true});
}

function setSource(value) {
  _ldb.source = SOURCES.includes(value) ? value : 'all';
  setParams({source: _ldb.source === 'all' ? null : _ldb.source});
  syncControls();
  _ldbReload();
}

function setQuery(value) {
  const q = (value || '').trim();
  setParams({q: q || null});
  _ldbReload();
}

function resetFilters() {
  _ldb.source = 'all'; _ldb.status = 'all'; _ldb.offset = 0;
  $('#ldb-q').value = '';
  $('#ldb-min-qty').value = '0';
  $('#ldb-max-qty').value = '99999';
  $('#ldb-seen').value = '7';
  $('#ldb-sort').value = 'qty_desc';
  $('#ldb-limit').value = '50';
  setParams({q: null, source: null, offset: null});
  syncControls();
  loadListingsDb({keepOld: true});
}

async function loadMore(btn) {
  if (_ldb.shownTo >= _ldb.total) return;
  const next = _ldb.offset + _ldb.limit;
  await pending(btn, 'Fetching…', async () => {
    const prev = _ldb.offset;
    _ldb.offset = next;
    const data = await loadListingsDb({append: true});
    if (data) setParams({offset: next}); else _ldb.offset = prev;
  });
  renderMore();   // pending() restores the button's old label on settle; re-render the countdown after it
}

// ───────── mount / load ─────────

let _ldbSearchTimer;
let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;

  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is actually loading — drop the state so a smoke on another tab is not blocked on a
  // hidden pane. load() re-sets it when the tab opens.
  const el = wrap();
  if (el && el.closest('[data-pane]')?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  el?.addEventListener('click', onRowClick);

  $$('#ldb-source .seg-btn').forEach(b => b.addEventListener('click', () => setSource(b.dataset.value)));

  $$('#ldb-status .seg-btn').forEach(b => b.addEventListener('click', () => {
    _ldb.status = b.dataset.value;
    syncControls();
    _ldbReload();
  }));

  $('#ldb-q')?.addEventListener('input', (e) => {
    clearTimeout(_ldbSearchTimer);
    const v = e.target.value;
    _ldbSearchTimer = setTimeout(() => setQuery(v), Q_DEBOUNCE_MS);
  });
  $('#ldb-q')?.addEventListener('search', (e) => { clearTimeout(_ldbSearchTimer); setQuery(e.target.value); });

  ['#ldb-min-qty', '#ldb-max-qty', '#ldb-seen', '#ldb-sort', '#ldb-limit']
    .forEach(sel => $(sel)?.addEventListener('change', _ldbReload));

  $('#ldb-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ reloading…', () => loadListingsDb({keepOld: true}));
  });

  $('#ldb-reset')?.addEventListener('click', resetFilters);

  $('#ldb-more')?.addEventListener('click', (e) => loadMore(e.currentTarget));
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch — dimming old rows if we have any. */
export async function load() {
  readParams();
  const el = wrap();
  return loadListingsDb({keepOld: !!el && el.dataset.state === 'ready'});
}
