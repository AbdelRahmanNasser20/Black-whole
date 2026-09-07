// static/admin/drafts.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, row, esc} from './shared.js';

// ───────── drafts ─────────

async function loadDrafts() {
  const grid = $('#drafts-grid');
  grid.innerHTML = '<div class="drafts-empty">Scanning ~/Desktop/Banquet chiars Pictures…</div>';
  const res = await fetch('/api/drafts');
  const {drafts} = await res.json();
  if (!drafts.length) {
    grid.innerHTML = '<div class="drafts-empty">No listing folders yet. Run a pipeline.</div>';
    return;
  }
  grid.innerHTML = '';
  for (const d of drafts) {
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
    grid.appendChild(card);
  }
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
}

export async function load() { return loadDrafts(); }
