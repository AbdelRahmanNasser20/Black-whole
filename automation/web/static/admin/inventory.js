// static/admin/inventory.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, escapeHtml, escapeAttr} from './shared.js';

// ─────────────────────────── Inventory tab ───────────────────────────

var _invItems = [];
var _invStatusFilter = '';

async function loadInventory() {
  const tbody = $('#inv-tbody');
  tbody.innerHTML = '<tr><td colspan="11" class="drafts-empty">Loading…</td></tr>';
  try {
    const qs = new URLSearchParams({with_stats: '1'});
    if (_invStatusFilter) qs.set('status', _invStatusFilter);
    const res = await fetch('/api/inventory?' + qs.toString());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _invItems = data.items || [];
    renderInvStats(data.stats || {lots: 0, chairs: 0, cities: 0});
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
        ? `<img src="${item.hero_image_url}" alt="" loading="lazy" decoding="async" width="72" height="54">`
        : '<div class="inv-hero-fallback">◉</div>'}</td>
      <td>
        <div class="inv-title">${escapeHtml(item.title || '—')}</div>
        <div class="inv-sub mono tiny">${escapeHtml((item.city || '') + (item.state ? ', ' + item.state : '') + (item.zip_code ? ' ' + item.zip_code : ''))}${item.chair_type ? ' · ' + escapeHtml(item.chair_type) : ''}</div>
        ${(item.contact_email || item.contact_phone || item.contact_name) ? `
          <div class="inv-sub mono tiny inv-contact">☎ ${escapeHtml([item.contact_name, item.contact_phone, item.contact_email].filter(Boolean).join(' · '))}</div>
        ` : ''}
        <div class="inv-sub inv-locs">
          <input class="inv-loc-input mono tiny" data-field="locations_text"
                 value="${escapeAttr(item.locations_text || '')}"
                 placeholder="extra locations — Baltimore, MD x1200; Orlando, FL"
                 title="Every place this lot sits. Blank = single location (uses the city above).">
        </div>
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
          ${['draft','listed','hidden','sold_out','lost_sold_out','owned','won_pickup','active_bid','lost'].map(s =>
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
        <button class="btn btn-small btn-ghost" data-act="remove-everywhere"
                title="Mark as moved: fake sold-out on the site + business feed, Mark as sold on Marketplace">moved</button>
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
  if (act === 'remove-everywhere') {
    if (!confirm(`Mark ${lotId} as MOVED everywhere?\n\nSite + business feed: fake sold-out (shows under ALREADY MOVED).\nMarketplace: Mark as sold on the family account.\n\nWatch the Launcher console.`)) return;
    await withButtonLoading(btn, '…queuing', async () => {
      try {
        await apiFetch(`/api/lots/${encodeURIComponent(lotId)}/remove`, {method: 'POST',
          headers: {'content-type': 'application/json'}, body: '{}'});
        toast(`${lotId} queued as moved — see Launcher tab.`, 'ok');
        setTimeout(loadInventory, 4000);
      } catch (err) {
        toast('Remove failed: ' + (err.message || err), 'err');
      }
    });
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

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
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
    await withButtonLoading(btn, '…creating', async () => {
      try {
        await apiFetch('/api/inventory', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        toast(`Listing ${payload.lot_id} created.`, 'ok');
        f.reset(); f.hidden = true;
        loadInventory();
      } catch (err) {
        toast('Create failed: ' + (err.message || err), 'err');
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
}

export async function load() { return loadInventory(); }
