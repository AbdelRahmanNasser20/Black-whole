// static/admin/chairs.js — Chairs tab (12), the operator-only chairs feed.
// One read site: GET /api/deals/chairs → UI.load (skeleton twin shipped in index.html → ready | empty | error).
// Every live seating lot with qty >= N across GovDeals + every recorder source, ranked by $/chair.
// Filters live in the URL so a filtered view is refresh-safe: ?cmin= ?csite= ?cstate= ?cend= ?csort=
// (prefixed `c` — Deals already owns `site`/`state`/`sort`). Table only, no map. Read-only: no buttons that write.
import {$, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, api, fmt} from '../ui/state.js';

const DEFAULT_MIN = 50;
const SORTS = ['unit_bid', 'ends', 'quantity', 'bid'];
const filter = {min: DEFAULT_MIN, site: '', state: '', end: '', sort: 'unit_bid'};
let sitesFilled = false;

const list = () => $('#chr-list');

function money(v) {
  if (v == null || isNaN(v)) return '—';
  return '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

// ───────── URL ↔ controls ─────────

function readParams() {
  const p = getParams();
  const n = parseInt(p.cmin, 10);
  filter.min = Number.isFinite(n) && n >= 0 ? n : DEFAULT_MIN;
  filter.site = p.csite || '';
  filter.state = /^[A-Za-z]{2}$/.test(p.cstate || '') ? p.cstate.toUpperCase() : '';
  filter.end = /^\d+$/.test(p.cend || '') ? p.cend : '';
  filter.sort = SORTS.includes(p.csort) ? p.csort : 'unit_bid';
  syncControls();
}

function syncControls() {
  const set = (id, v) => { const el = $(id); if (el) el.value = v; };
  set('#chr-min', filter.min);
  set('#chr-site', filter.site);
  set('#chr-state', filter.state);
  set('#chr-end', filter.end);
  set('#chr-sort', filter.sort);
}

function writeParams() {
  setParams({
    cmin: filter.min === DEFAULT_MIN ? null : String(filter.min),
    csite: filter.site || null,
    cstate: filter.state || null,
    cend: filter.end || null,
    csort: filter.sort === 'unit_bid' ? null : filter.sort,
  });
}

function fromControls() {
  const n = parseInt($('#chr-min')?.value, 10);
  filter.min = Number.isFinite(n) && n >= 0 ? n : DEFAULT_MIN;
  filter.site = $('#chr-site')?.value || '';
  const st = ($('#chr-state')?.value || '').trim().toUpperCase();
  filter.state = /^[A-Z]{2}$/.test(st) ? st : '';
  filter.end = $('#chr-end')?.value || '';
  filter.sort = SORTS.includes($('#chr-sort')?.value) ? $('#chr-sort').value : 'unit_bid';
  writeParams();
  return loadChairs({keepOld: true});
}

function fillSites(names) {
  const sel = $('#chr-site');
  if (!sel || sitesFilled || !names) return;
  for (const [value, label] of Object.entries(names)) {
    const o = document.createElement('option');
    o.value = value; o.textContent = label;
    sel.appendChild(o);
  }
  sitesFilled = true;
  sel.value = filter.site;
}

// ───────── render ─────────

function row(r, i) {
  const place = [r.city, r.state].filter(Boolean).join(', ') || '—';
  const qtyTitle = `quantity from ${r.quantity_source}`;
  const link = r.url
    ? `<a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title || '(untitled)')}</a>`
    : escapeHtml(r.title || '(untitled)');
  const viewer = r.viewer_url ? ` <a class="chr-viewer" href="${escapeAttr(r.viewer_url)}" target="_blank" rel="noopener" title="Archived-lot viewer">◫</a>` : '';
  const pick = r.operator_pick ? ' <span class="badge chr-pick" title="Starred / tracked / on a list">★</span>' : '';
  const landed = r.unit_landed != null ? `<span class="chr-sub" title="landed $/chair incl. buyer premium">${money(r.unit_landed)} landed</span>` : '';
  return `<tr>
    <td class="num chr-rank">${i + 1}</td>
    <td class="num chr-unit">${money(r.unit_bid)}${landed}</td>
    <td class="num" title="${escapeAttr(qtyTitle)}">${r.quantity != null ? fmt.int(r.quantity) : '?'}<span class="chr-sub">${escapeHtml(r.quantity_source)}</span></td>
    <td class="num">${money(r.current_bid)}<span class="chr-sub">${r.bid_count ?? 0} bids</span></td>
    <td class="num" title="${escapeAttr(r.end_utc || '')}">${escapeHtml(fmt.endsIn(r.end_utc))}</td>
    <td class="chr-title">${link}${viewer}${pick}</td>
    <td>${escapeHtml(place)}</td>
    <td><span class="badge chr-src">${escapeHtml(r.source_name || r.source)}</span></td>
  </tr>`;
}

function renderChairs(data) {
  const errs = $('#chr-errors');
  if (errs) {
    errs.hidden = !(data.errors && data.errors.length);
    errs.textContent = data.errors?.length ? 'Partial: ' + data.errors.join(' · ') : '';
  }
  return `<table class="table chr-table">
    <thead><tr>
      <th class="num">#</th><th class="num">$ / chair</th><th class="num">Qty</th><th class="num">Bid</th>
      <th class="num">Ends</th><th>Lot</th><th>Where</th><th>Source</th>
    </tr></thead>
    <tbody>${data.rows.map(row).join('')}</tbody>
  </table>`;
}

// ───────── load ─────────

function query() {
  const q = new URLSearchParams({min_qty: String(filter.min), sort: filter.sort});
  if (filter.site) q.set('site', filter.site);
  if (filter.state) q.set('state', filter.state);
  if (filter.end) q.set('ending', filter.end);
  return '/api/deals/chairs?' + q.toString();
}

async function loadChairs({keepOld = false} = {}) {
  const el = list();
  if (!el) return;
  const count = $('#chr-count');
  return uiLoad(el, async ({signal}) => {
    const data = await api(query(), {signal});
    fillSites(data.site_names);
    if (count) count.textContent = `${data.total} lot${data.total === 1 ? '' : 's'}`
      + (data.sites ? ' · ' + Object.entries(data.sites).map(([k, n]) => `${k} ${n}`).join(' · ') : '');
    return data;
  }, {
    skeleton: 'row',
    count: 8,
    keepOld,
    timeoutMs: 45000,               // a cold snapshot read across every source can take a while
    isEmpty: (d) => !d.rows.length,
    render: renderChairs,
    empty: {
      glyph: '◌',
      title: `No live seating lots with ${filter.min}+ chairs`,
      body: 'Lower the minimum quantity (0 also shows lots with no readable count) or clear a filter.',
    },
  });
}

// ───────── contract ─────────

export function mount() {
  readParams();
  ['#chr-site', '#chr-end', '#chr-sort'].forEach(id => $(id)?.addEventListener('change', fromControls));
  ['#chr-min', '#chr-state'].forEach(id => $(id)?.addEventListener('change', fromControls));
  $('#chr-refresh')?.addEventListener('click', () => loadChairs({keepOld: true}));
}

export async function load() {
  readParams();
  return loadChairs();
}
