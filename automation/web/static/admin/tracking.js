// static/admin/tracking.js — Tracking tab (Workstream E-tracking).
// Reads: GET /api/tracking → UI.load on #trk-list (skeleton 'row' twin shipped in index.html; refetches keepOld);
//        GET /api/tracking/{key}/history → UI.load inside the lot's drawer row (skeleton 'line' ×8).
// Mutations: POST /api/tracking, PATCH/DELETE /api/tracking/{key}, POST /api/tracking/sync → UI.pending.
// URL state: `?label=` (the list filter) through shell.js getParams/setParams — the shell owns only `tab`.
// The server polls on its own scheduler tick (_tracking_loop stays in the web process); this tab is read-mostly.
import {$, esc, _fmtRemaining} from './shared.js';
import {api, toast, load as uiLoad, pending} from '../ui/state.js';
// shell.js owns URL state (E1 contract: tabs use its getParams/setParams for their own keys; the shell owns `tab`).
// tests/web/test_admin_modules.py forbids a static import of ./shell.js (its regex predates E1 — the rule is that tabs
// never import *tabs*), so the same module instance is pulled through a dynamic import; swap for a static import
// once that regex admits shell.js.
const shell = () => import('./shell.js');

const trk = {
  items: [], labels: [], labelFilter: '',
  open: new Set(),        // "asset/account" keys with the history drawer open
  history: {},            // key -> /history payload (cached until refresh)
  loadedOnce: false,      // first load swaps the shipped skeleton; later loads keep the old rows dimmed
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

// "Star one on Auctions" → the Auctions tab through the rail (shell.js handles URL + activateTab).
function goToAuctions() {
  const link = $('.rail-tab[data-tab="auctions"]');
  if (link) link.click(); else location.search = '?tab=auctions';
}

async function setLabelFilter(label) {
  trk.labelFilter = label || '';
  const {setParams} = await shell();
  setParams({label: trk.labelFilter});
  loadTracking();
}

// ───────── list ─────────

async function loadTracking() {
  const list = $('#trk-list');
  if (!list) return;
  const {getParams} = await shell();
  trk.labelFilter = getParams().label || '';
  const url = '/api/tracking' + (trk.labelFilter ? `?label=${encodeURIComponent(trk.labelFilter)}` : '');
  const filter = trk.labelFilter;
  const body = await uiLoad(list, async ({signal}) => {
    const b = await api(url, {signal});
    trk.items = b.items || [];
    trk.labels = b.labels || [];
    renderTrackingLabels();
    return b;
  }, {
    skeleton: 'row',
    count: 6,
    keepOld: trk.loadedOnce,
    isEmpty: (b) => !(b.items || []).length,
    render: renderTrackingTable,
    empty: filter ? {
      glyph: '◌',
      title: `No lots in "${filter}"`,
      body: 'This list is empty or every lot on it was removed.',
      cta: {label: 'Show all lists', onClick: () => setLabelFilter('')},
    } : {
      glyph: '◌',
      title: 'Nothing tracked yet',
      body: 'Paste a GovDeals lot URL above, or star a lot on Auctions — favorites join automatically.',
      cta: {label: 'Go to Auctions', onClick: goToAuctions},
    },
    errorMessage: (err) => "Couldn't load the tracking list. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
  if (body !== undefined) trk.loadedOnce = true;
  return body;
}

function renderTrackingLabels() {
  const seg = $('#trk-label-filter');
  const opts = $('#trk-label-options');
  if (seg) {
    seg.innerHTML = [`<button type="button" class="seg-btn ${trk.labelFilter ? '' : 'is-active'}" data-value="">all</button>`]
      .concat(trk.labels.map(l =>
        `<button type="button" class="seg-btn ${trk.labelFilter === l.label ? 'is-active' : ''}" data-value="${esc(l.label)}">${esc(l.label)} <span class="trk-seg-count">${l.open}/${l.n}</span></button>`))
      .join('');
  }
  if (opts) opts.innerHTML = trk.labels.map(l => `<option value="${esc(l.label)}">`).join('');
  const open = trk.items.filter(r => !r.closed_at).length;
  const closed = trk.items.length - open;
  const sold = trk.items.filter(r => r.closed_at && r.final_bid != null);
  const total = sold.reduce((a, r) => a + Number(r.final_bid || 0), 0);
  const summary = $('#trk-summary');
  if (summary) {
    summary.innerHTML = `<strong>${open}</strong> open · <strong>${closed}</strong> closed`
      + (sold.length ? ` · ${_trkMoney(total)} realized across ${sold.length}` : '');
  }
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

function _trkRow(r) {
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
        <button type="button" class="btn btn-small trk-hist" data-key="${key}" aria-expanded="${isOpen}">${isOpen ? '▾' : '▸'} history</button>
        <button type="button" class="btn btn-small trk-del" data-key="${key}" title="Stop tracking (keeps observations)">✕</button>
      </td>
    </tr>
    ${isOpen ? `<tr class="trk-drawer" data-key="${key}"><td colspan="8"><div class="trk-drawer-body" data-key="${key}" data-state="${trk.history[key] ? 'ready' : 'loading'}">${trk.history[key] ? _trkDrawer(trk.history[key]) : ''}</div></td></tr>` : ''}`;
}

function renderTrackingTable(body) {
  return `
    <table class="table">
      <thead>
        <tr>
          <th>LIST</th><th>LOT</th><th>STATUS</th><th class="num">BIDS</th>
          <th class="num">PRICE</th><th>LEADER / WINNER</th><th>LAST POLL</th><th></th>
        </tr>
      </thead>
      <tbody id="trk-rows">${(body.items || []).map(_trkRow).join('')}</tbody>
    </table>`;
}

// Re-paint the rows from what we already have (drawer toggles) — no fetch, so no UI.load.
function repaintRows() {
  const list = $('#trk-list');
  if (!list || list.dataset.state !== 'ready') return;
  list.innerHTML = renderTrackingTable({items: trk.items});
  trk.open.forEach(key => { if (!trk.history[key]) loadHistory(key); });
}

// ───────── history drawer ─────────

function loadHistory(key) {
  const el = $(`#trk-list .trk-drawer-body[data-key="${key}"]`);
  if (!el) return;
  return uiLoad(el, async ({signal}) => {
    const h = await api(`/api/tracking/${key}/history`, {signal});
    trk.history[key] = h;      // cache on the fetcher so a Retry fills it too
    return h;
  }, {
    skeleton: 'line',
    count: 8,
    isEmpty: (h) => !(h.observations || []).length,
    empty: {
      glyph: '◌',
      title: 'No observations yet',
      body: 'The first poll lands within a minute of adding a lot, or hit ⟳ poll now.',
    },
    render: _trkDrawer,
    errorMessage: (err) => `Couldn't load the bid history for ${key}. ` + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
}

function _trkDrawer(h) {
  const obs = h.observations || [];
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

// ───────── mutations (all through UI.pending) ─────────

async function addLot() {
  const ref = $('#trk-ref').value.trim();
  const label = $('#trk-label').value.trim() || 'default';
  if (!ref) { toast('paste a GovDeals lot URL first', 'err'); return; }
  await pending($('#trk-add'), 'adding…', async () => {
    try {
      const row = await api('/api/tracking', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ref, label}),
      });
      toast(`tracking ${row.title || _trkKey(row)} under "${row.label}"`);
      $('#trk-ref').value = '';
      await loadTracking();
    } catch (err) { toast(`add failed: ${err.message || err}`, 'err'); }
  });
}

async function removeLot(btn, key) {
  await pending(btn, '…', async () => {
    try {
      await api(`/api/tracking/${key}`, {method: 'DELETE'});
      toast(`stopped tracking ${key}`);
      trk.open.delete(key);
      await loadTracking();
    } catch (err) { toast(`remove failed: ${err.message || err}`, 'err'); }
  });
}

async function renameList(inp) {
  const key = inp.dataset.key;
  const r = trk.items.find(x => _trkKey(x) === key);
  const label = inp.value.trim() || 'default';
  if (!r || r.label === label) return;
  const tr = inp.closest('tr');
  tr?.classList.add('is-pending');
  try {
    await api(`/api/tracking/${key}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label}),
    });
    toast(`${key} → ${label}`);
    await loadTracking();
  } catch (err) { toast(`rename failed: ${err.message || err}`, 'err'); }
  finally { tr?.classList.remove('is-pending'); }
}

async function pollNow() {
  await pending($('#trk-sync'), '⟳ polling…', async () => {
    try {
      const rep = await api('/api/tracking/sync', {method: 'POST'});
      toast(`polled ${rep.polled} · ${rep.recorded} changes · ${rep.closed} closed${rep.errors ? ` · ${rep.errors} errors` : ''}`);
      trk.history = {};
      await loadTracking();
    } catch (err) { toast(`poll failed: ${err.message || err}`, 'err'); }
  });
}

// ───────── mount ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is actually loading — drop the state so a smoke on another tab is not blocked on a
  // hidden pane. load() re-sets it when the tab opens.
  const list = $('#trk-list');
  const pane = list?.closest('[data-pane]');
  if (list && pane?.hidden) { delete list.dataset.state; list.removeAttribute('aria-busy'); }

  $('#trk-add')?.addEventListener('click', addLot);
  $('#trk-ref')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addLot(); } });
  $('#trk-refresh')?.addEventListener('click', () => { trk.history = {}; loadTracking(); });
  $('#trk-sync')?.addEventListener('click', pollNow);

  // Label filter → URL (?label=) → reload.
  $('#trk-label-filter')?.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    setLabelFilter(b.dataset.value);
  });

  // Row actions are delegated so a re-render never has to re-bind.
  list?.addEventListener('click', (e) => {
    const hist = e.target.closest('.trk-hist');
    if (hist) {
      const key = hist.dataset.key;
      if (trk.open.has(key)) trk.open.delete(key); else trk.open.add(key);
      repaintRows();
      return;
    }
    const del = e.target.closest('.trk-del');
    if (del) removeLot(del, del.dataset.key);
  });
  list?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.classList.contains('trk-label-cell')) { e.preventDefault(); e.target.blur(); }
  });
  list?.addEventListener('focusout', (e) => {
    if (e.target.classList.contains('trk-label-cell')) renameList(e.target);
  });
}

export async function load() { return loadTracking(); }
