// static/site/platform/deals.js — GET /platform/deals. ES module.
//
//   Feed     REAL: GET /platform/api/auctions (server-side filters, paging, facets). The read model and the
//            public_deals policy live in automation/web/platform_api.py; the contract has no photo field and
//            this module never renders one. `ok:false` or an empty open feed is shown as a direction, never a
//            blank table, and closed lots never stand in for open ones.
//   Sources  the names are already in the HTML (planned ones labelled Planned, disabled). This adds each
//            adapter-backed site's true status from GET /platform/api/sites: Live or Paused, plus the lot count
//            the current facets report. A failed read leaves the names alone and claims nothing.
//   Drawer   one lot's detail with a link to the source page. Memory only; nothing is saved.
import {api, esc, fmt} from '../../ui/state.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const FEED_URL = '/platform/api/auctions';
const SITES_URL = '/platform/api/sites';
const PER_PAGE = 50;

const f = {q: '', status: 'open', site: '', category: '', state: '', ending: '', no_bids: false, max_bid: '', sort: 'ending'};
const view = {items: [], total: 0, page: 1, ok: null, facets: null, selected: null, busy: null};

function money(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const d = Number.isInteger(n) ? 0 : 2;
  return '$' + n.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function query(page) {
  const p = new URLSearchParams({status: f.status, sort: f.sort, page: String(page), per_page: String(PER_PAGE)});
  if (f.q) p.set('q', f.q);
  if (f.site) p.set('site', f.site);
  if (f.category) p.set('category', f.category);
  if (f.state) p.set('state', f.state);
  if (f.ending) p.set('ending', f.ending);
  if (f.no_bids) p.set('no_bids', '1');
  if (f.max_bid !== '' && f.max_bid != null) p.set('max_bid', String(f.max_bid));
  return `${FEED_URL}?${p}`;
}

// ── facets ──
function fillFacet(sel, rows, current, label) {
  const keep = sel.options[0];
  sel.innerHTML = ''; sel.appendChild(keep);
  for (const r of rows || []) {
    const o = document.createElement('option');
    o.value = r.key; o.textContent = `${label ? label(r) : r.name} (${fmt.int(r.count)})`;
    sel.appendChild(o);
  }
  sel.value = current && [...sel.options].some(o => o.value === current) ? current : '';
  sel.disabled = !(rows && rows.length);
}
function paintFacets(facets) {
  if (!facets) return;
  fillFacet($('#pd-site'), facets.sites, f.site);
  fillFacet($('#pd-category'), facets.categories, f.category);
  fillFacet($('#pd-state'), facets.states, f.state);
  const bySite = Object.fromEntries((facets.sites || []).map(s => [s.key, s.count]));
  $$('.pd-source[data-planned="0"]').forEach(b => {
    const n = bySite[b.dataset.site];
    b.querySelector('.pd-source-n').textContent = n ? `${fmt.int(n)} ${f.status}` : '';
    b.classList.toggle('is-active', f.site === b.dataset.site);
    b.setAttribute('aria-pressed', String(f.site === b.dataset.site));
  });
}

// ── rows ──
function rowHtml(it) {
  const closed = it.status === 'closed';
  const bids = it.bid_count;
  const ends = closed ? (it.closed_at ? fmt.date(it.closed_at) : '—') : fmt.endsIn(it.ends_at);
  const urgent = !closed && it.ends_at && (new Date(it.ends_at).getTime() - Date.now()) < 86400000;
  return `<tr class="pt-row${view.selected === it.id ? ' is-selected' : ''}" data-id="${esc(it.id)}" tabindex="0">
    <td class="pt-c-main"><span class="pt-row-main">${esc(it.title)}</span></td>
    <td data-label="Site">${esc(it.site_name || it.site)}</td>
    <td class="dim" data-label="Category">${esc(it.category || '—')}</td>
    <td data-label="Where">${esc([it.city, it.state].filter(Boolean).join(', ') || '—')}</td>
    <td class="num" data-label="${closed ? 'Final' : 'Current bid'}">${esc(money(closed ? it.final_price : it.current_bid))}</td>
    <td class="num${bids === 0 ? ' pt-zero' : ''}" data-label="Bids">${bids == null ? '—' : esc(fmt.int(bids))}</td>
    <td class="mono${urgent ? ' pt-urgent' : ''}" data-label="${closed ? 'Closed' : 'Ends'}">${esc(ends)}</td>
  </tr>`;
}
function emptyHtml() {
  if (view.ok === false) return '<tr class="pt-row-msg"><td colspan="7">The feed did not answer. Reload the page in a minute.</td></tr>';
  if (f.status === 'open' && !hasFilters()) {
    return `<tr class="pt-row-msg"><td colspan="7">None are open in the feed right now.
      <button type="button" class="btn btn-ghost btn-small" id="pd-show-closed">Show closed auctions</button></td></tr>`;
  }
  return `<tr class="pt-row-msg"><td colspan="7">No ${f.status} auction matches these filters.
    <button type="button" class="btn btn-ghost btn-small" id="pd-empty-reset">Clear filters</button></td></tr>`;
}
function hasFilters() {
  return !!(f.q || f.site || f.category || f.state || f.ending || f.no_bids || f.max_bid !== '');
}
function renderList() {
  $('#pd-rows').innerHTML = view.items.map(rowHtml).join('') || emptyHtml();
  $('#pd-more').hidden = view.items.length >= view.total;
  const n = view.total;
  $('#pd-count').textContent = view.ok ? `${fmt.int(n)} ${f.status} auction${n === 1 ? '' : 's'}` : '';
  const truth = $('#pd-truth');
  if (view.ok === false) { truth.dataset.mode = ''; truth.textContent = 'The feed did not answer; nothing is shown as open.'; }
  else if (f.status === 'open' && n > 0) { truth.dataset.mode = 'live'; truth.textContent = `Live data: ${fmt.int(n)} open auction${n === 1 ? '' : 's'} across the sites marked live.`; }
  else if (f.status === 'open') { truth.dataset.mode = ''; truth.textContent = 'Real data, but no open auction in the feed right now.'; }
  else { truth.dataset.mode = ''; truth.textContent = `Real data: recorded outcomes of ${fmt.int(n)} closed auction${n === 1 ? '' : 's'}.`; }
  $('#pd-tag').hidden = !(view.ok && f.status === 'open' && n > 0);
}

// ── drawer ──
function openDrawer(it) {
  view.selected = it.id;
  const closed = it.status === 'closed';
  $('#pd-drawer-tag').textContent = closed ? 'Closed auction' : 'Open auction';
  $('#pd-drawer-tag').className = `pt-chip ${closed ? '' : 'pt-chip--live'}`;
  $('#pd-drawer-body').innerHTML = `
    <div class="pt-drawer-kicker">${esc(it.site_name || it.site)} · ${esc(it.id)}</div>
    <h2 class="pt-drawer-title" id="pd-drawer-title">${esc(it.title)}</h2>
    <dl>
      <div><dt>${closed ? 'Final price' : 'Current bid'}</dt><dd class="mono">${esc(money(closed ? it.final_price : it.current_bid))}</dd></div>
      <div><dt>Bids</dt><dd class="mono">${it.bid_count == null ? '—' : esc(fmt.int(it.bid_count))}</dd></div>
      <div><dt>${closed ? 'Closed' : 'Ends'}</dt><dd class="mono">${esc(closed ? fmt.date(it.closed_at) : `${fmt.endsIn(it.ends_at)} (${fmt.date(it.ends_at)})`)}</dd></div>
      <div><dt>Where</dt><dd>${esc([it.city, it.state].filter(Boolean).join(', ') || '—')}</dd></div>
      <div><dt>Category</dt><dd>${esc(it.category || '—')}</dd></div>
      <div><dt>Site</dt><dd>${esc(it.site_name || it.site)}</dd></div>
    </dl>
    <div class="pt-drawer-actions">
      ${it.url ? `<a class="btn btn-primary btn-small pd-site-link" href="${esc(it.url)}" target="_blank" rel="noopener">OPEN ON ${esc((it.site_name || it.site).toUpperCase())}</a>` : ''}
      <button type="button" class="btn btn-ghost btn-small" disabled title="Not built yet">LANDED COST</button>
    </div>
    <p class="pt-drawer-note">Photos stay on the auction site. Landed cost per unit and watch alerts are the operator desk, not this page.</p>`;
  $('#pd-drawer').hidden = false; $('#pd-scrim').hidden = false;
  renderList();
  $('#pd-drawer').focus();
}
function closeDrawer() {
  view.selected = null;
  $('#pd-drawer').hidden = true; $('#pd-scrim').hidden = true;
  renderList();
}

// ── load ──
async function load({append = false} = {}) {
  if (view.busy) view.busy.abort();
  const ac = new AbortController(); view.busy = ac;
  const page = append ? view.page + 1 : 1;
  $('#pd-feed').setAttribute('aria-busy', 'true');
  try {
    const res = await api(query(page), {signal: ac.signal});
    if (ac.signal.aborted) return;
    view.ok = !!res.ok; view.total = res.total || 0; view.page = page;
    view.items = append ? [...view.items, ...(res.items || [])] : (res.items || []);
    if (!append) paintFacets(res.facets);
  } catch (err) {
    if (ac.signal.aborted) return;
    view.ok = false; view.total = 0; view.items = append ? view.items : [];
  } finally {
    if (!ac.signal.aborted) { $('#pd-feed').removeAttribute('aria-busy'); view.busy = null; }
  }
  renderList();
}

async function loadSites() {
  let rows;
  try { rows = await api(SITES_URL); } catch { return; }        // names stay; nothing is claimed
  if (!Array.isArray(rows)) return;
  for (const s of rows) {
    const b = $(`.pd-source[data-site="${CSS.escape(s.key)}"][data-planned="0"]`);
    const chip = b && b.querySelector('[data-status]');
    if (!chip) continue;
    const status = s.status === 'live' ? 'live' : 'paused';
    chip.className = `pt-chip pt-chip--${status}`;
    chip.textContent = status === 'live' ? 'Live' : 'Paused';
    b.title = status === 'live' ? 'Observed in the last 24 hours' : 'Adapter exists; not observed in the last 24 hours';
  }
}

// ── wire ──
function setFilters(patch) { Object.assign(f, patch); load(); }
function wire() {
  let t;
  $('#pd-q').addEventListener('input', e => { clearTimeout(t); t = setTimeout(() => setFilters({q: e.target.value.trim()}), 250); });
  $('#pd-form').addEventListener('submit', e => e.preventDefault());
  $('#pd-site').addEventListener('change', e => setFilters({site: e.target.value}));
  $('#pd-category').addEventListener('change', e => setFilters({category: e.target.value}));
  $('#pd-state').addEventListener('change', e => setFilters({state: e.target.value}));
  $('#pd-ending').addEventListener('change', e => setFilters({ending: e.target.value}));
  $('#pd-nobids').addEventListener('change', e => setFilters({no_bids: e.target.checked}));
  $('#pd-maxbid').addEventListener('change', e => setFilters({max_bid: e.target.value === '' ? '' : Number(e.target.value)}));
  $('#pd-sort').addEventListener('change', e => setFilters({sort: e.target.value}));
  const setStatus = status => {
    $$('#pd-status .pt-seg-btn').forEach(b => {
      const on = b.dataset.status === status;
      b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on));
    });
    setFilters({status});
  };
  $$('#pd-status .pt-seg-btn').forEach(b => b.addEventListener('click', () => setStatus(b.dataset.status)));
  const reset = () => {
    $('#pd-form').reset();
    setFilters({q: '', site: '', category: '', state: '', ending: '', no_bids: false, max_bid: '', sort: 'ending'});
  };
  $('#pd-reset').addEventListener('click', reset);
  $$('.pd-source[data-planned="0"]').forEach(b => b.addEventListener('click', () => {
    const site = f.site === b.dataset.site ? '' : b.dataset.site;
    $('#pd-site').value = site;
    setFilters({site});
  }));
  $('#pd-more').addEventListener('click', () => load({append: true}));
  $('#pd-rows').addEventListener('click', e => {
    if (e.target.closest('#pd-show-closed')) { setStatus('closed'); return; }
    if (e.target.closest('#pd-empty-reset')) { reset(); return; }
    const tr = e.target.closest('tr.pt-row');
    if (!tr) return;
    const it = view.items.find(x => x.id === tr.dataset.id);
    if (it) openDrawer(it);
  });
  $('#pd-rows').addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr.pt-row');
    if (!tr) return;
    e.preventDefault();
    const it = view.items.find(x => x.id === tr.dataset.id);
    if (it) openDrawer(it);
  });
  $('#pd-drawer-close').addEventListener('click', closeDrawer);
  $('#pd-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#pd-drawer').hidden) closeDrawer(); });
}

if ($('#pd-rows')) { wire(); load(); loadSites(); }
