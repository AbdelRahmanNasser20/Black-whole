// static/admin/listings_db.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, escapeHtml, escapeAttr, _ageInDays, _fmtAge, queueRuns} from './shared.js';

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

// Debounced search
let _ldbSearchTimer;

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
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
}

export async function load() { return loadListingsDb(); }
