// static/admin/shared.js — helpers used by more than one admin tab (split verbatim from app.js, Workstream F).
// Cross-tab calls go through `hooks` (a tab registers what another tab needs) — tabs never import each other.
import {toast, api as apiFetch} from '../ui/state.js';
export {toast, apiFetch};

export const hooks = {};
const applyState = (...a) => hooks.applyState(...a);

// ───────── URL params (the one place admin URL state is read/written) ─────────
// Pure helpers over location/history: the shell owns `tab`, each tab owns its own keys through these.
// They live here (not in shell.js) because index.html loads shell as `shell.js?v=…` — a tab that imported
// `./shell.js` would pull in a SECOND shell instance that boots every tab mid-evaluation (TDZ crash).

/** Current query string as a plain object. */
export function getParams() {
  const out = {};
  for (const [k, v] of new URLSearchParams(location.search)) out[k] = v;
  return out;
}

/** Merge `patch` into the query string. null/undefined/'' deletes a key. replace=true keeps history flat;
 *  replace=false pushes an entry (used for tab switches so Back works). Returns the new params. */
export function setParams(patch, {replace = true} = {}) {
  const sp = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === null || v === undefined || v === '') sp.delete(k);
    else sp.set(k, String(v));
  }
  const qs = sp.toString();
  const url = location.pathname + (qs ? '?' + qs : '') + location.hash;
  const cur = location.pathname + location.search + location.hash;
  if (url !== cur) history[replace ? 'replaceState' : 'pushState'](history.state, '', url);
  return getParams();
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// Disable + relabel a button while `fn` runs. Always restores label and
// disabled state, even if fn throws. Returns fn's return value.
export async function withButtonLoading(btn, loadingText, fn) {
  if (!btn) return fn();
  const orig = btn.textContent;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  if (loadingText) btn.textContent = loadingText;
  btn.classList.add('is-loading');
  try {
    return await fn();
  } finally {
    btn.disabled = origDisabled;
    btn.textContent = orig;
    btn.classList.remove('is-loading');
  }
}

export function row(label, val, html = false) {
  if (val == null || val === '') return '';
  const cell = html ? val : esc(String(val));
  return `<dt>${label}</dt><dd>${cell}</dd>`;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

export function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}
export function escapeAttr(s) { return escapeHtml(s); }

export const SOURCE_NAMES = { gd: 'GovDeals', ps: 'Public Surplus', bs: 'BidSpotter' };

export function _ageInDays(isoStr) {
  if (!isoStr) return null;
  const ms = Date.now() - new Date(isoStr).getTime();
  if (Number.isNaN(ms)) return null;
  return ms / 86400000;
}

export function _fmtAge(days) {
  if (days == null) return 'never';
  if (days < 1/24) return 'just now';
  if (days < 1) return `${Math.round(days * 24)}h ago`;
  const d = Math.floor(days);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

export function _fmtRemaining(secs) {
  if (secs == null) return 'no end date';
  if (secs <= 0) return 'ended';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return `${h}h ${m}m`;
  }
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  return `${d}d ${h}h`;
}

export async function queueRuns(urls) {
  await apiFetch('/api/runs/queue', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({urls}),
  });
  // Refresh the launcher state so the queue strip updates immediately.
  fetch('/api/runs/state').then(r => r.json()).then(applyState).catch(() => {});
}
