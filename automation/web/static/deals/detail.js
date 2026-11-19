// static/deals/detail.js — lot detail page: live timer in the bid rail, "Similar lots" via UI.load, Copy link via UI.pending.
// The similar-lots endpoint is exclusion-filtered server-side (public_deals.py) — nothing is hidden here.
import { load, api, pending, toast } from '../ui/state.js';
import { card, tickTimers } from '../ui/card.js';
import { paint as paintStars } from '../ui/stars.js';

const $ = (sel, root = document) => root.querySelector(sel);

// ── back link: go back when we came from inside the site, else fall through to /deals ──
const back = $('[data-back]');
if (back) back.addEventListener('click', (e) => {
  if (history.length > 1 && document.referrer.startsWith(location.origin)) { e.preventDefault(); history.back(); }
});

// ── bid rail timer ──
tickTimers();
setInterval(tickTimers, 30_000);

// ── similar lots ──
const similar = $('#similar');
if (similar) {
  const self = similar.dataset.self || location.pathname;
  const base = new URLSearchParams({ status: 'active', sort: 'ends', per_page: '25' });
  if (similar.dataset.category) base.set('category', similar.dataset.category);
  const withState = new URLSearchParams(base);
  if (similar.dataset.st) withState.set('state', similar.dataset.st);
  const others = (body) => (body.rows || []).filter((r) => r.viewer_url !== self);
  // Same state first; when that gives fewer than 4, the same category anywhere.
  const fetchSimilar = async ({ signal }) => {
    let body = await api(`/deals/api/lots?${withState}`, { signal });
    if (others(body).length < 4 && similar.dataset.st) body = await api(`/deals/api/lots?${base}`, { signal });
    return body;
  };
  load(similar, fetchSimilar, {
    skeleton: 'card', count: 4,
    isEmpty: (body) => others(body).length === 0,
    empty: { title: 'No similar live lots right now', body: 'Try the whole feed instead.', cta: { label: 'Browse all', href: '/deals' } },
    render: (body) => others(body).slice(0, 12).map((r) => card(r)).join(''),
  }).then(() => { tickTimers(similar); paintStars(similar); });
}

// ── copy link ──
const copyBtn = $('[data-copy]');
if (copyBtn) copyBtn.addEventListener('click', () => pending(copyBtn, 'Copying…', async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    toast('Link copied', 'ok');
  } catch (err) {
    toast("Couldn't copy — select the address bar instead.", 'err');
  }
}));
