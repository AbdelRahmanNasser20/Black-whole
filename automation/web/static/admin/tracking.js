// static/admin/tracking.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, toast, apiFetch, esc, _fmtRemaining} from './shared.js';

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

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
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
}

export async function load() { return loadTracking(); }
