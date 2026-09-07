// static/ui/state.js — the one way to fetch and show state. ES module; also window.UI for legacy scripts.
export function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
export const fmt = {
  money(v) { return v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2}); },
  int(v) { return v == null ? '—' : Number(v).toLocaleString(); },
  endsIn(iso, nowMs = Date.now()) {
    if (!iso) return '—';
    const s = Math.floor((new Date(iso).getTime() - nowMs) / 1000);
    if (s <= 0) return 'ended';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  },
  ago(iso) { const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000); return m < 1 ? 'just now' : m < 60 ? `${m} min` : `${Math.round(m / 60)} h`; },
  date(iso) { return iso ? new Date(iso).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—'; },
};
export function toast(message, kind = 'info', ttlMs = 4000) {
  let host = document.getElementById('toast-container');
  if (!host) { host = document.createElement('div'); host.id = 'toast-container'; host.className = 'toast-container'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host); }
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`; el.setAttribute('role', kind === 'err' ? 'alert' : 'status'); el.textContent = message;
  host.appendChild(el); requestAnimationFrame(() => el.classList.add('toast-in'));
  const dismiss = () => { el.classList.remove('toast-in'); el.classList.add('toast-out'); el.addEventListener('transitionend', () => el.remove(), {once: true}); };
  el.addEventListener('click', dismiss); setTimeout(dismiss, ttlMs);
}
export async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    const err = new Error(detail); err.status = res.status; throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}
const SK = {
  line: '<div class="sk sk-line" aria-hidden="true"></div>',
  pill: '<span class="sk sk-pill" aria-hidden="true"></span>',
  row: '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>'.repeat(5) + '</div>',
  tr: '<tr class="sk-tr" aria-hidden="true"><td colspan="99"><div class="sk sk-line"></div></td></tr>',
  card: '<div class="sk-card" aria-hidden="true"><div class="sk sk-band"></div><div class="sk sk-line" style="--w:40%"></div>'
      + '<div class="sk sk-line"></div><div class="sk sk-line" style="--w:70%"></div><div class="sk sk-line" style="--w:50%"></div></div>',
};
export function skeleton(kind, count = 1) { return (SK[kind] || SK.line).repeat(count); }
export function setBusy(el, busy) { if (busy) el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy'); }
export function renderEmpty(el, {glyph = '◌', title, body = '', cta = null} = {}) {
  el.innerHTML = `<div class="ui-empty"><div class="glyph">${esc(glyph)}</div><h2>${esc(title)}</h2>${body ? `<p>${esc(body)}</p>` : ''}`
    + (cta ? (cta.href ? `<a class="btn btn-primary" href="${esc(cta.href)}">${esc(cta.label)}</a>` : `<button class="btn btn-primary" type="button" data-cta>${esc(cta.label)}</button>`) : '') + '</div>';
  if (cta && cta.onClick) el.querySelector('[data-cta]').addEventListener('click', cta.onClick);
  el.dataset.state = 'empty';
}
export function renderError(el, {message, retry}) {
  el.innerHTML = `<div class="ui-error" role="alert"><h2>Something didn't load</h2><p>${esc(message)}</p><button class="btn" type="button" data-retry>Retry</button></div>`;
  if (retry) el.querySelector('[data-retry]').addEventListener('click', retry);
  el.dataset.state = 'error';
}
export function markStale(el, {since = Date.now(), label = 'stale'} = {}) {
  clearStale(el);
  const b = document.createElement('span'); b.className = 'ui-stale-badge'; b.dataset.since = String(since);
  b.textContent = `${label} · ${fmt.ago(new Date(since).toISOString())}`; el.appendChild(b);
}
export function clearStale(el) { el.querySelectorAll(':scope > .ui-stale-badge').forEach(b => b.remove()); }
export async function pending(btn, label, fn) {
  if (!btn) return fn();
  const orig = btn.textContent, wasDisabled = btn.disabled;
  btn.disabled = true; if (label) btn.textContent = label; btn.classList.add('is-pending');
  try { return await fn(); }
  finally { btn.disabled = wasDisabled; btn.textContent = orig; btn.classList.remove('is-pending'); }
}
export async function load(el, fetcher, opts = {}) {
  const {skeleton: kind = 'line', count = 6, keepOld = false, timeoutMs = 15000, render, isEmpty, empty, onError, errorMessage} = opts;
  if (el.__uiAbort) el.__uiAbort.abort();
  const ac = new AbortController(); el.__uiAbort = ac;
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  el.dataset.state = 'loading'; setBusy(el, true);
  if (!keepOld || !el.children.length) el.innerHTML = skeleton(kind, count);
  else markStale(el, {label: 'refreshing'});
  try {
    const data = await fetcher({signal: ac.signal});
    if (ac.signal.aborted && el.__uiAbort !== ac) return undefined;   // superseded
    clearStale(el);
    const emptyNow = isEmpty ? isEmpty(data) : (Array.isArray(data) ? !data.length : false);
    if (emptyNow) { renderEmpty(el, empty || {title: 'Nothing here yet'}); return data; }
    if (render) { const out = render(data); if (typeof out === 'string') el.innerHTML = out; else if (out) { el.innerHTML = ''; el.appendChild(out); } }
    el.dataset.state = 'ready';
    return data;
  } catch (err) {
    if (el.__uiAbort !== ac) return undefined;
    const msg = errorMessage ? errorMessage(err)
      : ac.signal.aborted ? `The server didn't answer in ${Math.round(timeoutMs / 1000)} s.`
      : err.status ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.` : (err.message || 'Network error.');
    renderError(el, {message: msg, retry: () => load(el, fetcher, opts)});
    if (onError) onError(err);
    return undefined;
  } finally { clearTimeout(timer); setBusy(el, false); if (el.__uiAbort === ac) el.__uiAbort = null; }
}
window.UI = {esc, fmt, toast, api, skeleton, setBusy, renderEmpty, renderError, markStale, clearStale, pending, load};
