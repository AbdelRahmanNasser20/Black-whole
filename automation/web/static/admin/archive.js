// static/admin/archive.js — Archive tab: the private, permanent copy of every closed GovDeals lot (recorder/lot_archive.py).
// Reads: GET /api/archive/lots?q=&category=&outcome=&min_price=&max_price=&since=&until=&page= → UI.load on #arc-list.
// URL state: the filter keys above through shared.js getParams/setParams — the shell owns only `tab`.
// Each row opens /admin/archive/{source}/{asset}/{account}/{auction} (source = govdeals | allsurplus), the listing rebuilt from our archive only.
import {$, esc, getParams, setParams} from './shared.js';
import {api, fmt, load as uiLoad} from '../ui/state.js';

const KEYS = ['q', 'source', 'category', 'outcome', 'min_price', 'max_price', 'since', 'until', 'page'];
const arc = {loadedOnce: false, facetsPainted: false};

function _form() { return $('#arc-form'); }

function _readForm() {
  const f = _form();
  const out = {};
  if (!f) return out;
  for (const k of KEYS) {
    const el = f.elements.namedItem(k);
    if (el && el.value !== '') out[k] = el.value;
  }
  return out;
}

function _fillForm(params) {
  const f = _form();
  if (!f) return;
  for (const k of KEYS) {
    const el = f.elements.namedItem(k);
    if (el && k !== 'page') el.value = params[k] || '';
  }
}

function _paintFacets(facets) {
  const f = _form();
  if (!f || !facets) return;
  const fill = (name, counts) => {
    const sel = f.elements.namedItem(name);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">all</option>' + Object.entries(counts || {})
      .map(([k, n]) => `<option value="${esc(k)}">${esc(k.replace(/_/g, ' '))} (${n})</option>`).join('');
    sel.value = cur;
  };
  fill('source', facets.source);
  fill('category', facets.category);
  fill('outcome', facets.outcome);
  _fillForm(getParams());
}

const SOURCES = new Set(['govdeals', 'allsurplus']);
function _src(m) { return SOURCES.has(m.source) ? m.source : 'govdeals'; }
function _lotHref(m) { const [a, b, c] = m.lot_key.split('/'); return `/admin/archive/${_src(m)}/${a}/${b}/${c}`; }
function _photoSrc(m) { const [a, b, c] = m.lot_key.split('/'); return `/api/archive/${_src(m)}/${a}/${b}/${c}/photo/0`; }
function _price(m) { return (!m.currency || m.currency === 'USD') ? fmt.money(m.final_price) : (m.final_price == null ? '—' : `${esc(m.currency)} ${Number(m.final_price).toLocaleString()}`); }

function _row(m) {
  const place = [m.city, m.state].filter(Boolean).join(', ');
  const outcome = m.outcome || 'unknown';
  return `<tr>
    <td>${m.photo_count ? `<img class="arc-thumb" src="${_photoSrc(m)}" alt="" loading="lazy">` : ''}</td>
    <td class="arc-lot"><a href="${_lotHref(m)}">${esc(m.title || m.lot_key)}</a>
      <div class="tiny mono">${_src(m) === 'allsurplus' ? 'AllSurplus · ' : ''}${esc(m.lot_key)}${place ? ' · ' + esc(place) : ''}${m.completeness === 'partial' ? ' · partial' : ''}</div></td>
    <td class="tiny">${esc((m.canonical_category || 'other').replace(/_/g, ' '))}</td>
    <td><span class="arc-outcome arc-o-${esc(outcome)}">${esc(outcome.replace(/_/g, ' '))}</span></td>
    <td class="num mono">${_price(m)}</td>
    <td class="num mono">${m.bid_count ?? '—'}</td>
    <td class="tiny mono">${fmt.date(m.closed_at)}</td>
  </tr>`;
}

function _render(b) {
  const pages = b.pages || 1;
  const summary = $('#arc-summary');
  if (summary) summary.textContent = `${b.total.toLocaleString()} of ${b.archived.toLocaleString()} archived lots · store ${b.store || '—'} · index ${b.index}`;
  return `<table class="table"><thead><tr><th></th><th>Lot</th><th>Category</th><th>Outcome</th><th>Final</th><th>Bids</th><th>Closed</th></tr></thead>
    <tbody>${b.items.map(_row).join('')}</tbody></table>
    <div class="arc-pager">page ${b.page} / ${pages}
      <button type="button" class="btn btn-small" data-arc-page="${b.page - 1}" ${b.page <= 1 ? 'disabled' : ''}>‹ prev</button>
      <button type="button" class="btn btn-small" data-arc-page="${b.page + 1}" ${b.page >= pages ? 'disabled' : ''}>next ›</button></div>`;
}

async function loadArchive() {
  const list = $('#arc-list');
  if (!list) return undefined;
  const params = {};
  const p = getParams();
  for (const k of KEYS) if (p[k]) params[k] = p[k];
  _fillForm(p);
  const url = '/api/archive/lots' + (Object.keys(params).length ? '?' + new URLSearchParams(params) : '');
  const filtered = Object.keys(params).some(k => k !== 'page');
  const body = await uiLoad(list, async ({signal}) => {
    const b = await api(url, {signal});
    _paintFacets(b.facets);
    return b;
  }, {
    skeleton: 'row', count: 6, keepOld: arc.loadedOnce,
    isEmpty: (b) => !(b.items || []).length,
    render: _render,
    empty: filtered
      ? {glyph: '◌', title: 'No archived lot matches', body: 'Loosen a filter or clear them all.', cta: {label: 'Clear filters', onClick: clearFilters}}
      : {glyph: '◌', title: 'Nothing archived yet', body: 'Run `python -m recorder.cli archive-backfill --apply`, or let the recorder archive lots as they close.'},
    errorMessage: (err) => err.status === 503
      ? 'The archive store is not configured on this server (R2_* or LOT_ARCHIVE_STORE=local).'
      : `Couldn't load the archive. ${err.status ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.` : (err.message || '')}`,
  });
  if (body !== undefined) arc.loadedOnce = true;
  return body;
}

function applyFilters() {
  const patch = Object.fromEntries(KEYS.map(k => [k, null]));
  Object.assign(patch, _readForm());
  delete patch.page; patch.page = null;
  setParams(patch);
  loadArchive();
}

function clearFilters() {
  const f = _form();
  if (f) f.reset();
  setParams(Object.fromEntries(KEYS.map(k => [k, null])));
  loadArchive();
}

export function mount() {
  const f = _form();
  if (!f) return;
  f.addEventListener('submit', (e) => { e.preventDefault(); applyFilters(); });
  f.addEventListener('change', (e) => { if (e.target.tagName === 'SELECT') applyFilters(); });
  $('#arc-clear')?.addEventListener('click', clearFilters);
  $('#arc-list')?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-arc-page]');
    if (!b || b.disabled) return;
    setParams({page: b.dataset.arcPage});
    loadArchive();
  });
}

export function load() { return loadArchive(); }
