/* automation/web/static/deals_public.js — Surplus Radar page logic.
 * All state lives in the URL query string (shareable links, back button
 * works). One fetch per state change; facets once (5-min server cache). */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (t) => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const money = (v) => v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 2});
  const PER_PAGE = window.SR_PER_PAGE || [25, 50, 100];
  const KEYS = ['q', 'category', 'state', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price', 'sort', 'dir', 'page', 'per_page', 'bbox'];
  const DEFAULTS = {status: 'active', sort: 'ends', page: '1', per_page: String(PER_PAGE[0])};

  const st = Object.assign({}, DEFAULTS, Object.fromEntries(
    KEYS.map(k => [k, new URLSearchParams(location.search).get(k)]).filter(([, v]) => v)));
  let map = null, mapOn = false, facets = null, lastBody = null;

  function qs(extra = {}) {
    const p = new URLSearchParams();
    for (const k of KEYS) { const v = (extra[k] !== undefined ? extra[k] : st[k]); if (v !== undefined && v !== null && v !== '') p.set(k, v); }
    return p;
  }
  function pushUrl() {
    const p = qs({bbox: ''});  // bbox is transient — never in the shareable URL
    for (const k of Object.keys(DEFAULTS)) if (p.get(k) === DEFAULTS[k]) p.delete(k);
    history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
  }
  function set(patch, {resetPage = true} = {}) {
    Object.assign(st, patch);
    if (resetPage) st.page = '1';
    pushUrl(); syncControls(); load();
    if (mapOn) loadPins();
  }

  // ── controls ──────────────────────────────────────────────────────────
  function syncControls() {
    $('#sr-q').value = st.q || '';
    $$('.sr-seg').forEach(seg => {
      const cur = st[seg.dataset.key] ?? '';
      $$('button', seg).forEach(b => b.classList.toggle('on', (b.dataset.value || '') === String(cur)));
    });
    $('#sr-sort').value = st.dir ? `${st.sort}:${st.dir}` : st.sort;
    $('#sr-state').value = st.state || '';
    $('#sr-min-price').value = st.min_price || '';
    $('#sr-max-price').value = st.max_price || '';
    $$('.sr-pill').forEach(p => p.classList.toggle('on', (p.dataset.cat || '') === (st.category || '')));
    $$('.sr-cat').forEach(p => p.classList.toggle('on', (p.dataset.cat || '') === (st.category || '')));
  }
  let qTimer = null;
  $('#sr-q').addEventListener('input', (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => set({q: e.target.value.trim()}), 300); });
  $$('.sr-seg').forEach(seg => seg.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    set({[seg.dataset.key]: b.dataset.value});
  }));
  $('#sr-sort').addEventListener('change', (e) => { const [sort, dir] = e.target.value.split(':'); set({sort, dir: dir || ''}); });
  $('#sr-state').addEventListener('change', (e) => set({state: e.target.value}));
  let priceTimer = null;
  ['#sr-min-price', '#sr-max-price'].forEach(sel => $(sel).addEventListener('input', () => {
    clearTimeout(priceTimer);
    priceTimer = setTimeout(() => set({min_price: $('#sr-min-price').value, max_price: $('#sr-max-price').value}), 400);
  }));
  $('#sr-clear').addEventListener('click', () => {
    for (const k of KEYS) delete st[k];
    Object.assign(st, DEFAULTS); set({});
  });
  $('#sr-pills').addEventListener('click', (e) => { const p = e.target.closest('.sr-pill'); if (p) set({category: p.dataset.cat}); });
  $('#sr-cats').addEventListener('click', (e) => { const p = e.target.closest('.sr-cat'); if (p) set({category: p.dataset.cat}); });

  // ── facets + portfolio stats ──────────────────────────────────────────
  async function loadFacets() {
    try {
      const r = await fetch('/deals/api/facets'); if (!r.ok) throw new Error(r.status);
      facets = await r.json();
    } catch (e) { facets = {categories: [], states: [], stats: {}}; }
    const s = facets.stats || {};
    $$('#sr-stats [data-stat]').forEach(el => { const v = s[el.dataset.stat]; el.textContent = v == null ? '—' : Number(v).toLocaleString(); });
    const cats = facets.categories || [];
    const total = cats.reduce((a, c) => a + Number(c.count || 0), 0);
    $('#sr-pills').innerHTML = `<span class="sr-pill" data-cat="">all<small>${total.toLocaleString()}</small></span>` +
      cats.slice(0, 10).map(c => `<span class="sr-pill" data-cat="${esc(c.value)}">${esc(c.value.replace(/_/g, ' '))}<small>${Number(c.count).toLocaleString()}</small></span>`).join('');
    $('#sr-cats').innerHTML = `<div class="sr-cat" data-cat="">all categories<small>${total.toLocaleString()}</small></div>` +
      cats.map(c => `<div class="sr-cat" data-cat="${esc(c.value)}">${esc(c.value.replace(/_/g, ' '))}<small>${Number(c.count).toLocaleString()}</small></div>`).join('');
    const sel = $('#sr-state');
    (facets.states || []).forEach(f => { const o = document.createElement('option'); o.value = f.value; o.textContent = `${f.value} (${f.count})`; sel.appendChild(o); });
    syncControls();
  }

  // ── list ──────────────────────────────────────────────────────────────
  function endsCell(iso, closed) {
    if (!iso) return '—';
    const ms = new Date(iso) - Date.now();
    if (closed || ms < 0) return `<span class="sr-sub">${new Date(iso).toLocaleDateString()}</span>`;
    const h = ms / 36e5;
    const txt = h < 1 ? `${Math.max(1, Math.round(ms / 6e4))} min` : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`;
    return `<span class="${h < 6 ? 'sr-ends-red' : h < 24 ? 'sr-ends-yellow' : ''}">${txt}</span>`;
  }
  function rowHtml(r) {
    const closed = !!r.outcome_complete;
    const bid = closed && r.final_bid != null ? r.final_bid : r.current_bid;
    const qty = `${r.quantity}${r.quantity_source === 'default' ? '<div class="sr-qty-src">n/a</div>' : ''}`;
    return `<tr>
      <td class="sr-title"><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
        <div class="sr-sub">${esc(r.native_category_name || '')}${closed ? ` · closed${r.outcome ? ' · ' + esc(r.outcome).replace(/_/g, ' ') : ''}` : ''}</div></td>
      <td>${esc((r.canonical_category || '—').replace(/_/g, ' '))}</td>
      <td>${esc(r.city || '')}${r.state ? ', ' + esc(r.state) : ''}</td>
      <td class="num">${r.bid_count ?? '—'}</td>
      <td class="num">${money(bid)}</td>
      <td class="num">${qty}</td>
      <td class="num">${money(r.unit_bid)}</td>
      <td class="num">${money(r.unit_landed)}</td>
      <td>${endsCell(r.end_utc, closed)}</td>
      <td><a class="sr-sub" href="${esc(r.viewer_url)}" title="our archived copy (text only)">⧉</a></td>
    </tr>`;
  }
  function cardHtml(r) {
    return `<div class="sr-card">
      <div class="sr-title"><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a></div>
      <div class="sr-card-row"><span>${esc(r.city || '')}${r.state ? ', ' + esc(r.state) : ''}</span><span>${endsCell(r.end_utc, !!r.outcome_complete)}</span></div>
      <div class="sr-card-row"><span>${r.bid_count ?? 0} bids · ${money(r.current_bid)}</span><span>qty ${r.quantity} · ${money(r.unit_bid)}/unit</span></div>
    </div>`;
  }
  function pagerHtml(b, id) {
    const first = (b.page - 1) * b.per_page + 1, last = Math.min(b.total, b.page * b.per_page);
    return `<span class="sr-total">PAGE ${b.page} OF ${b.pages.toLocaleString()} · ${b.total.toLocaleString()} LOTS${b.total ? ` · ${first}–${last}` : ''}${st.bbox ? ' · IN MAP VIEW' : ''}</span>
      <button data-go="1" ${b.page <= 1 ? 'disabled' : ''}>«</button>
      <button data-go="${b.page - 1}" ${b.page <= 1 ? 'disabled' : ''}>‹ prev</button>
      <button data-go="${b.page + 1}" ${b.page >= b.pages ? 'disabled' : ''}>next ›</button>
      <button data-go="${b.pages}" ${b.page >= b.pages ? 'disabled' : ''}>»</button>
      <input type="number" min="1" max="${b.pages}" value="${b.page}" aria-label="jump to page" data-jump>
      <select data-per-page aria-label="rows per page">${PER_PAGE.map(n => `<option value="${n}" ${n === b.per_page ? 'selected' : ''}>${n} / page</option>`).join('')}</select>`;
  }
  async function load() {
    const tbody = $('#sr-rows');
    tbody.innerHTML = '<tr><td colspan="10" class="sr-empty">Loading…</td></tr>';
    let body;
    try {
      const r = await fetch('/deals/api/lots?' + qs().toString());
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      body = await r.json();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="10" class="sr-empty">Could not load lots (${esc(e.message || e)}). Try again in a minute.</td></tr>`;
      return;
    }
    lastBody = body;
    tbody.innerHTML = body.rows.length ? body.rows.map(rowHtml).join('') : '<tr><td colspan="10" class="sr-empty">No lots match. Clear a filter.</td></tr>';
    $('#sr-cards').innerHTML = body.rows.map(cardHtml).join('');
    for (const id of ['#sr-pager-top', '#sr-pager-bottom']) $(id).innerHTML = pagerHtml(body, id);
    if (String(body.page) !== st.page) { st.page = String(body.page); pushUrl(); }
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('.sr-pager button[data-go]'); if (!b || b.disabled) return;
    set({page: b.dataset.go}, {resetPage: false}); window.scrollTo({top: $('#sr-pager-top').offsetTop - 120, behavior: 'smooth'});
  });
  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-jump]')) set({page: String(Math.max(1, Number(e.target.value) || 1))}, {resetPage: false});
    if (e.target.matches('[data-per-page]')) set({per_page: e.target.value});
  });

  // ── map (reuses admin_map.js; pins are exclusion-filtered server-side) ─
  async function loadPins() {
    const p = qs({page: '', per_page: '', sort: '', dir: '', bbox: ''});
    let data;
    try { const r = await fetch('/deals/api/pins?' + p.toString()); data = await r.json(); }
    catch (e) { $('#sr-map-note').textContent = 'map data unavailable'; return; }
    map.setPoints(data.points.map(pt => ({
      lat: pt.lat, lng: pt.lng, title: pt.title,
      popup: `<b>${esc(pt.title)}</b><br>${esc(pt.city || '')}, ${esc(pt.state || '')}<br>${money(pt.current_bid)} · ${pt.bid_count ?? 0} bids<br><a href="${esc(pt.govdeals_url)}" target="_blank" rel="noopener">GovDeals ↗</a>`,
    })));
    if (!st.bbox) map.fit();
    $('#sr-map-note').textContent = `${data.points.length.toLocaleString()} lots pinned${data.capped ? ' (first 5,000 — narrow the filters)' : ''} · pan to filter the list`;
  }
  $('#sr-map-toggle').addEventListener('click', async () => {
    mapOn = !mapOn;
    $('#sr-map-toggle').classList.toggle('on', mapOn);
    $('#sr-map-wrap').hidden = !mapOn;
    if (mapOn && !map) {
      map = await window.AdminMap.mount($('#sr-map'));
      map.onViewport(() => { st.bbox = map.bboxParam(); set({}, {resetPage: true}); });
    }
    if (mapOn) { map.invalidateSize && map.invalidateSize(); loadPins(); }
    else { st.bbox = ''; set({}); }
  });

  // ── about block: open on first visit, remember collapse ───────────────
  try {
    const about = $('#sr-about');
    about.open = localStorage.getItem('sr.about') !== 'closed';
    about.addEventListener('toggle', () => localStorage.setItem('sr.about', about.open ? 'open' : 'closed'));
  } catch (_) { $('#sr-about').open = true; }

  syncControls(); loadFacets(); load();
})();
