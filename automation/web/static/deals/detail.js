// static/deals/detail.js — lot detail page: live timer in the bid rail, "Similar lots" via UI.load, Copy link via UI.pending.
// The similar-lots endpoint is exclusion-filtered server-side (public_deals.py) — nothing is hidden here.
import { load, api, pending, toast } from '../ui/state.js';
import { card, tickTimers } from '../ui/card.js';

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
  const self = Number(similar.dataset.self);
  const params = new URLSearchParams({ status: 'active', sort: 'ends' });
  if (similar.dataset.category) params.set('category', similar.dataset.category);
  if (similar.dataset.st) params.set('state', similar.dataset.st);
  const others = (body) => (body.rows || []).filter((r) => Number(r.asset_id) !== self);
  load(similar, ({ signal }) => api(`/deals/api/lots?${params}`, { signal }), {
    skeleton: 'card', count: 4,
    isEmpty: (body) => others(body).length === 0,
    empty: { title: 'No similar live lots right now', body: 'Try the whole feed instead.', cta: { label: 'Browse all', href: '/deals' } },
    render: (body) => others(body).slice(0, 4).map((r) => card(r)).join(''),
  }).then(() => tickTimers(similar));
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
