// static/admin/drafts.js — Drafts tab (Workstream E-drafts). One read site: GET /api/drafts → UI.load.
// States (plan §1.6): skeleton twin shipped in index.html → ready | empty (renderEmpty + "Run a pipeline") | error (Retry).
import {$, row, esc} from './shared.js';
import {api, load as uiLoad} from '../ui/state.js';

// ───────── drafts ─────────

function draftCard(d) {
  const card = document.createElement('article');
  card.className = 'draft';

  const imgs = (d.images || []).slice(0, 8).map(name =>
    `<img src="/image/${encodeURIComponent(d.folder)}/${encodeURIComponent(name)}" loading="lazy" alt="">`
  ).join('');
  const remaining = Math.max(0, (d.image_count || 0) - 8);
  const moreCell = remaining > 0 ? `<div class="more">+${remaining}</div>` : '';

  card.innerHTML = `
    <header class="draft-head">
      <h2 class="draft-title">${esc(d.title || d.folder)}</h2>
      <div class="draft-loc">${esc(d.location || '— location unknown —')}</div>
      <div class="draft-folder">${esc(d.folder)}</div>
    </header>
    <div class="draft-grid-imgs">${imgs}${moreCell}</div>
    <dl class="draft-meta">
      ${row('qty', d.quantity)}
      ${row('type', d.chair_type)}
      ${row('dimensions', d.dimensions)}
      ${row('price', d.suggested_price ? `<span class="price">$${d.suggested_price}/ea</span>` : null, true)}
      ${row('images', d.image_count)}
    </dl>
    <div class="draft-actions">
      ${d.facebook_url
        ? `<a href="${esc(d.facebook_url)}" target="_blank" rel="noopener">↗ Facebook draft</a>`
        : '<a class="disabled" title="no FB URL — paste one on the Inventory tab or run the pipeline">↗ Facebook draft</a>'}
      ${d.ebay_url
        ? `<a href="${esc(d.ebay_url)}" target="_blank" rel="noopener">↗ eBay draft</a>`
        : '<a class="disabled" title="no eBay URL — paste one on the Inventory tab or run the pipeline">↗ eBay draft</a>'}
    </div>
  `;
  return card;
}

function renderDrafts(drafts) {
  const frag = document.createDocumentFragment();
  for (const d of drafts) frag.appendChild(draftCard(d));
  return frag;
}

// "Run a pipeline" → the Launcher tab. Clicking the rail link goes through shell.js (URL param + activateTab)
// without a page reload; a plain navigation is the fallback if the rail is missing.
function goToLauncher() {
  const link = $('.rail-tab[data-tab="launcher"]');
  if (link) link.click(); else location.search = '?tab=launcher';
}

async function loadDrafts() {
  const grid = $('#drafts-grid');
  if (!grid) return;
  return uiLoad(grid, async ({signal}) => (await api('/api/drafts', {signal})).drafts || [], {
    skeleton: 'card',
    count: 6,
    render: renderDrafts,
    empty: {
      glyph: '◌',
      title: 'No listing folders yet',
      body: 'A pipeline run drops a folder under ~/Desktop/Banquet chiars Pictures; drafts show up here.',
      cta: {label: 'Run a pipeline', onClick: goToLauncher},
    },
    errorMessage: (err) => "Couldn't load drafts. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The folder scan didn't answer in 15 s."),
  });
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is actually loading — drop the state so a smoke on another tab is not blocked on a
  // hidden pane. load() re-sets it when the tab opens.
  const grid = $('#drafts-grid');
  const pane = grid?.closest('[data-pane]');
  if (grid && pane?.hidden) { delete grid.dataset.state; grid.removeAttribute('aria-busy'); }
}

export async function load() { return loadDrafts(); }
