// static/site/platform/bankruptcies.js — GET /platform/bankruptcies. ES module.
//
//   Data      SAMPLE, invented: /static/site/platform/bankruptcies.sample.json. Nothing is a court record.
//   Filters   search, state, chapter, industry, filed-within, assets band, sort — all in memory.
//   Drawer    one filing's detail (case, court, asset classes, summary). Memory only; nothing is saved.
//   Ask AI    UI ONLY. The form never leaves the page: it always answers the coming-soon state and offers
//             the filters instead. There is no model behind it yet, and the page never claims one.
import {api, esc} from '../../ui/state.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SAMPLE_URL = '/static/site/platform/bankruptcies.sample.json';
const DAY_MS = 86400000;

const state = {data: null, rows: [], q: '', state: '', chapter: '', industry: '', filed: '', assets: '',
               sort: 'filed:desc', selected: null};

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
  if (f.state) out = out.filter(r => r.state === f.state);
  if (f.chapter) out = out.filter(r => r.chapter === f.chapter);
  if (f.industry) out = out.filter(r => r.industry === f.industry);
  if (f.filed) {
    const edge = nowMs - Number(f.filed) * DAY_MS;
    out = out.filter(r => new Date(r.filed + 'T00:00:00Z').getTime() >= edge);
  }
  if (f.assets) {
    const [lo, hi] = f.assets.split('-').map(x => (x === '' ? null : Number(x)));
    out = out.filter(r => (lo == null || r.est_assets >= lo) && (hi == null || r.est_assets < hi));
  }
  const terms = String(f.q || '').toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  if (terms.length) {
    out = out.filter(r => {
      const hay = `${r.debtor} ${r.industry} ${r.city} ${r.state} ${r.signal} ${(r.asset_classes || []).join(' ')} ${r.case_no}`.toLowerCase();
      return terms.every(t => hay.includes(t));
    });
  }
  const [key, dir] = String(f.sort || 'filed:desc').split(':');
  const sign = dir === 'asc' ? 1 : -1;
  return [...out].sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === y) return 0;
    return (x > y ? 1 : -1) * sign;
  });
}

// ── render ──
function rowHtml(r) {
  const age = daysAgo(r.filed);
  const when = age <= 0 ? 'today' : age === 1 ? 'yesterday' : `${age} d ago`;
  return `<tr class="pt-row${state.selected === r.id ? ' is-selected' : ''}" data-id="${esc(r.id)}" tabindex="0">
    <td class="pt-c-main"><span class="pt-row-main">${esc(r.debtor)}</span><span class="pt-row-sub">${esc((r.asset_classes || []).slice(0, 2).join(' · '))}</span></td>
    <td class="mono" data-label="Chapter">${esc(r.chapter)}</td>
    <td data-label="Industry">${esc(r.industry)}</td>
    <td data-label="Where">${esc(r.city)}, ${esc(r.state)}</td>
    <td class="mono" data-label="Filed">${esc(r.filed)}<span class="pt-row-sub">${esc(when)}</span></td>
    <td class="num" data-label="Est. assets">${esc(moneyShort(r.est_assets))}</td>
    <td data-label="Signal">${esc(r.signal)}</td>
  </tr>`;
}

function render() {
  if (!state.data) return;
  state.rows = filterRows(state.data.filings, state);
  const tbody = $('#bk-rows');
  const total = state.data.filings.length;
  $('#bk-count').textContent = state.rows.length === total ? `${total} sample filings` : `${state.rows.length} of ${total} sample filings`;
  tbody.innerHTML = state.rows.map(rowHtml).join('') ||
    `<tr class="pt-row-msg"><td colspan="7">No sample filing matches these filters. <button type="button" class="btn btn-ghost btn-small" id="bk-empty-reset">Clear filters</button></td></tr>`;
  $$('.pt-sort').forEach(b => {
    const [key, dir] = state.sort.split(':');
    b.classList.toggle('is-sorted', b.dataset.sort === key);
    if (b.dataset.sort === key) b.dataset.dir = dir; else delete b.dataset.dir;
  });
}

// ── drawer ──
function openDrawer(r) {
  state.selected = r.id;
  $('#bk-drawer-body').innerHTML = `
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

async function load() {
  try {
    state.data = await api(SAMPLE_URL);
  } catch {
    $('#bk-rows').innerHTML = '<tr class="pt-row-msg"><td colspan="7">The sample file did not load. Reload the page to try again.</td></tr>';
    return;
  }
  const rows = state.data.filings || [];
  fillSelect($('#bk-state'), [...new Set(rows.map(r => r.state))].sort());
  fillSelect($('#bk-chapter'), state.data.chapters || [...new Set(rows.map(r => r.chapter))]);
  fillSelect($('#bk-industry'), state.data.industries || [...new Set(rows.map(r => r.industry))]);
  render();
}

if ($('#bk-rows')) { wire(); initAsk(); load(); }
