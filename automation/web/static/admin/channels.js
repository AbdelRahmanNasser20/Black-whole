// static/admin/channels.js — Channels tab (12): where every lot is, on every sales channel.
// Multichannel master plan, Phase 1.6. One read site (GET /api/channels → switches + queue + matrix,
// UI.load: skeleton twins shipped in index.html → ready | empty | error, keepOld on refresh / after a
// mutation) and three mutations, each through UI.pending on the clicked control:
//   · switches   → PATCH /api/channels/switches {key: 0|1}   (master + one per channel; server rejects
//                  anything that is not a channel* / browser_channel* key)
//   · queue      → POST  /api/channels/queue/{id}/approve | /reject
//   · Sync now   → POST  /api/channels/sync (same pass the background loop runs; toasts its counts)
//
// What this UI deliberately does NOT do:
//   · it never flips `channel_fb_marketplace_enabled` on its own — the red "OFF — waiting for
//     operator" pill stays until the operator clicks that one switch (the family account is already
//     shadowbanned; Renew/Relist are the spam triggers — decision D2);
//   · it never posts anywhere. Approve only moves a row to `queued`; the sync loop does the post,
//     and only when the channel switch is on (a 409 comes back otherwise — shown as a toast).
import {$, $$, toast, escapeHtml, escapeAttr} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const CHANNEL_LABELS = {
  site: 'Site', fb_catalog: 'FB Catalog', google: 'Google', ebay: 'eBay',
  fb_marketplace: 'FB Marketplace', craigslist: 'Craigslist',
};
const STATE_LABELS = {
  off: 'off', queued: 'queued', pending_approval: 'awaiting approval', live: 'live',
  delisted: 'delisted', error: 'error',
};
// One CSS modifier per known state (channels.css). An unknown state paints as `off` rather than
// minting a class name from server data.
const STATE_CLASS = {
  off: 'ch-cell--off', queued: 'ch-cell--queued', pending_approval: 'ch-cell--pending_approval',
  live: 'ch-cell--live', delisted: 'ch-cell--delisted', error: 'ch-cell--error',
};

const queueEl = () => $('#ch-queue');
const matrixEl = () => $('#ch-matrix');

let last = null;          // the last GET /api/channels body (switches + queue + matrix + channels)

function label(c) { return CHANNEL_LABELS[c] || c; }
function when(ts) { return ts ? String(ts).replace('T', ' ').slice(0, 16) : ''; }
function since(ts) {
  if (!ts) return '';
  const ms = Date.now() - new Date(ts).getTime();
  if (!isFinite(ms) || ms < 0) return when(ts);
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} min`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h`;
  return `${Math.round(h / 24)} d`;
}

// ───────── switches ─────────
// The inputs are rendered server-side (index.html, one per automation.channels.CHANNELS) so a channel
// can never be missing from the admin; this only paints their state and the FB Marketplace pill.

function paintSwitches(switches) {
  const master = !!Number(switches.channels_master_enabled ?? 0);
  $$('#ch-switches input[data-key]').forEach(input => {
    const key = input.dataset.key;
    const on = !!Number(switches[key] ?? 0);
    input.checked = on;
    const wrap = input.closest('.ch-switch');
    if (!wrap) return;
    wrap.classList.toggle('is-on', on);
    wrap.classList.toggle('is-masked', key !== 'channels_master_enabled' && !master);
    const state = wrap.querySelector('.ch-switch-state');
    if (state) state.textContent = on ? 'ON' : 'OFF';
  });
  const pill = $('#ch-pill-fb_marketplace');
  if (pill) pill.hidden = !!Number(switches.channel_fb_marketplace_enabled ?? 0);
}

async function flipSwitch(input) {
  const key = input.dataset.key;
  const value = input.checked ? 1 : 0;
  const wrap = input.closest('.ch-switch');
  wrap?.classList.add('is-pending');
  input.disabled = true;
  try {
    const switches = await api('/api/channels/switches', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value}),
    });
    if (last) last.switches = switches;
    paintSwitches(switches);
    toast(`${wrap?.querySelector('.ch-switch-label')?.textContent || key} → ${value ? 'ON' : 'OFF'}`, 'ok');
  } catch (err) {
    input.checked = !input.checked;          // roll the control back to what the server still has
    toast('Switch failed: ' + (err.message || err), 'err');
  } finally {
    input.disabled = false;
    wrap?.classList.remove('is-pending');
  }
}

// ───────── approval queue ─────────

function queueRow(q) {
  const lot = `<a href="/listings/${encodeURIComponent(q.lot_id)}" target="_blank" rel="noopener">${escapeHtml(q.title || q.lot_id)}</a>
    <div class="ch-tiny">${escapeHtml(q.lot_id)} · ${escapeHtml(q.status || '')} · qty ${q.quantity_remaining ?? '?'}</div>`;
  return `
    <tr data-id="${escapeAttr(q.id)}">
      <td class="ch-lot">${lot}</td>
      <td><span class="badge ch-chan">${escapeHtml(label(q.channel))}</span></td>
      <td class="ch-tiny" title="${escapeAttr(when(q.updated_at))}">${escapeHtml(since(q.updated_at))}</td>
      <td class="ch-actions">
        <button type="button" class="btn btn-small btn-primary" data-approve="${escapeAttr(q.id)}">Approve</button>
        <button type="button" class="btn btn-small btn-ghost ch-danger" data-reject="${escapeAttr(q.id)}">Reject</button>
      </td>
    </tr>`;
}

function renderQueue(items) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <table class="table ch-queue-table">
      <thead><tr><th>LOT</th><th>CHANNEL</th><th>WAITING</th><th></th></tr></thead>
      <tbody>${items.map(queueRow).join('')}</tbody>
    </table>`;
  wrap.querySelectorAll('[data-approve]').forEach(b => b.addEventListener('click', (e) => decide(e.currentTarget, 'approve')));
  wrap.querySelectorAll('[data-reject]').forEach(b => b.addEventListener('click', (e) => decide(e.currentTarget, 'reject')));
  return wrap.firstElementChild;
}

function decide(btn, verb) {
  const id = btn.dataset.approve || btn.dataset.reject;
  if (verb === 'reject' && !confirm(`Reject queue item #${id}? The lot stays on every other channel.`)) return;
  return pending(btn, '…', async () => {
    try {
      await api(`/api/channels/queue/${encodeURIComponent(id)}/${verb}`, {method: 'POST'});
      toast(verb === 'approve'
        ? `#${id} approved — the sync loop lists it on its next pass.`
        : `#${id} rejected.`, 'ok');
      await loadAll({keepOld: true});
    } catch (err) {
      const why = err.status === 409 && /disabled/.test(err.message || '')
        ? 'channel is switched off — flip it on first (FB Marketplace: OFF — waiting for operator).'
        : (err.message || err);
      toast(`${verb === 'approve' ? 'Approve' : 'Reject'} failed: ${why}`, 'err');
    }
  });
}

// ───────── matrix ─────────

function cell(c, info) {
  const state = (info && info.state) || 'off';
  const text = STATE_LABELS[state] || state;
  const title = info?.last_error ? ` title="${escapeAttr(info.last_error)}"` : '';
  const inner = state === 'live' && info?.url
    ? `<a href="${escapeAttr(info.url)}" target="_blank" rel="noopener">${escapeHtml(text)} ↗</a>`
    : escapeHtml(text);
  return `<td class="ch-cell ${STATE_CLASS[state] || STATE_CLASS.off}" data-channel="${escapeAttr(c)}"${title}><span class="ch-state">${inner}</span></td>`;
}

function matrixRow(lot, channels) {
  return `
    <tr data-lot="${escapeAttr(lot.lot_id)}">
      <td class="ch-lot">
        <a href="/listings/${encodeURIComponent(lot.lot_id)}" target="_blank" rel="noopener">${escapeHtml(lot.title || lot.lot_id)}</a>
        <div class="ch-tiny">${escapeHtml(lot.lot_id)} · ${escapeHtml(lot.status || '')} · qty ${lot.qty ?? '?'}</div>
      </td>
      ${channels.map(c => cell(c, lot.channels?.[c])).join('')}
    </tr>`;
}

function renderMatrix(body) {
  const channels = body.channels?.all || Object.keys(body.matrix?.[0]?.channels || {});
  return `
    <table class="table ch-matrix-table">
      <thead><tr><th>LOT</th>${channels.map(c => `<th>${escapeHtml(label(c)).toUpperCase()}</th>`).join('')}</tr></thead>
      <tbody>${(body.matrix || []).map(l => matrixRow(l, channels)).join('')}</tbody>
    </table>`;
}

// ───────── load ─────────
// One GET feeds three regions. The queue and the matrix are separate UI.load targets so each gets its
// own empty / error state; the switches are painted from the same body.

async function loadAll({keepOld = false} = {}) {
  const q = queueEl(), m = matrixEl();
  if (!q || !m) return;
  let body = null;
  const fetchOnce = async ({signal}) => {
    if (!body) body = await api('/api/channels', {signal});
    return body;
  };
  const queueP = uiLoad(q, async (o) => (await fetchOnce(o)).queue || [], {
    skeleton: 'row', count: 3, keepOld,
    render: renderQueue,
    empty: {glyph: '◌', title: 'Nothing waiting for you', body: 'FB Marketplace (re)lists park here until you approve them.'},
    errorMessage: (err) => "Couldn't load the queue. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
  const matrixP = uiLoad(m, fetchOnce, {
    skeleton: 'row', count: 6, keepOld,
    isEmpty: (b) => !(b.matrix || []).length,
    render: renderMatrix,
    empty: {glyph: '◌', title: 'No lots in inventory', body: 'Add a lot (Launcher or /list-lot) and it shows up here on every channel.',
            cta: {label: 'Open Inventory', href: '?tab=inventory'}},
    errorMessage: (err) => "Couldn't load the channel matrix. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
  await Promise.all([queueP, matrixP]);
  if (body) {
    last = body;
    paintSwitches(body.switches || {});
    const n = $('#ch-queue-count');
    if (n) n.textContent = (body.queue || []).length ? `${body.queue.length} waiting` : '';
  }
}

async function syncNow() {
  try {
    const r = await api('/api/channels/sync', {method: 'POST'});
    const skipped = Object.entries(r.skipped || {}).map(([k, v]) => `${k} ${v}`).join(', ');
    toast(`Sync: ${r.planned ?? 0} planned · ${r.applied ?? 0} applied · ${r.errors ?? 0} errors${skipped ? ' · skipped ' + skipped : ''}`,
          (r.errors || 0) ? 'err' : 'ok', 7000);
    await loadAll({keepOld: true});
  } catch (err) {
    toast('Sync failed: ' + (err.message || err), 'err');
  }
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twins inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is loading — drop the state so a smoke on another tab is not blocked on a hidden pane.
  for (const el of [queueEl(), matrixEl()]) {
    const pane = el?.closest('[data-pane]');
    if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }
  }
  $$('#ch-switches input[data-key]').forEach(input => input.addEventListener('change', () => flipSwitch(input)));
  $('#ch-sync')?.addEventListener('click', (e) => pending(e.currentTarget, 'syncing…', syncNow));
  $('#ch-refresh')?.addEventListener('click', (e) => pending(e.currentTarget, '↻ refreshing…', () => loadAll({keepOld: true})));
}

/** Tab activation (and popstate via the shell): fetch switches + queue + matrix. */
export async function load() {
  const m = matrixEl();
  return loadAll({keepOld: !!m && m.dataset.state === 'ready'});
}
