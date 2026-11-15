// static/site/platform/finds_store.js — a saved search, and where it lives with no account.
// A saved search is one set of the public /deals filters (the names /deals/api/lots takes) plus a name.
// With no account the list lives in this browser under SEARCH_KEY. After sign-in merge.js moves it to the
// account (POST /platform/api/finds) together with the browser stars (STAR_KEY, written by auctions.js).
// Storage can be blocked or full: every read and write is guarded, and the page works without it.
export const SEARCH_KEY = 'pf.pending_searches.v1';
export const STAR_KEY = 'pf.stars.v1';
export const FILTERS = ['q', 'category', 'state', 'site', 'max_bids', 'ending_within', 'min_price', 'max_price'];
export const MAX_SEARCHES = 10;      // the account cap (PLATFORM_MAX_ALERTS_PER_USER default)
const NUMERIC = ['max_bids', 'ending_within', 'min_price', 'max_price'];

// Keep only real filters, in one shape: strings trimmed, numbers as numbers, state upper, site lower.
export function clean(raw) {
  const out = {};
  for (const k of FILTERS) {
    let v = raw ? raw[k] : null;
    if (v === undefined || v === null || String(v).trim() === '') continue;
    if (NUMERIC.includes(k)) { v = Number(v); if (!Number.isFinite(v) || v < 0) continue; }
    else v = String(v).trim().slice(0, 80);
    if (k === 'state') v = v.toUpperCase();
    if (k === 'site') v = v.toLowerCase();
    out[k] = v;
  }
  return Object.keys(out).length ? out : null;
}

export function fromParams(params) {
  const raw = {};
  for (const k of FILTERS) raw[k] = params.get(k);
  return clean(raw);
}

export function toQuery(find) {
  const p = new URLSearchParams();
  for (const k of FILTERS) if (find[k] !== undefined && find[k] !== null && find[k] !== '') p.set(k, String(find[k]));
  return p;
}

export const sameFilters = (a, b) => toQuery(a).toString() === toQuery(b).toString();

const money = (n) => '$' + Number(n).toLocaleString('en-US', {maximumFractionDigits: 0});
// Plain words for one search: "forklift · GA · GSA Auctions · up to $5,000".
export function describe(find, siteName = (k) => k) {
  const parts = [];
  if (find.q) parts.push(`“${find.q}”`);
  if (find.category) parts.push(String(find.category).replace(/_/g, ' '));
  if (find.state) parts.push(find.state);
  if (find.site) parts.push(siteName(find.site));
  if (find.min_price != null && find.max_price != null) parts.push(`${money(find.min_price)} to ${money(find.max_price)}`);
  else if (find.max_price != null) parts.push(`up to ${money(find.max_price)}`);
  else if (find.min_price != null) parts.push(`from ${money(find.min_price)}`);
  if (find.max_bids != null) parts.push(Number(find.max_bids) === 0 ? 'no bids' : `${find.max_bids} bids or fewer`);
  if (find.ending_within != null) parts.push(`ends in ${find.ending_within} h`);
  return parts.join(' · ') || 'Every lot';
}

export function readPending() {
  try {
    const list = JSON.parse(window.localStorage.getItem(SEARCH_KEY) || '[]');
    if (!Array.isArray(list)) return [];
    return list.map((it) => {
      const f = clean(it);
      return f && it && typeof it.id === 'string'
        ? {...f, id: it.id, name: typeof it.name === 'string' ? it.name.slice(0, 80) : '',
           created_at: it.created_at || null, last_viewed_at: it.last_viewed_at || null,
           want_alert: it.want_alert === true}
        : null;
    }).filter(Boolean).slice(0, MAX_SEARCHES);
  } catch { return []; }
}

export function writePending(list) {
  try { window.localStorage.setItem(SEARCH_KEY, JSON.stringify(list)); return true; }
  catch { return false; }
}

// → {ok, reason: 'saved' | 'duplicate' | 'full' | 'empty' | 'storage'}
export function addPending(filters, name = '') {
  const f = clean(filters);
  if (!f) return {ok: false, reason: 'empty'};
  const list = readPending();
  if (list.some((it) => sameFilters(it, f))) return {ok: true, reason: 'duplicate'};
  if (list.length >= MAX_SEARCHES) return {ok: false, reason: 'full'};
  list.push({...f, id: 'b' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
             name: String(name || '').slice(0, 80), created_at: new Date().toISOString(), last_viewed_at: null,
             want_alert: false});
  return writePending(list) ? {ok: true, reason: 'saved'} : {ok: false, reason: 'storage'};
}

export function removePending(id) { writePending(readPending().filter((it) => it.id !== id)); }
export function patchPending(id, patch) {
  writePending(readPending().map((it) => (it.id === id ? {...it, ...patch} : it)));
}
export function touchPending(id) { patchPending(id, {last_viewed_at: new Date().toISOString()}); }

// The sign-in cookie is set by the signup page's own script, so its presence is readable here.
// It only picks the code path; the server decides who the caller is.
export const hasSession = () => /(?:^|;\s*)sb-access-token=[^;]+/.test(document.cookie);

// Browser stars → [{source, lot_key}] for POST /platform/api/saved/import. A star id is "source:lot id".
export function starRefs() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STAR_KEY) || '{}');
    return Object.keys(saved && typeof saved === 'object' ? saved : {}).map((id) => {
      const i = id.indexOf(':');
      return i > 0 && i < id.length - 1 ? {source: id.slice(0, i), lot_key: id.slice(i + 1)} : null;
    }).filter(Boolean).slice(0, 200);
  } catch { return []; }
}
