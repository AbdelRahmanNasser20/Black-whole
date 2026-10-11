// static/site/platform/bankruptcies.js — GET /platform/bankruptcies. ES module.
//
//   Data      LIVE first: /distress/api/facets + /distress/api/cases (public columns only, no contacts),
//             up to MAX_LIVE newest rows, normalised by fromCase() onto the page's row shape.
//             FALLBACK: any API error (503 = migration 024 not applied) or an empty table loads the
//             invented /static/site/platform/bankruptcies.sample.json and flips on the sample badge + banner.
//   Tabs      Leads = every case; Sales = cases with a sale / auction notice (sale_noticed_at).
//   Filters   search, state, chapter, industry, filed-within, assets band (sample only), sort — in memory.
//   Drawer    one filing's detail. Memory only; nothing is saved.
//   Ask AI    UI ONLY. The form never leaves the page: it always answers the coming-soon state and offers
//             the filters instead. There is no model behind it yet, and the page never claims one.
import {api, esc} from '../../ui/state.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SAMPLE_URL = '/static/site/platform/bankruptcies.sample.json';
const CASES_URL = '/distress/api/cases';
const FACETS_URL = '/distress/api/facets';
const PER_PAGE = 100;            // the API's largest page
const MAX_LIVE = 1000;           // newest N cases held in memory for the in-page filters
const DAY_MS = 86400000;
const SALE_SIGNALS = new Set(['Sale motion filed', 'Auction scheduled']);
const INDUSTRY_LABELS = {hotel: 'Hotel', resort: 'Resort', catering: 'Catering', event_venue: 'Event venue',
  party_rental: 'Party rental', church: 'Church', restaurant: 'Restaurant', other: 'Other'};
export const industryLabel = v => INDUSTRY_LABELS[v] || String(v || '').replace(/_/g, ' ') || 'Other';

const state = {data: null, mode: null, total: 0, rows: [], tab: 'leads', q: '', state: '', chapter: '', industry: '',
               filed: '', assets: '', sort: 'filed:desc', selected: null};

const day = iso => (iso ? String(iso).slice(0, 10) : '');

// ── live API row → page row (pure) ──
export function fromCase(c) {
  const warn = c.source === 'warn';
  const jobs = Number(c.employees_affected) > 0 ? ` · ${Number(c.employees_affected).toLocaleString('en-US')} jobs` : '';
  const sale = day(c.sale_noticed_at);
  return {
    id: String(c.id), live: true, source: c.source,
    debtor: c.case_name || '—',
    chapter: c.chapter ? String(c.chapter) : (warn ? 'WARN' : '—'),
    industry: industryLabel(c.industry_tag),
    city: c.city || '', state: c.state || '',
    filed: day(c.date_filed) || day(c.effective_date),
    est_assets: null, est_liabilities: null,
    court: warn ? 'WARN notice' : (c.court_id || ''),
    case_no: c.docket_number || '',
    signal: sale ? `Sale noticed ${sale}` : (warn ? 'WARN closure notice' + jobs : 'Petition filed'),
    sale: Boolean(sale), sale_noticed_at: sale,
    asset_classes: [],
    docket_url: c.docket_url || '', petition_url: c.petition_url || '', sale_url: c.sale_url || '',
    employees_affected: c.employees_affected || null, effective_date: day(c.effective_date),
  };
}
function fromSample(r) { return {...r, sale: SALE_SIGNALS.has(r.signal)}; }

function money(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return '$' + n.toLocaleString('en-US', {maximumFractionDigits: 0});
}
function moneyShort(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M';
  if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'k';
  return '$' + n;
}
function daysAgo(iso) {
  const t = new Date(iso + 'T00:00:00Z').getTime();
  return Math.floor((Date.now() - t) / DAY_MS);
}
function fillSelect(sel, values) {
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v; o.textContent = v; sel.appendChild(o);
  }
}

// ── filter + sort (pure) ──
export function filterRows(rows, f, nowMs = Date.now()) {
  let out = rows;
  if (f.tab === 'sales') out = out.filter(r => r.sale);
  if (f.state) out = out.filter(r => r.state === f.state);
  if (f.chapter) out = out.filter(r => r.chapter === f.chapter);
  if (f.industry) out = out.filter(r => r.industry === f.industry);
  if (f.filed) {
    const edge = nowMs - Number(f.filed) * DAY_MS;
    out = out.filter(r => r.filed && new Date(r.filed + 'T00:00:00Z').getTime() >= edge);
  }
  if (f.assets) {
    const [lo, hi] = f.assets.split('-').map(x => (x === '' ? null : Number(x)));
    out = out.filter(r => r.est_assets != null && (lo == null || r.est_assets >= lo) && (hi == null || r.est_assets < hi));
  }
  const terms = String(f.q || '').toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  if (terms.length) {
    out = out.filter(r => {
      const hay = `${r.debtor} ${r.industry} ${r.city} ${r.state} ${r.signal} ${(r.asset_classes || []).join(' ')} ${r.case_no} ${r.court}`.toLowerCase();
      return terms.every(t => hay.includes(t));
    });
  }
  const [key, dir] = String(f.sort || 'filed:desc').split(':');
  const sign = dir === 'asc' ? 1 : -1;
  return [...out].sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === y) return 0;
    if (x == null || x === '') return 1;           // blanks last, whatever the direction
    if (y == null || y === '') return -1;
    return (x > y ? 1 : -1) * sign;
  });
}

// ── render ──
function rowHtml(r) {
  const age = r.filed ? daysAgo(r.filed) : null;
  const when = age == null ? '' : age <= 0 ? 'today' : age === 1 ? 'yesterday' : `${age} d ago`;
  const sub = r.live ? [r.court, r.case_no].filter(Boolean).join(' · ') : (r.asset_classes || []).slice(0, 2).join(' · ');
  const where = [r.city, r.state].filter(Boolean).join(', ') || '—';
  const fourth = r.live ? esc(r.court || '—') : esc(moneyShort(r.est_assets));
  return `<tr class="pt-row${state.selected === r.id ? ' is-selected' : ''}" data-id="${esc(r.id)}" tabindex="0">
    <td class="pt-c-main"><span class="pt-row-main">${esc(r.debtor)}</span><span class="pt-row-sub">${esc(sub)}</span></td>
    <td class="mono" data-label="Chapter">${esc(r.chapter)}</td>
    <td data-label="Industry">${esc(r.industry)}</td>
    <td data-label="Where">${esc(where)}</td>
    <td class="mono" data-label="Filed">${esc(r.filed || '—')}<span class="pt-row-sub">${esc(when)}</span></td>
    <td class="num" data-label="${r.live ? 'Court' : 'Est. assets'}">${fourth}</td>
    <td data-label="Signal">${esc(r.signal)}</td>
  </tr>`;
}

function render() {
  if (!state.data) return;
  state.rows = filterRows(state.data.filings, state);
  const tbody = $('#bk-rows');
  const held = state.data.filings.length;
  const noun = state.mode === 'live' ? 'cases' : 'sample filings';
  const of = state.mode === 'live' && state.total > held ? `newest ${held.toLocaleString('en-US')} of ${state.total.toLocaleString('en-US')}` : held.toLocaleString('en-US');
  $('#bk-count').textContent = state.rows.length === held ? `${of} ${noun}` : `${state.rows.length.toLocaleString('en-US')} of ${of} ${noun}`;
  const none = state.tab === 'sales' ? 'No case carries a sale or auction notice for these filters yet.' : `No ${state.mode === 'live' ? 'case' : 'sample filing'} matches these filters.`;
  tbody.innerHTML = state.rows.map(rowHtml).join('') ||
    `<tr class="pt-row-msg"><td colspan="7">${esc(none)} <button type="button" class="btn btn-ghost btn-small" id="bk-empty-reset">Clear filters</button></td></tr>`;
  $$('#bk-tab .pt-seg-btn').forEach(b => {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on));
  });
  $$('.pt-sort').forEach(b => {
    const [key, dir] = state.sort.split(':');
    b.classList.toggle('is-sorted', b.dataset.sort === key);
    if (b.dataset.sort === key) b.dataset.dir = dir; else delete b.dataset.dir;
  });
}

// ── drawer ──
function liveDrawer(r) {
  const link = (u, t) => (u ? `<a class="btn btn-ghost btn-small" href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>` : '');
  const warn = r.source === 'warn';
  return `
    <div class="pt-drawer-kicker">${esc([r.case_no, r.court].filter(Boolean).join(' · ') || (warn ? 'WARN notice' : 'Court case'))}</div>
    <h2 class="pt-drawer-title" id="bk-drawer-title">${esc(r.debtor)}</h2>
    <dl>
      <div><dt>${warn ? 'Notice' : 'Chapter'}</dt><dd class="mono">${esc(r.chapter)}</dd></div>
      <div><dt>Filed</dt><dd class="mono">${esc(r.filed || '—')}</dd></div>
      <div><dt>Industry</dt><dd>${esc(r.industry)}</dd></div>
      <div><dt>Where</dt><dd>${esc([r.city, r.state].filter(Boolean).join(', ') || '—')}</dd></div>
      ${warn && r.employees_affected ? `<div><dt>Jobs affected</dt><dd class="mono">${esc(Number(r.employees_affected).toLocaleString('en-US'))}</dd></div>` : ''}
      ${warn && r.effective_date ? `<div><dt>Effective</dt><dd class="mono">${esc(r.effective_date)}</dd></div>` : ''}
      <div><dt>Sale notice</dt><dd>${esc(r.sale_noticed_at || 'None on the docket yet')}</dd></div>
      <div><dt>Source</dt><dd>${warn ? 'State WARN notice' : 'CourtListener (PACER docket)'}</dd></div>
    </dl>
    <div class="pt-drawer-actions">
      ${link(r.docket_url, 'DOCKET')}
      ${r.petition_url !== r.docket_url ? link(r.petition_url, warn ? 'NOTICE' : 'PETITION') : ''}
      ${link(r.sale_url, 'SALE FILING')}
      <a class="btn btn-ghost btn-small" href="/platform/deals">SEE OPEN AUCTIONS</a>
    </div>
    <p class="pt-drawer-note">Public record. Trustee, attorney and party contacts are not shown here, and watching a case is not built yet.</p>`;
}

function openDrawer(r) {
  state.selected = r.id;
  $('#bk-drawer-chip').hidden = Boolean(r.live);
  $('#bk-drawer-body').innerHTML = r.live ? liveDrawer(r) : `
    <div class="pt-drawer-kicker">${esc(r.case_no)} · ${esc(r.court)}</div>
    <h2 class="pt-drawer-title" id="bk-drawer-title">${esc(r.debtor)}</h2>
    <dl>
      <div><dt>Chapter</dt><dd class="mono">${esc(r.chapter)}</dd></div>
      <div><dt>Filed</dt><dd class="mono">${esc(r.filed)}</dd></div>
      <div><dt>Industry</dt><dd>${esc(r.industry)}</dd></div>
      <div><dt>Where</dt><dd>${esc(r.city)}, ${esc(r.state)}</dd></div>
      <div><dt>Est. assets</dt><dd class="mono">${esc(money(r.est_assets))}</dd></div>
      <div><dt>Est. liabilities</dt><dd class="mono">${esc(money(r.est_liabilities))}</dd></div>
      <div><dt>Latest signal</dt><dd>${esc(r.signal)}</dd></div>
    </dl>
    <h3>Asset classes listed</h3>
    <ul>${(r.asset_classes || []).map(a => `<li>${esc(a)}</li>`).join('')}</ul>
    <h3>Summary</h3>
    <p>${esc(r.summary)}</p>
    <div class="pt-drawer-actions">
      <button type="button" class="btn btn-primary btn-small" disabled title="Not built yet">WATCH THIS CASE</button>
      <a class="btn btn-ghost btn-small" href="/platform/deals">SEE OPEN AUCTIONS</a>
    </div>
    <p class="pt-drawer-note">Watching a case, docket links and trustee contact are not built yet. This filing is invented.</p>`;
  $('#bk-drawer').hidden = false; $('#bk-scrim').hidden = false;
  render();
  $('#bk-drawer').focus();
}
function closeDrawer() {
  state.selected = null;
  $('#bk-drawer').hidden = true; $('#bk-scrim').hidden = true;
  render();
}

// ── Ask AI: UI only, never leaves the page ──
function initAsk() {
  const form = $('#bk-ask-form'), input = $('#bk-ask-q'), out = $('#bk-ask-result');
  if (!form) return;
  const answer = q => {
    const asked = q.trim();
    out.innerHTML = `<span class="pt-chip pt-chip--soon">Coming soon</span>
      <strong>Plain-English search is not built yet.</strong>
      ${asked ? `You asked: “${esc(asked)}”. ` : ''}When it lands it will turn a question like that into the filters below.
      For now, <button type="button" id="bk-ask-filters">use the filters</button> — they cover state, chapter, industry, filing date and assets.`;
    out.hidden = false;
    $('#bk-ask-filters').addEventListener('click', () => $('#bk-q').focus());
  };
  form.addEventListener('submit', e => { e.preventDefault(); answer(input.value); });
  $$('.bk-ask-example').forEach(b => b.addEventListener('click', () => { input.value = b.textContent; answer(input.value); }));
}

// ── wire ──
function wire() {
  const bind = (id, key) => $(id).addEventListener('input', e => { state[key] = e.target.value; render(); });
  bind('#bk-q', 'q'); bind('#bk-state', 'state'); bind('#bk-chapter', 'chapter'); bind('#bk-industry', 'industry');
  bind('#bk-filed', 'filed'); bind('#bk-assets', 'assets'); bind('#bk-sort', 'sort');
  $('#bk-form').addEventListener('submit', e => e.preventDefault());
  const reset = () => {
    for (const k of ['q', 'state', 'chapter', 'industry', 'filed', 'assets']) state[k] = '';
    state.sort = 'filed:desc';
    $('#bk-form').reset();
    render();
  };
  $('#bk-reset').addEventListener('click', reset);
  $$('#bk-tab .pt-seg-btn').forEach(b => b.addEventListener('click', () => { state.tab = b.dataset.tab; render(); }));
  $('#bk-rows').addEventListener('click', e => {
    if (e.target.closest('#bk-empty-reset')) { reset(); return; }
    const tr = e.target.closest('tr.pt-row');
    if (!tr) return;
    const r = state.rows.find(x => x.id === tr.dataset.id);
    if (r) openDrawer(r);
  });
  $('#bk-rows').addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr.pt-row');
    if (!tr) return;
    e.preventDefault();
    const r = state.rows.find(x => x.id === tr.dataset.id);
    if (r) openDrawer(r);
  });
  $$('.pt-sort').forEach(b => b.addEventListener('click', () => {
    const [key, dir] = state.sort.split(':');
    const next = b.dataset.sort === key && dir === 'desc' ? 'asc' : b.dataset.sort === key ? 'desc' : (b.dataset.sort === 'debtor' ? 'asc' : 'desc');
    state.sort = `${b.dataset.sort}:${next}`;
    $('#bk-sort').value = state.sort;
    render();
  }));
  $('#bk-drawer-close').addEventListener('click', closeDrawer);
  $('#bk-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#bk-drawer').hidden) closeDrawer(); });
}

// ── load: live API first, invented sample on any failure or an empty table ──
async function loadLive() {
  const first = await api(`${CASES_URL}?sort=filed&dir=desc&page=1&per_page=${PER_PAGE}`);
  if (!first || !Array.isArray(first.rows) || !first.total) return null;      // empty table → sample
  const pages = Math.min(first.pages || 1, Math.ceil(MAX_LIVE / PER_PAGE));
  const rest = await Promise.all(Array.from({length: pages - 1}, (_, k) =>
    api(`${CASES_URL}?sort=filed&dir=desc&page=${k + 2}&per_page=${PER_PAGE}`)));
  let facets = null;
  try { facets = await api(FACETS_URL); } catch { /* filters fall back to the loaded rows */ }
  const cases = [first, ...rest].flatMap(b => b.rows || []);
  return {total: first.total, facets, filings: cases.map(fromCase)};
}

function setMode(mode) {
  state.mode = mode;
  const live = mode === 'live';
  const chip = $('#bk-mode');
  chip.className = `pt-chip ${live ? 'pt-chip--live' : 'pt-chip--sample'}`;
  chip.textContent = live ? 'Live court data' : 'Sample data, invented';
  $('#bk-banner-live').hidden = !live;
  $('#bk-banner-sample').hidden = live;
  // live rows carry no asset estimate: the column shows the court, the assets filter/sorts go away
  $('#bk-assets').hidden = live;
  $$('#bk-sort option[value^="est_assets"]').forEach(o => { o.hidden = live; o.disabled = live; });
  if (live) $('#bk-col-assets').textContent = 'Court';
}

async function load() {
  let live = null;
  try { live = await loadLive(); } catch { live = null; }
  if (live) {
    state.data = {filings: live.filings};
    state.total = live.total;
    setMode('live');
    const f = live.facets || {};
    const rows = live.filings;
    const facetStates = (f.states || []).map(s => s.value).filter(Boolean);
    fillSelect($('#bk-state'), (facetStates.length ? facetStates : rows.map(r => r.state).filter(Boolean))
      .filter((v, i, a) => a.indexOf(v) === i).sort());
    fillSelect($('#bk-chapter'), [...new Set(rows.map(r => r.chapter).filter(c => c && c !== '—'))].sort());
    fillSelect($('#bk-industry'), (f.industries || []).length
      ? f.industries.map(c => industryLabel(c.value)) : [...new Set(rows.map(r => r.industry))].sort());
    render();
    return;
  }
  try {
    const data = await api(SAMPLE_URL);
    state.data = {...data, filings: (data.filings || []).map(fromSample)};
  } catch {
    $('#bk-rows').innerHTML = '<tr class="pt-row-msg"><td colspan="7">The filings did not load. Reload the page to try again.</td></tr>';
    return;
  }
  const rows = state.data.filings;
  state.total = rows.length;
  setMode('sample');
  fillSelect($('#bk-state'), [...new Set(rows.map(r => r.state))].sort());
  fillSelect($('#bk-chapter'), state.data.chapters || [...new Set(rows.map(r => r.chapter))]);
  fillSelect($('#bk-industry'), state.data.industries || [...new Set(rows.map(r => r.industry))]);
  render();
}

if ($('#bk-rows')) { wire(); initAsk(); load(); }
