// static/admin/inventory.js — Inventory tab (plan §10 E-inventory).
// Reads go through UI.load (skeleton → ready | empty | error, keepOld on refresh), mutations through UI.pending,
// inline cell edits mark their <tr> is-pending. Filter state (`status`, `q`) lives in the URL via shell.js.
// `q` is a client-side filter over the rows already fetched — /api/inventory has no search parameter.
// The private storage note on a lot (facility address + gate code) is never read from a row here and never rendered.
import {$, $$, toast, escapeHtml, escapeAttr} from './shared.js';
import {load as uiLoad, pending, api, renderEmpty} from '../ui/state.js';

// URL params — same semantics as shell.js getParams()/setParams() (this tab owns `status` and `q`, the shell
// owns `tab`). Not imported from shell.js on purpose: index.html loads shell as `shell.js?v=…`, so a tab that
// imports `./shell.js` pulls in a SECOND shell instance that boots every tab mid-evaluation (TDZ crash).
function getParams() {
  const out = {};
  for (const [k, v] of new URLSearchParams(location.search)) out[k] = v;
  return out;
}
function setParams(patch, {replace = true} = {}) {
  const sp = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === null || v === undefined || v === '') sp.delete(k);
    else sp.set(k, String(v));
  }
  const qs = sp.toString();
  const url = location.pathname + (qs ? '?' + qs : '') + location.hash;
  const cur = location.pathname + location.search + location.hash;
  if (url !== cur) history[replace ? 'replaceState' : 'pushState'](history.state, '', url);
  return getParams();
}

const STATUSES = ['draft', 'listed', 'hidden', 'sold_out', 'lost_sold_out', 'owned', 'won_pickup', 'active_bid', 'lost'];
const PLATFORM_LABELS = {
  facebook: 'Facebook Marketplace',
  ebay: 'eBay',
  fb_business: 'Facebook Business post',
  ad: 'Ad',
};
const Q_DEBOUNCE_MS = 150;

let items = [];                         // last server payload (already status-filtered server-side)
const filter = {status: '', q: ''};     // mirrors ?status= and ?q=

const wrap = () => $('#inv-wrap');

// ───────── URL ↔ controls ─────────

function statusValues() { return $$('#inv-status-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const p = getParams();
  filter.status = statusValues().includes(p.status || '') ? (p.status || '') : '';
  filter.q = (p.q || '').trim();
  syncControls();
}

function syncControls() {
  $$('#inv-status-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.status;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
  const q = $('#inv-q');
  if (q && q.value !== filter.q) q.value = filter.q;
}

// ───────── data ─────────

function matches(item, q) {
  if (!q) return true;
  const hay = [item.lot_id, item.title, item.subtitle, item.city, item.state, item.zip_code, item.chair_type, item.status]
    .filter(Boolean).join(' ').toLowerCase();
  return q.toLowerCase().split(/\s+/).every(w => hay.includes(w));
}

function visible() { return items.filter(it => matches(it, filter.q)); }

function emptyArgs() {
  if (filter.q) {
    return {glyph: '⌕', title: 'No lots match', body: `Nothing in this view matches “${filter.q}”.`,
            cta: {label: 'Clear search', onClick: () => setQuery('')}};
  }
  if (filter.status) {
    return {title: `No ${filter.status} lots`, body: 'Nothing in the ledger carries this status.',
            cta: {label: 'Show all', onClick: () => setStatus('')}};
  }
  return {title: 'No lots in the ledger', body: 'Import the listings folder as draft rows, or add one by hand.',
          cta: {label: 'Backfill from folders', onClick: () => $('#inv-backfill')?.click()}};
}

async function loadInventory({keepOld = false} = {}) {
  const el = wrap();
  if (!el) return undefined;
  const qs = new URLSearchParams({with_stats: '1'});
  if (filter.status) qs.set('status', filter.status);
  return uiLoad(el, ({signal}) => api('/api/inventory?' + qs.toString(), {signal}), {
    skeleton: 'row', count: 10, keepOld,
    isEmpty(data) {
      items = data.items || [];
      renderStats(data.stats || {lots: 0, chairs: 0, cities: 0});
      renderCount();
      return !visible().length;
    },
    empty: emptyArgs(),
    render: () => tableHtml(visible()),
    errorMessage: (err) => err.status ? `Couldn't load the ledger. The server said ${err.status}.`
                                      : "Couldn't load the ledger. The database didn't answer in 15 s.",
  });
}

/** Re-render from the rows already fetched (q changed — no refetch). */
function applyFilter() {
  const el = wrap();
  if (!el || !items.length && el.dataset.state !== 'ready' && el.dataset.state !== 'empty') return;
  renderCount();
  const rows = visible();
  if (!rows.length) { renderEmpty(el, emptyArgs()); return; }
  el.innerHTML = tableHtml(rows);
  el.dataset.state = 'ready';
}

function setStatus(value) {
  filter.status = value || '';
  setParams({status: filter.status || null});
  syncControls();
  loadInventory({keepOld: true});
}

function setQuery(value) {
  filter.q = (value || '').trim();
  setParams({q: filter.q || null});
  syncControls();
  applyFilter();
}

// ───────── render ─────────

function renderStats(stats) {
  const el = $('#inv-stats');
  if (!el) return;
  el.removeAttribute('aria-hidden');
  el.innerHTML = `
    <div class="strip-cell"><div class="strip-num">${Number(stats.lots || 0).toLocaleString()}</div><div class="strip-lab">ACTIVE LOTS</div></div>
    <div class="strip-cell"><div class="strip-num">${Number(stats.chairs || 0).toLocaleString()}</div><div class="strip-lab">CHAIRS LEFT</div></div>
    <div class="strip-cell"><div class="strip-num">${Number(stats.cities || 0).toLocaleString()}</div><div class="strip-lab">CITIES</div></div>
  `;
}

function renderCount() {
  const el = $('#inv-count');
  if (!el) return;
  const n = visible().length;
  el.innerHTML = filter.q && n !== items.length
    ? `<strong>${n}</strong> of ${items.length} lots`
    : `<strong>${items.length}</strong> lot${items.length === 1 ? '' : 's'}`;
}

function tableHtml(rows) {
  return `<table class="table" id="inv-table">
    <thead><tr>
      <th>LOT</th><th>HERO</th><th>TITLE</th><th>QTY</th><th>PRICE</th><th>STATUS</th>
      <th>FB</th><th>EBAY</th><th>FB BIZ</th><th>AD</th><th>ACTIONS</th>
    </tr></thead>
    <tbody id="inv-tbody">${rows.map(rowHtml).join('')}</tbody>
  </table>`;
}

function rowHtml(item) {
  const loc = (item.city || '') + (item.state ? ', ' + item.state : '') + (item.zip_code ? ' ' + item.zip_code : '');
  const contact = [item.contact_name, item.contact_phone, item.contact_email].filter(Boolean).join(' · ');
  return `<tr data-lot-id="${escapeAttr(item.lot_id)}">
      <td class="mono tiny">${escapeHtml(item.lot_id)}</td>
      <td class="inv-hero">${item.hero_image_url
        ? `<img src="${escapeAttr(item.hero_image_url)}" alt="" loading="lazy" decoding="async" width="72" height="54">`
        : '<div class="inv-hero-fallback">◉</div>'}</td>
      <td>
        <div class="inv-title">${escapeHtml(item.title || '—')}</div>
        <div class="inv-sub mono tiny">${escapeHtml(loc)}${item.chair_type ? ' · ' + escapeHtml(item.chair_type) : ''}</div>
        ${contact ? `<div class="inv-sub mono tiny inv-contact">☎ ${escapeHtml(contact)}</div>` : ''}
        <div class="inv-sub inv-locs">
          <input class="inv-loc-input mono tiny" data-field="locations_text"
                 value="${escapeAttr(item.locations_text || '')}"
                 placeholder="extra locations — Baltimore, MD x1200; Orlando, FL"
                 title="Every place this lot sits. Blank = single location (uses the city above).">
        </div>
        <div class="inv-sub inv-extras">
          <button type="button" class="btn btn-small btn-ghost inv-acct" data-act="acct" title="Set the GovDeals login that owns this lot">
            🔐 ${item.govdeals_username
                  ? escapeHtml(item.govdeals_username) + (item.govdeals_password_set ? ' ✓' : ' (no pw)')
                  : 'set acct'}
          </button>
          ${item.buyer_cert_url
            ? `<a class="btn btn-small btn-ghost inv-cert-link" href="${escapeAttr(item.buyer_cert_url)}" target="_blank" rel="noopener" title="Open buyer certificate">📎 ${escapeHtml(item.buyer_cert_filename || 'cert')}</a>
               <button type="button" class="btn btn-small btn-ghost inv-danger" data-act="cert-clear" title="Remove certificate">✕</button>`
            : `<button type="button" class="btn btn-small btn-ghost" data-act="cert-attach" title="Attach winning-bid buyer certificate">📎 attach cert</button>`}
        </div>
      </td>
      <td>
        <input type="number" class="inv-qty" value="${item.quantity_remaining ?? ''}" min="0" data-field="quantity_remaining" aria-label="Quantity remaining">
        <div class="inv-sub mono tiny">of <input type="number" class="inv-qty-orig" value="${item.quantity_original ?? ''}" min="0" data-field="quantity_original" aria-label="Quantity original"></div>
      </td>
      <td>
        <input type="number" class="inv-price" value="${item.price_per_chair ?? ''}" min="0" step="1" data-field="price_per_chair" aria-label="Price per chair">
      </td>
      <td>
        <select class="inv-status" data-field="status" aria-label="Status">
          ${STATUSES.map(s => `<option value="${s}" ${s === item.status ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
      </td>
      <td>${platformCell(item, 'facebook')}</td>
      <td>${platformCell(item, 'ebay')}</td>
      <td>${platformCell(item, 'fb_business')}</td>
      <td>${platformCell(item, 'ad')}</td>
      <td class="inv-row-actions">
        <button type="button" class="btn btn-small btn-ghost" data-act="view">view</button>
        <button type="button" class="btn btn-small btn-ghost" data-act="republish">republish</button>
        <button type="button" class="btn btn-small btn-ghost" data-act="remove-everywhere"
                title="Mark as moved: fake sold-out on the site + business feed, Mark as sold on Marketplace">moved</button>
        <button type="button" class="btn btn-small btn-ghost inv-danger" data-act="delete">✕</button>
      </td>
    </tr>`;
}

function platformCell(item, platform) {
  const url = item[platform + '_url'];
  const ts = item[platform + '_published_at'];
  if (url) {
    return `
      <div class="plat-ok">
        <a href="${escapeAttr(url)}" target="_blank" rel="noopener">✓ link</a>
        <div class="mono tiny">${ts ? escapeHtml(ts.slice(0, 10)) : ''}</div>
        <button type="button" class="btn btn-small plat-clear" data-platform="${platform}" title="Clear URL">✕</button>
      </div>`;
  }
  return `<button type="button" class="btn btn-small plat-set" data-platform="${platform}">paste URL</button>`;
}

function flashRow(tr, kind) {
  tr.classList.remove('flash-ok', 'flash-err');
  void tr.offsetWidth;  // reflow so the animation restarts
  tr.classList.add(kind === 'ok' ? 'flash-ok' : 'flash-err');
}

/** Row-level pending: the <tr> dims + gets the progress glyph while `fn` runs (UI.pending is for buttons). */
async function pendingRow(tr, fn) {
  tr.classList.add('is-pending');
  tr.setAttribute('aria-busy', 'true');
  try { return await fn(); }
  finally { tr.classList.remove('is-pending'); tr.removeAttribute('aria-busy'); }
}

const lotUrl = (lotId, tail = '') => `/api/inventory/${encodeURIComponent(lotId)}${tail}`;
const jsonOpts = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

// ───────── inline edits (#24) ─────────

async function onFieldChange(e) {
  const input = e.target.closest('[data-field]');
  if (!input) return;
  const tr = input.closest('tr');
  const lotId = tr.dataset.lotId;
  const field = input.dataset.field;
  let value = input.value;
  if (field === 'quantity_remaining' || field === 'quantity_original' || field === 'price_per_chair') {
    value = value === '' ? null : Number(value);
  }
  await pendingRow(tr, async () => {
    try {
      const updated = await api(lotUrl(lotId), jsonOpts('PATCH', {[field]: value}));
      // If status auto-flipped (qty=0 → sold_out), sync the UI.
      const sel = tr.querySelector('select[data-field="status"]');
      if (sel && updated && updated.status) sel.value = updated.status;
      const it = items.find(x => x.lot_id === lotId);
      if (it) { it[field] = value; if (updated && updated.status) it.status = updated.status; }
      flashRow(tr, 'ok');
    } catch (err) {
      flashRow(tr, 'err');
      toast('Update failed: ' + (err.message || err), 'err');
    }
  });
}

// ───────── row actions ─────────

async function onRowClick(e) {
  const btn = e.target.closest('button');
  if (!btn || !wrap().contains(btn)) return;
  if (btn.classList.contains('plat-set')) return onPlatformSet(btn);
  if (btn.classList.contains('plat-clear')) return onPlatformClear(btn);
  if (btn.dataset.act) return onAction(btn);
}

async function onAction(btn) {
  const tr = btn.closest('tr');
  const lotId = tr.dataset.lotId;
  const act = btn.dataset.act;
  if (act === 'view') {
    window.open(`/listings/${encodeURIComponent(lotId)}`, '_blank');
    return;
  }
  if (act === 'remove-everywhere') {
    if (!confirm(`Mark ${lotId} as MOVED everywhere?\n\nSite + business feed: fake sold-out (shows under ALREADY MOVED).\nMarketplace: Mark as sold on the family account.\n\nWatch the Launcher console.`)) return;
    await pending(btn, '…queuing', async () => {
      try {
        await api(`/api/lots/${encodeURIComponent(lotId)}/remove`, jsonOpts('POST', {}));
        toast(`${lotId} queued as moved — see Launcher tab.`, 'ok');
        setTimeout(() => loadInventory({keepOld: true}), 4000);
      } catch (err) {
        toast('Remove failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'delete') {
    if (!confirm(`Delete lot ${lotId} from the ledger? (Folder on disk is untouched.)`)) return;
    await pending(btn, '…deleting', async () => {
      try {
        await api(lotUrl(lotId), {method: 'DELETE'});
        toast(`Lot ${lotId} deleted from ledger.`, 'ok');
        loadInventory({keepOld: true});
      } catch (err) {
        toast('Delete failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'acct') {
    const item = items.find(x => x.lot_id === lotId);
    const curUser = item?.govdeals_username || '';
    const username = prompt(`GovDeals username/email for lot ${lotId}\n(leave blank to clear):`, curUser);
    if (username === null) return;
    let password = null;
    if (username.trim()) {
      // Don't prefill the password — we never return it from the API. An empty
      // submit keeps the existing password; "-" explicitly clears it.
      const pwPrompt = prompt(
        `GovDeals password for ${username.trim()}\n(empty = keep current, "-" = clear, anything else = replace):`, '');
      if (pwPrompt === null) return;
      password = pwPrompt;
    }
    const body = {govdeals_username: username.trim() || null};
    if (!username.trim()) body.govdeals_password = null;
    else if (password === '-') body.govdeals_password = null;
    else if (password !== '' && password !== null) body.govdeals_password = password;
    await pending(btn, '…saving', async () => {
      try {
        await api(lotUrl(lotId), jsonOpts('PATCH', body));
        toast(`Account saved for ${lotId}.`, 'ok');
        loadInventory({keepOld: true});
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
      await pending(btn, '…uploading', async () => {
        try {
          const fd = new FormData();
          fd.append('file', file);
          await api(lotUrl(lotId, '/buyer-cert'), {method: 'POST', body: fd});
          toast(`Certificate attached to ${lotId}.`, 'ok');
          loadInventory({keepOld: true});
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
    await pending(btn, '…', async () => {
      try {
        await api(lotUrl(lotId, '/buyer-cert'), {method: 'DELETE'});
        toast(`Certificate removed for ${lotId}.`, 'ok');
        loadInventory({keepOld: true});
      } catch (err) {
        toast('Remove failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }
  if (act === 'republish') {
    const item = items.find(x => x.lot_id === lotId);
    if (!item) return;
    const url = item.govdeals_url;
    if (!url) {
      toast('This row has no GovDeals URL — republish only works for scraped lots.', 'err');
      return;
    }
    if (!confirm(`Republish ${lotId}? This ignores the dedup check and spends API tokens.`)) return;
    await pending(btn, '…queuing', async () => {
      try {
        await api('/api/runs/start', jsonOpts('POST', {url, force_republish: true}));
        toast(`Lot ${lotId} queued. Switch to Launcher to watch.`, 'ok');
      } catch (err) {
        toast('Republish failed: ' + (err.message || err), 'err');
      }
    });
  }
}

async function onPlatformSet(btn) {
  const lotId = btn.closest('tr').dataset.lotId;
  const platform = btn.dataset.platform;
  const label = PLATFORM_LABELS[platform] || platform.toUpperCase();
  const url = prompt(`Paste the ${label} URL for lot ${lotId}:`);
  if (!url) return;
  await pending(btn, '…saving', async () => {
    try {
      await api(lotUrl(lotId, '/platform'), jsonOpts('POST', {platform, url: url.trim()}));
      toast(`${label} URL saved for ${lotId}.`, 'ok');
      loadInventory({keepOld: true});
    } catch (err) {
      toast(`Save failed: ${err.message || err}`, 'err');
    }
  });
}

async function onPlatformClear(btn) {
  const lotId = btn.closest('tr').dataset.lotId;
  const platform = btn.dataset.platform;
  const label = PLATFORM_LABELS[platform] || platform.toUpperCase();
  if (!confirm(`Clear the ${label} URL for lot ${lotId}?`)) return;
  await pending(btn, '…', async () => {
    try {
      await api(lotUrl(lotId, '/platform'), jsonOpts('POST', {platform, url: null}));
      toast(`${label} URL cleared for ${lotId}.`, 'ok');
      loadInventory({keepOld: true});
    } catch (err) {
      toast(`Clear failed: ${err.message || err}`, 'err');
    }
  });
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is actually loading — drop the state so a smoke on another tab is not blocked on a
  // hidden pane. load() re-sets it when the tab opens.
  const el = wrap();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  // one delegated listener each — rows are re-rendered on every load
  wrap()?.addEventListener('change', onFieldChange);
  wrap()?.addEventListener('click', onRowClick);

  $('#inv-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ loading…', () => loadInventory({keepOld: true}));
  });

  $('#inv-backfill')?.addEventListener('click', async (e) => {
    if (!confirm('Walk the listings folder and import any missing rows as drafts?')) return;
    await pending(e.currentTarget, '…backfilling', async () => {
      try {
        const data = await api('/api/inventory/backfill', {method: 'POST'});
        toast(`Backfill done · +${data.counts.added} added · ${data.counts.updated} updated · ${data.counts.skipped} skipped`, 'ok');
        loadInventory({keepOld: true});
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
    await pending(btn, '…creating', async () => {
      try {
        await api('/api/inventory', jsonOpts('POST', payload));
        toast(`Listing ${payload.lot_id} created.`, 'ok');
        f.reset(); f.hidden = true;
        loadInventory({keepOld: true});
      } catch (err) {
        toast('Create failed: ' + (err.message || err), 'err');
      }
    });
  });

  // status filter → ?status= → refetch
  $$('#inv-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => setStatus(b.dataset.value)));

  // search → ?q= → client-side filter (debounced)
  let qTimer = null;
  $('#inv-q')?.addEventListener('input', (e) => {
    clearTimeout(qTimer);
    const v = e.target.value;
    qTimer = setTimeout(() => setQuery(v), Q_DEBOUNCE_MS);
  });
  $('#inv-q')?.addEventListener('search', (e) => { clearTimeout(qTimer); setQuery(e.target.value); });
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch — dimming old rows if we have any. */
export async function load() {
  readParams();
  const el = wrap();
  return loadInventory({keepOld: !!el && el.dataset.state === 'ready'});
}
