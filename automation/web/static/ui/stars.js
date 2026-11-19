// static/ui/stars.js — the VISITOR's own starred lots, kept in this browser under 'pf.stars.v1'
// (the same key the platform demo and merge.js use, so a star travels into the account after sign-in).
// Storage can be blocked or full: every read and write is guarded; the page works with no stars.
// A star id is "<source>:<lot key>" — the key is the tail of the lot page URL, so it is stable across loads.
import {starLimit, UPGRADE_TEXT} from './plan.js';
export const STAR_KEY = 'pf.stars.v1';
const KEEP = ['title', 'viewer_url', 'url', 'govdeals_url', 'source', 'source_name', 'city', 'state', 'current_bid',
  'bid_count', 'end_utc', 'photo_url', 'canonical_category', 'quantity', 'quantity_source', 'unit_bid',
  'landed_cost', 'outcome_complete', 'outcome', 'final_bid', 'final_bid_count', 'asset_id', 'account_id', 'auction_id'];
const listeners = new Set();

export function starId(row) {
  if (!row) return '';
  const source = row.source || 'govdeals';
  const path = row.viewer_url || '';
  const i = path.lastIndexOf('-' + source + '-');
  if (i > 0) return `${source}:${path.slice(i + source.length + 2)}`;
  if (row.lot_key) return `${source}:${row.lot_key}`;
  if (row.id && String(row.id).includes(':')) return String(row.id);
  if (row.asset_id) return `${source}:${row.asset_id}.${row.account_id}.${row.auction_id}`;
  return '';
}
export function readAll() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STAR_KEY) || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
  } catch { return {}; }
}
function write(all) {
  try { window.localStorage.setItem(STAR_KEY, JSON.stringify(all)); return true; } catch { return false; }
}
export const has = (id) => Boolean(id) && Object.prototype.hasOwnProperty.call(readAll(), id);
export const count = () => Object.keys(readAll()).length;
// Rows as they looked when starred, newest first.
export function list() {
  return Object.entries(readAll()).map(([id, r]) => ({...r, id, star_id: id}))
    .sort((a, b) => String(b.starred_at || '').localeCompare(String(a.starred_at || '')));
}
export function toggle(row) {
  const id = starId(row); if (!id) return null;
  const all = readAll();
  if (all[id]) { delete all[id]; write(all); notify(); return false; }
  if (Object.keys(all).length >= starLimit()) return null;   // free = 3, Standard = plan.MAX_STARS
  const keep = {}; for (const k of KEEP) if (row[k] !== undefined) keep[k] = row[k];
  all[id] = {...keep, id, starred_at: new Date().toISOString()};
  write(all); notify(); return true;
}
export function onChange(cb) { listeners.add(cb); return () => listeners.delete(cb); }
function notify() { listeners.forEach((cb) => { try { cb(); } catch { /* a listener must not break the others */ } }); }

// Paint every star button under `root` from the store (card.js renders them unpressed).
export function paint(root = document) {
  const all = readAll();
  root.querySelectorAll('.card-star[data-star-id]').forEach((b) => {
    const on = Boolean(all[b.dataset.starId]);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.title = on ? 'Remove star' : 'Star this lot';
  });
}
// Click handling for every card on the page. The row comes from the card's own data-row JSON.
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('.card-star[data-star-id]');
  if (!b) return;
  e.preventDefault();
  let row = null;
  try { row = JSON.parse(b.closest('.card').dataset.row || 'null'); } catch { row = null; }
  if (!row) return;
  const r = toggle(row);
  if (r === null) { b.title = UPGRADE_TEXT; if (window.confirm(UPGRADE_TEXT + ' See the plans?')) window.location.href = '/platform#pricing'; return; }
  paint(b.closest('.card'));
});
