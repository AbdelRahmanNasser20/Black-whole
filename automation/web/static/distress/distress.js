// static/distress/distress.js — public /distress page. ES module.
// The URL is the state. Reads only /distress/api/* — the server's column allow-list
// (public_distress.py) already drops trustee / attorney / party contacts.
import {api, fmt, esc} from '../ui/state.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const KEYS = ['q', 'industry', 'state', 'chapter', 'source', 'tab', 'sort', 'dir'];
const FILTERS = ['q', 'industry', 'state', 'chapter', 'source'];
const DEFAULTS = {tab: 'leads', sort: 'filed'};
const PER_PAGE = 50;
const LABELS = {hotel: 'Hotel', resort: 'Resort', catering: 'Catering', event_venue: 'Event venue',
  party_rental: 'Party rental', church: 'Church', restaurant: 'Restaurant', other: 'Other'};
const label = v => LABELS[v] || String(v || '').replace(/_/g, ' ');

const st = Object.assign({}, DEFAULTS, Object.fromEntries(
  KEYS.map(k => [k, new URLSearchParams(location.search).get(k)]).filter(([, v]) => v)));
let page = 1;
let last = null;

function qs() {
  const p = new URLSearchParams();
  for (const k of KEYS) if (st[k]) p.set(k, st[k]);
  p.set('page', String(page)); p.set('per_page', String(PER_PAGE));
  return p.toString();
}
function pushUrl() {
  const p = new URLSearchParams();
  for (const k of KEYS) if (st[k] && DEFAULTS[k] !== st[k]) p.set(k, st[k]);
  history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
}
function set(patch) {
  Object.assign(st, patch);
  for (const k of Object.keys(st)) if (st[k] === '') delete st[k];
  Object.assign(st, Object.fromEntries(Object.entries(DEFAULTS).filter(([k]) => !st[k])));
  page = 1; pushUrl(); sync(); loadCases();
}

function sync() {
  if ($('#ds-q').value !== (st.q || '')) $('#ds-q').value = st.q || '';
  $$('.seg[data-key]').forEach(seg => {
    const cur = st[seg.dataset.key] ?? '';
    $$('.seg-btn', seg).forEach(b => b.classList.toggle('is-active', (b.dataset.value || '') === String(cur)));
  });
  $('#ds-state').value = st.state || '';
  $('#ds-sort').value = st.dir ? `${st.sort}:${st.dir}` : st.sort;
  $$('#ds-chips .chip').forEach(c => c.classList.toggle('is-active', (c.dataset.v || '') === (st.industry || '')));
}

function day(iso) { return iso ? String(iso).slice(0, 10) : '—'; }
function row(r) {
  const court = r.source === 'warn' ? 'WARN notice' : [r.court_id, r.docket_number].filter(Boolean).join(' · ');
  const place = [r.city, r.state].filter(Boolean).join(', ');
  const ch = r.chapter ? `<span class="ds-tag${r.chapter === '7' ? ' is-ch7' : ''}">Ch ${esc(r.chapter)}</span> ` : '';
  const emp = r.employees_affected ? ` · ${esc(fmt.int(r.employees_affected))} jobs` : '';
  const sale = r.sale_noticed_at ? `<div class="ds-sale">Sale noticed ${esc(day(r.sale_noticed_at))}</div>` : '';
  const links = [
    r.docket_url && `<a href="${esc(r.docket_url)}" target="_blank" rel="noopener">Docket →</a>`,
    r.petition_url && r.petition_url !== r.docket_url && `<a href="${esc(r.petition_url)}" target="_blank" rel="noopener">${r.source === 'warn' ? 'Notice' : 'Petition'} →</a>`,
    r.sale_url && `<a href="${esc(r.sale_url)}" target="_blank" rel="noopener">Sale filing →</a>`,
  ].filter(Boolean).join('');
  return `<article class="ds-row">
    <div>
      <p class="ds-name">${esc(r.case_name || '—')}</p>
      <div class="ds-meta">${ch}<span class="ds-tag">${esc(label(r.industry_tag))}</span> · filed ${esc(day(r.date_filed))} · ${esc(court)}${place ? ' · ' + esc(place) : ''}${emp}</div>
      ${sale}
    </div>
    <div class="ds-links">${links}</div>
  </article>`;
}

async function loadFacets() {
  let f;
  try { f = await api('/distress/api/facets'); } catch (_) { return; }
  const inds = f.industries || [];
  const total = inds.reduce((a, c) => a + Number(c.count || 0), 0);
  const chip = (v, l, n) => `<button class="chip" type="button" data-v="${esc(v)}">${esc(l)} <span class="chip-count">${esc(fmt.int(n))}</span></button>`;
  $('#ds-chips').innerHTML = chip('', 'All', total) + inds.map(c => chip(c.value, label(c.value), c.count)).join('');
  const sel = $('#ds-state');
  (f.states || []).forEach(s => { const o = document.createElement('option'); o.value = s.value; o.textContent = `${s.value} (${fmt.int(s.count)})`; sel.appendChild(o); });
  const s = f.stats || {};
  $$('#ds-stats [data-stat]').forEach(el => { const v = s[el.dataset.stat]; el.textContent = v == null ? '—' : fmt.int(v); });
  sync();
}

async function loadCases({append = false} = {}) {
  const list = $('#ds-list');
  if (!append) list.setAttribute('aria-busy', 'true');
  let b;
  try { b = await api('/distress/api/cases?' + qs()); }
  catch (err) {
    list.removeAttribute('aria-busy');
    list.innerHTML = `<div class="ds-empty">Couldn't load cases${err.status ? ` (server said ${esc(err.status)})` : ''}.</div>`;
    $('#ds-more').hidden = true; return;
  }
  list.removeAttribute('aria-busy');
  last = b;
  const html = b.rows.map(row).join('');
  if (append) list.insertAdjacentHTML('beforeend', html);
  else list.innerHTML = html || `<div class="ds-empty">${st.tab === 'sales' ? 'No docket carries a sale or auction notice yet for these filters.' : 'No cases match these filters.'}</div>`;
  $('#ds-count').innerHTML = `<strong>${esc(fmt.int(b.total))}</strong> case${b.total === 1 ? '' : 's'}`;
  const remaining = b.total - b.page * b.per_page;
  $('#ds-more').hidden = !(remaining > 0);
  if (remaining > 0) $('#ds-more').textContent = `Load more (${fmt.int(remaining)} remaining)`;
}

let qTimer = null;
$('#ds-q').addEventListener('input', e => { clearTimeout(qTimer); qTimer = setTimeout(() => set({q: e.target.value.trim()}), 300); });
$('#ds-search').addEventListener('submit', e => { e.preventDefault(); set({q: $('#ds-q').value.trim()}); });
$$('.seg[data-key]').forEach(seg => seg.addEventListener('click', e => {
  const b = e.target.closest('.seg-btn'); if (b) set({[seg.dataset.key]: b.dataset.value});
}));
$('#ds-state').addEventListener('change', e => set({state: e.target.value}));
$('#ds-sort').addEventListener('change', e => { const [sort, dir] = e.target.value.split(':'); set({sort, dir: dir || ''}); });
$('#ds-chips').addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) set({industry: c.dataset.v}); });
$('#ds-clear').addEventListener('click', () => set(Object.fromEntries(FILTERS.map(k => [k, '']))));
$('#ds-more').addEventListener('click', () => { page += 1; loadCases({append: true}); });
try {
  const about = $('#ds-about');
  about.open = localStorage.getItem('distress.about') === 'open';
  about.addEventListener('toggle', () => { try { localStorage.setItem('distress.about', about.open ? 'open' : 'closed'); } catch (_) {} });
} catch (_) {}

sync();
loadFacets();
loadCases();
