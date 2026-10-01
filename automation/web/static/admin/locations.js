// static/admin/locations.js — Locations tab (13): where every held lot physically sits.
// Read: GET /api/locations (storage notes come back with gate codes masked) through UI.load.
// Edit: GET /api/locations/{lot}?raw=1 fills the textarea, PUT saves it — both through UI.pending.
// The filter lives in the URL (?loc=missing). Storage notes are PRIVATE: this tab is the only web surface for them.
import {$, $$, toast, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const filter = {missing: false};          // mirrors ?loc=
const list = () => $('#loc-list');
const enc = (id) => encodeURIComponent(id).replace(/%2F/g, '/');

// ───────── URL ↔ controls ─────────

function readParams() {
  filter.missing = getParams().loc === 'missing';
  syncControls();
}

function syncControls() {
  $$('#loc-filter .seg-btn').forEach(b => {
    const on = (b.dataset.value === 'missing') === filter.missing;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setFilter(value) {
  filter.missing = value === 'missing';
  setParams({loc: filter.missing ? 'missing' : ''});
  syncControls();
  loadLocations({keepOld: true});
}

// ───────── data ─────────

function renderCount(items) {
  const el = $('#loc-count');
  if (!el) return;
  const chairs = items.reduce((n, r) => n + (r.quantity_remaining || 0), 0);
  const gaps = items.filter(r => !r.recorded).length;
  el.textContent = `${items.length} lot${items.length === 1 ? '' : 's'} · ${chairs.toLocaleString()} chairs`
    + (gaps && !filter.missing ? ` · ${gaps} not recorded` : '');
}

async function loadLocations({keepOld = false} = {}) {
  const el = list();
  if (!el) return undefined;
  const url = '/api/locations' + (filter.missing ? '?missing=1' : '');
  return uiLoad(el, ({signal}) => api(url, {signal}), {
    skeleton: 'row', count: 6, keepOld,
    isEmpty(data) {
      const items = data.items || [];
      renderCount(items);
      return !items.length;
    },
    empty: filter.missing
      ? {glyph: '✓', title: 'Every held lot has a location', body: 'Nothing left to record.',
         cta: {label: 'Show all', onClick: () => setFilter('')}}
      : {glyph: '◌', title: 'No held lots', body: 'Nothing is owned, won or in transit right now.'},
    render: (data) => tableFragment(data.items || []),
    errorMessage: (err) => err.status ? `Couldn't load locations. The server said ${err.status}.`
                                      : "Couldn't load locations. The database didn't answer in 15 s.",
  });
}

// ───────── render ─────────

function rowHtml(r) {
  const where = [r.city, r.state, r.zip_code].filter(Boolean).join(', ') || '—';
  const note = r.recorded
    ? `<div class="loc-note">${escapeHtml(r.storage_note)}</div>`
    : `<span class="badge loc-missing">NOT RECORDED</span>`;
  return `
    <td class="loc-where">${escapeHtml(where)}</td>
    <td><div class="loc-title">${escapeHtml(r.title || '')}</div><div class="mono tiny loc-id">${escapeHtml(r.lot_id)}</div></td>
    <td class="mono loc-qty">${(r.quantity_remaining ?? 0).toLocaleString()}</td>
    <td><span class="badge loc-status loc-status-${escapeAttr(r.status)}">${escapeHtml(r.status)}</span>
        ${r.fake_sold_out ? '<span class="badge loc-fso" title="Hidden from buyers (fake_sold_out), chairs still here">hidden</span>' : ''}</td>
    <td class="loc-note-cell">${note}</td>
    <td><button type="button" class="btn btn-small" data-edit>edit</button></td>`;
}

function tableFragment(items) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <table class="table loc-table">
      <thead><tr><th>Public city</th><th>Lot</th><th>Qty</th><th>Status</th><th>Storage (private)</th><th></th></tr></thead>
      <tbody></tbody>
    </table>`;
  const tbody = wrap.querySelector('tbody');
  for (const r of items) {
    const tr = document.createElement('tr');
    tr.dataset.lot = r.lot_id;
    if (!r.recorded) tr.className = 'is-missing';
    tr.innerHTML = rowHtml(r);
    tbody.appendChild(tr);
  }
  const frag = document.createDocumentFragment();
  frag.appendChild(wrap.firstElementChild);
  return frag;
}

// ───────── edit (delegated — rows are re-rendered on every load) ─────────

async function openEditor(btn, tr) {
  const lot = tr.dataset.lot;
  await pending(btn, '…', async () => {
    try {
      const row = await api(`/api/locations/${enc(lot)}?raw=1`);
      const cell = tr.querySelector('.loc-note-cell');
      cell.innerHTML = `
        <textarea class="loc-input mono tiny" rows="3"
                  placeholder="Facility — street, city, ZIP. Unit / space. Gate code. Who has the key.">${escapeHtml(row.storage_note || '')}</textarea>
        <div class="loc-edit-actions">
          <button type="button" class="btn btn-small btn-primary" data-save>save</button>
          <button type="button" class="btn btn-small btn-ghost" data-cancel>cancel</button>
        </div>`;
      cell.querySelector('textarea').focus();
    } catch (err) {
      toast("Couldn't open the note: " + (err.message || err), 'err');
    }
  });
}

async function save(btn, tr) {
  const lot = tr.dataset.lot;
  const storage_note = tr.querySelector('.loc-input')?.value ?? '';
  await pending(btn, '…', async () => {
    try {
      await api(`/api/locations/${enc(lot)}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({storage_note}),
      });
      toast(`Location saved for ${lot}.`, 'ok');
      loadLocations({keepOld: true});
    } catch (err) {
      toast('Save failed: ' + (err.message || err), 'err');
    }
  });
}

function onListClick(e) {
  const btn = e.target.closest('button[data-edit], button[data-save], button[data-cancel]');
  const tr = btn?.closest('tr[data-lot]');
  if (!tr) return;
  if (btn.dataset.edit !== undefined) openEditor(btn, tr);
  else if (btn.dataset.save !== undefined) save(btn, tr);
  else loadLocations({keepOld: true});
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;

  // Skeleton ships inside data-state="loading"; until the tab opens nothing is loading — drop the state so a
  // smoke on another tab is not blocked on a hidden pane. load() re-sets it.
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  el?.addEventListener('click', onListClick);
  $('#loc-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ loading…', () => loadLocations({keepOld: true}));
  });
  $$('#loc-filter .seg-btn').forEach(b => b.addEventListener('click', () => setFilter(b.dataset.value)));
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch. */
export async function load() {
  readParams();
  const el = list();
  return loadLocations({keepOld: !!el && el.dataset.state === 'ready'});
}
