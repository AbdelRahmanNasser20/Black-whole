// static/site/platform/finds.js — /platform/finds: saved searches, each with its newest lots.
// Two modes, one page (see templates/platform_finds.html):
//   browser  no account. Searches live in finds_store (localStorage); lots come from the public /deals/api/lots.
//   account  signed in. Searches are /platform/api/finds rows; lots come from /platform/api/finds/{id}/lots.
// Both return the same public rows, so the views are the shared ones (static/deals/views.js, static/ui/glmap.js).
// "New" = first seen in the last 24 hours. "Since your last visit" is shown when the last visit is known.
// Nothing is filtered for policy here: the server already did it (public_deals.py).
import {api, esc, fmt, toast, renderEmpty, renderError, skeleton} from '../../ui/state.js';
import {tickTimers} from '../../ui/card.js';
import {renderCards, renderTable, mountMap} from '../../deals/views.js';
import {STARTERS, readPending, addPending, removePending, touchPending, patchPending, toQuery, describe, clean,
        MAX_SEARCHES} from './finds_store.js';
import {mergeAll} from './merge.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const root = $('#finds');
const signedIn = root.dataset.signedIn === '1';
const DAY_MS = 24 * 3600 * 1000;
const ROWS = 50;
const VIEWS = ['cards', 'table', 'map'];
const VIEW_STORE = 'finds.view';

const st = {finds: [], sel: null, scope: 'new', view: 'cards', lots: new Map(), siteNames: {}, max: MAX_SEARCHES};
try { const v = localStorage.getItem(VIEW_STORE); if (VIEWS.includes(v)) st.view = v; } catch { /* blocked storage */ }

let map = null, mapFailed = false;

const siteName = (k) => st.siteNames[k] || String(k || '').replace(/_/g, ' ');
const label = (f) => f.name || describe(f, siteName);
const seenMs = (row) => { const t = Date.parse(row.first_seen_at || ''); return Number.isFinite(t) ? t : 0; };

/* ── data ─────────────────────────────────────────────────────────────────── */
async function loadFinds() {
  if (!signedIn) { st.finds = readPending(); return; }
  const d = await api('/platform/api/finds', {credentials: 'include'});
  st.finds = d.finds || [];
  if (d.max) st.max = d.max;
}

// → {rows, total, lastViewed (ms | 0), error}
async function fetchLots(find) {
  if (signedIn) {
    const d = await api(`/platform/api/finds/${find.id}/lots`, {credentials: 'include'});
    return {rows: d.rows || [], total: d.total || 0, lastViewed: Date.parse(d.last_viewed_at || '') || 0};
  }
  const p = toQuery(find);
  p.set('sort', 'newest'); p.set('per_page', String(ROWS));
  const d = await api('/deals/api/lots?' + p.toString());
  return {rows: d.rows || [], total: d.total || 0, lastViewed: Date.parse(find.last_viewed_at || '') || 0};
}

function counts(entry) {
  const now = Date.now();
  const fresh = entry.rows.filter((r) => seenMs(r) > now - DAY_MS);
  const since = entry.lastViewed ? entry.rows.filter((r) => seenMs(r) > entry.lastViewed).length : null;
  return {fresh, since};
}

async function ensureLots(find, {force = false} = {}) {
  if (!force && st.lots.has(find.id)) return st.lots.get(find.id);
  let entry;
  try { entry = await fetchLots(find); }
  catch (err) { entry = {rows: [], total: 0, lastViewed: 0, error: err}; }
  st.lots.set(find.id, entry);
  paintTabs();
  return entry;
}

function markSeen(find) {
  if (signedIn) fetch(`/platform/api/finds/${find.id}/seen`, {method: 'POST', credentials: 'include'}).catch(() => {});
  else touchPending(find.id);
}

/* ── tabs: one per saved search, with its count of new lots ───────────────── */
function paintTabs() {
  const host = $('#finds-tabs');
  host.innerHTML = st.finds.map((f) => {
    const entry = st.lots.get(f.id);
    const n = entry && !entry.error ? counts(entry).fresh.length : null;
    const badge = n === null ? '<span class="finds-badge is-loading">…</span>'
      : `<span class="finds-badge${n ? ' is-new' : ''}" title="${n} new in the last 24 hours">${n >= ROWS ? ROWS + '+' : n}</span>`;
    const on = f.id === st.sel;
    return `<button type="button" class="finds-tab${on ? ' is-active' : ''}" data-id="${esc(String(f.id))}" aria-pressed="${on}">`
      + `<span class="finds-tab-name">${esc(label(f))}</span>${badge}</button>`;
  }).join('');
  $('#finds-empty').hidden = st.finds.length > 0;
  $('#finds-panel').hidden = st.finds.length === 0;
}

/* ── the selected search ──────────────────────────────────────────────────── */
function selected() { return st.finds.find((f) => f.id === st.sel) || null; }

function shownRows(entry) {
  return st.scope === 'new' ? counts(entry).fresh : entry.rows;
}

function popupHtml(r) {
  const place = [r.city, r.state].filter(Boolean).map(esc).join(', ');
  const bids = r.bid_count ?? 0;
  const ext = r.url || r.govdeals_url;
  const links = [];
  if (r.viewer_url) links.push(`<a href="${esc(r.viewer_url)}">View lot</a>`);
  if (ext) links.push(`<a href="${esc(ext)}" target="_blank" rel="noopener">${esc(r.source_name || 'GovDeals')} ↗</a>`);
  return `<strong>${esc(r.title || 'Untitled lot')}</strong><br>${place || '—'}<br>`
    + `<span class="mono">${esc(fmt.money(r.current_bid))} · ${esc(fmt.int(bids))} bid${bids === 1 ? '' : 's'} · ⏱ ${esc(fmt.endsIn(r.end_utc))}</span>`
    + (links.length ? `<br>${links.join(' · ')}` : '');
}

async function paintMap(rows) {
  const note = $('#finds-map-note');
  const placed = rows.filter((r) => r.lat != null && r.lng != null);
  if (mapFailed) { note.textContent = 'The map could not load. The Cards and Table views show the same lots.'; return; }
  try {
    if (!map) map = await mountMap($('#finds-map'), {styleUrl: root.dataset.styleUrl || ''});
  } catch {
    mapFailed = true;
    $('#finds-map').hidden = true;
    note.textContent = 'The map could not load. The Cards and Table views show the same lots.';
    return;
  }
  map.invalidateSize();
  map.setPoints(placed.map((r) => ({lat: r.lat, lng: r.lng, title: r.title || '', popup: popupHtml(r)})));
  map.fit();
  const off = rows.length - placed.length;
  note.textContent = !rows.length ? 'No lots to show on the map.'
    : `${placed.length} lot${placed.length === 1 ? '' : 's'} on the map` + (off ? `, ${off} with no known location.` : '.');
}

function paintLots(find, entry) {
  const cards = $('#finds-cards'), table = $('#finds-table'), mapWrap = $('#finds-map-wrap');
  cards.hidden = st.view !== 'cards'; table.hidden = st.view !== 'table'; mapWrap.hidden = st.view !== 'map';
  const target = st.view === 'table' ? table : cards;
  if (entry.error) {
    cards.hidden = false; table.hidden = true; mapWrap.hidden = true;
    const bad = entry.error.status === 400;
    renderError(cards, {
      message: bad ? 'This search has a filter the feed no longer takes. Delete it and save it again.'
        : entry.error.status === 429 ? 'Too many requests. Wait a minute, then try again.'
        : 'The lots did not load.',
      retry: () => select(find.id, {force: true})});
    $('#finds-count').textContent = '';
    return;
  }
  const {fresh, since} = counts(entry);
  const rows = shownRows(entry);
  const more = entry.total > entry.rows.length;
  const parts = [`${fresh.length >= ROWS ? ROWS + '+' : fresh.length} new in the last 24 hours`];
  if (since !== null) parts.push(`${since} since your last visit`);
  parts.push(`${fmt.int(entry.total)} open lot${entry.total === 1 ? '' : 's'} match`);
  $('#finds-count').textContent = parts.join(' · ');

  if (!rows.length) {
    cards.hidden = false; table.hidden = true; mapWrap.hidden = true;
    if (st.scope === 'new' && entry.rows.length) {
      renderEmpty(cards, {glyph: '◌', title: 'No new lots in the last 24 hours',
        body: `${fmt.int(entry.total)} open lot${entry.total === 1 ? '' : 's'} match this search. Press All to see them.`});
    } else {
      renderEmpty(cards, {glyph: '◌', title: 'No open lots match this search',
        body: 'New lots show here when an auction site lists one.'});
    }
    return;
  }
  if (st.view === 'map') { paintMap(rows); return; }
  target.innerHTML = st.view === 'table' ? renderTable(rows) : renderCards(rows);
  if (st.scope === 'all' && more) {
    target.insertAdjacentHTML('beforeend',
      `<p class="finds-more">Newest ${rows.length} of ${esc(fmt.int(entry.total))}. <a href="/deals?${esc(toQuery(find).toString())}&sort=newest">See all in Deals →</a></p>`);
  }
  tickTimers(target);
}

function paintPanel(find) {
  $('#finds-name').textContent = label(find);
  $('#finds-open').href = '/deals?' + toQuery(find).toString() + '&sort=newest';
  const on = signedIn ? !!find.alert : !!find.want_alert;
  const btn = $('#finds-alert');
  btn.textContent = signedIn ? `Email alert: ${on ? 'on' : 'off'}` : 'Get email alerts';
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.classList.toggle('is-on', signedIn && on);
  $$('#finds-scope .seg-btn').forEach((b) => { const a = b.dataset.scope === st.scope; b.classList.toggle('is-active', a); b.setAttribute('aria-pressed', a); });
  $$('#finds-view .seg-btn').forEach((b) => { const a = b.dataset.view === st.view; b.classList.toggle('is-active', a); b.setAttribute('aria-pressed', a); });
}

async function select(id, {force = false} = {}) {
  st.sel = id;
  const find = selected();
  paintTabs();
  if (!find) return;
  paintPanel(find);
  if (force || !st.lots.has(id)) {
    $('#finds-cards').hidden = false; $('#finds-table').hidden = true; $('#finds-map-wrap').hidden = true;
    $('#finds-cards').innerHTML = skeleton('card', 6);
    $('#finds-count').textContent = 'Reading the feed…';
  }
  const entry = await ensureLots(find, {force});
  if (st.sel !== id) return;                 // another search was picked while this one loaded
  paintLots(find, entry);
  if (!entry.error) markSeen(find);          // counts above used the previous visit; now move it forward
}

/* ── actions ──────────────────────────────────────────────────────────────── */
function say(text, ok = true) {
  const el = $('#finds-add-msg');
  el.textContent = text; el.hidden = !text; el.classList.toggle('is-err', !ok);
}

async function addSearch(filters) {
  // Check what was typed first: clean() drops what the server would refuse, and a dropped
  // word or state must be said, not saved as a wider search.
  const typedQ = String((filters && filters.q) || '').trim();
  const typedState = String((filters && filters.state) || '').trim();
  if (typedQ.length === 1) { say('Use a search word of 2 letters or more.', false); return; }
  if (typedState && !/^[A-Za-z]{2}$/.test(typedState)) { say('State is the 2-letter code, for example GA.', false); return; }
  const f = clean(filters);
  if (!f) { say('Type a search word, a state, or a max bid.', false); return; }
  if (signedIn) {
    try {
      const made = await api('/platform/api/finds', {method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(f)});
      if (!st.finds.some((it) => it.id === made.id)) st.finds.push(made);   // an identical search comes back as the one on file
      say(made.duplicate ? 'That search is already saved.'
        : made.alert_held ? 'Saved. The email alert stays off until this search can be stored in full.'
        : made.degraded ? 'Saved. Only the search word, state, site and max bid were stored for now.' : 'Search saved.');
      $('#finds-add').reset();
      return select(made.id);
    } catch (err) {
      // The answer's own code decides the message; a status alone can mean more than one thing.
      const code = err.detail && typeof err.detail === 'object' ? err.detail.error : null;
      say(code === 'alert_limit' ? `An account holds ${st.max} saved searches. Delete one first.`
        : code === 'keyword_required' ? 'Add a search word. A search with no word cannot be stored in an account yet.'
        : code === 'alert_needs_filter' ? 'An email alert needs a search word, a category, a state or a site.'
        : err.status === 422 ? 'The server did not take that search. Check the search word and the state.'
        : 'The search was not saved. Try again.', false);
      return;
    }
  }
  const r = addPending(f);
  if (!r.ok) { say(r.reason === 'full' ? `This browser holds ${MAX_SEARCHES} saved searches. Delete one first.` : 'This browser would not store the search.', false); return; }
  st.finds = readPending();
  const mine = st.finds.find((it) => toQuery(it).toString() === toQuery(f).toString());
  say(r.reason === 'duplicate' ? 'That search is already saved.' : 'Search saved in this browser.');
  $('#finds-add').reset();
  if (mine) select(mine.id);
}

async function removeSearch() {
  const find = selected();
  if (!find) return;
  if (signedIn) {
    try { await api(`/platform/api/finds/${find.id}`, {method: 'DELETE', credentials: 'include'}); }
    catch { toast('The search was not deleted. Try again.', 'err'); return; }
  } else removePending(find.id);
  st.finds = st.finds.filter((f) => f.id !== find.id);
  st.lots.delete(find.id);
  toast('Search deleted.', 'ok');
  if (st.finds.length) select(st.finds[0].id); else { st.sel = null; paintTabs(); }
}

async function toggleAlert() {
  const find = selected();
  if (!find) return;
  if (!signedIn) {
    // An alert needs an email on file. Remember the wish, then go to sign-in; merge.js turns it on after.
    patchPending(find.id, {want_alert: true});
    location.href = '/platform/signup';
    return;
  }
  const btn = $('#finds-alert');
  btn.disabled = true;
  try {
    const next = await api(`/platform/api/finds/${find.id}`, {method: 'PATCH', credentials: 'include',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({alert: !find.alert})});
    Object.assign(find, next);
    paintPanel(find);
    toast(find.alert ? 'Email alert on. New matching lots go to your email.' : 'Email alert off.', 'ok');
  } catch (err) {
    const code = err.detail && typeof err.detail === 'object' ? err.detail.error : null;
    toast(code === 'alert_needs_filter' ? 'An email alert needs a search word, a category, a state or a site.'
      : 'The alert did not change. Try again.', 'err');
  }
  btn.disabled = false;
}

/* ── wiring ───────────────────────────────────────────────────────────────── */
$('#finds-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('.finds-tab');
  if (b) { const f = st.finds.find((x) => String(x.id) === b.dataset.id); if (f) select(f.id); }
});
$('#finds-scope').addEventListener('click', (e) => {
  const b = e.target.closest('.seg-btn'); if (!b) return;
  st.scope = b.dataset.scope;
  const find = selected(), entry = find && st.lots.get(find.id);
  if (find && entry) { paintPanel(find); paintLots(find, entry); }
});
$('#finds-view').addEventListener('click', (e) => {
  const b = e.target.closest('.seg-btn'); if (!b || !VIEWS.includes(b.dataset.view)) return;
  st.view = b.dataset.view;
  try { localStorage.setItem(VIEW_STORE, st.view); } catch { /* blocked storage */ }
  const find = selected(), entry = find && st.lots.get(find.id);
  if (find && entry) { paintPanel(find); paintLots(find, entry); }
});
$('#finds-alert').addEventListener('click', toggleAlert);
$('#finds-delete').addEventListener('click', removeSearch);
// Starter searches on the empty page: one click saves it (browser or account) like the form would.
const starters = $('#finds-starters');
if (starters) {
  starters.innerHTML = STARTERS.map((f, i) => `<button type="button" class="chip" data-starter="${i}">${esc(f.name)}</button>`).join('');
  starters.addEventListener('click', (e) => {
    const b = e.target.closest('[data-starter]'); if (!b) return;
    const f = STARTERS[Number(b.dataset.starter)]; if (f) addSearch({...f});
  });
}
$('#finds-add').addEventListener('submit', (e) => {
  e.preventDefault();
  addSearch(Object.fromEntries(new FormData(e.target).entries()));
});
setInterval(() => tickTimers(root), 30000);

async function start() {
  // Site names for the labels. A failure only means the labels show the site key.
  api('/deals/api/facets').then((d) => {
    for (const s of (d && d.sites) || []) st.siteNames[s.value] = s.name;
    paintTabs(); const f = selected(); if (f) $('#finds-name').textContent = label(f);
  }).catch(() => {});
  if (signedIn) {
    const moved = await mergeAll();          // what the browser kept before sign-in → the account
    if (moved.searches) toast(`${moved.searches} saved search${moved.searches === 1 ? '' : 'es'} moved to your account.`, 'ok');
    const notes = [];
    if (moved.held) notes.push(`${moved.held} email alert${moved.held === 1 ? ' stays' : 's stay'} off until the search can be stored in full.`);
    if (moved.capped) notes.push(`Your account is full (${st.max} saved searches). ${moved.kept} more are still in this browser.`);
    for (const r of moved.rejected) {
      notes.push(`“${describe(r.search)}” could not be saved to your account`
        + (r.error === 'keyword_required' ? ' yet: it needs a search word.' : '.') + ' It is still in this browser.');
    }
    if (notes.length) say(notes.join(' '), false);
  }
  try { await loadFinds(); }
  catch {
    renderError($('#finds-tabs'), {message: 'Your saved searches did not load.', retry: start});
    return;
  }
  paintTabs();
  if (!st.finds.length) return;
  await select(st.finds[0].id);
  for (const f of st.finds) if (!st.lots.has(f.id)) await ensureLots(f);   // fill the other badges, one at a time
}
start();
