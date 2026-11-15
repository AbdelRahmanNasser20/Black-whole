// static/deals/views.js — the three ways to show one page of `/deals/api/lots` rows: cards, a table, a map.
// Pure renderers (rows in, HTML out) plus the map mount, so /deals and the saved-search pages share them.
// No policy here: the rows are already public (public_deals.py), so nothing is filtered or added.
import {esc, fmt} from '../ui/state.js';
import {card} from '../ui/card.js';
import {mount} from '../ui/glmap.js';

export function renderCards(rows, opts) {
  return (rows || []).map((r) => card(r, opts)).join('');
}

function tableRow(row, nowMs) {
  const closed = !!row.outcome_complete;
  const price = closed && row.final_bid != null ? row.final_bid : row.current_bid;
  const bids = closed && row.final_bid_count != null ? row.final_bid_count : (row.bid_count ?? 0);
  const qty = Number(row.quantity) || 0;
  const unit = qty > 1 && row.quantity_source !== 'default' && row.unit_bid != null
    ? `${esc(fmt.money(row.unit_bid))} <span class="feed-td-qty">× ${esc(fmt.int(qty))}</span>` : '—';
  const extUrl = row.url || row.govdeals_url;
  const title = esc(row.title || 'Untitled lot');
  const link = row.viewer_url ? `<a class="feed-td-title" href="${esc(row.viewer_url)}">${title}</a>`
    : extUrl ? `<a class="feed-td-title" href="${esc(extUrl)}" target="_blank" rel="noopener">${title}</a>`
    : `<span class="feed-td-title">${title}</span>`;
  const site = esc(row.source_name || 'GovDeals');
  const siteCell = extUrl ? `<a href="${esc(extUrl)}" target="_blank" rel="noopener">${site} ↗</a>` : site;
  const place = [row.city, row.state].filter(Boolean).map(esc).join(', ') || '—';
  const ends = closed
    ? `<span class="card-timer is-closed">closed${row.outcome ? ' · ' + esc(String(row.outcome).replace(/_/g, ' ')) : ''}</span>`
    : `<span class="card-timer" data-ends="${esc(row.end_utc || '')}">⏱ ${esc(fmt.endsIn(row.end_utc || '', nowMs))}</span>`;
  return `<tr${closed ? ' class="is-closed"' : ''}>
  <td>${link}</td><td class="feed-td-site">${siteCell}</td><td>${place}</td>
  <td class="num">${esc(fmt.money(price))}</td><td class="num">${esc(fmt.int(bids))}</td>
  <td class="num feed-td-unit">${unit}</td><td class="feed-td-ends">${ends}</td>
</tr>`;
}

export function renderTable(rows) {
  const nowMs = Date.now();
  return `<table class="table feed-table">
<thead><tr><th scope="col">Lot</th><th scope="col" class="feed-td-site">Site</th><th scope="col">Location</th>
<th scope="col" class="num">Bid</th><th scope="col" class="num">Bids</th><th scope="col" class="num feed-td-unit">$ / unit</th>
<th scope="col">Ends</th></tr></thead>
<tbody>${(rows || []).map((r) => tableRow(r, nowMs)).join('')}</tbody>
</table>`;
}

// The last page this viewer can open: the list's own last page, or the server's `max_page` when it sends one
// (a visitor with no account reads the first rows only; deeper pages need a free account).
export function lastPage({pages = 1, max_page} = {}) {
  const all = Math.max(1, Number(pages) || 1), cap = Number(max_page) || 0;
  return cap > 0 ? Math.min(all, cap) : all;
}

// Pager for a paged list. Buttons carry data-page; the page that owns the list listens for the click.
export function renderPager({page = 1, pages = 1, total = 0, per_page = 0, max_page} = {}) {
  page = Number(page) || 1;
  const all = Math.max(1, Number(pages) || 1);
  pages = lastPage({pages: all, max_page});
  const capped = pages < all;
  if (all <= 1) return '';
  const first = (page - 1) * per_page + 1, last = Math.min(total, page * per_page);
  const btn = (label, target, off, name) =>
    `<button class="btn btn-ghost btn-small" type="button" data-page="${target}"${off ? ' disabled' : ''} aria-label="${name}">${label}</button>`;
  return btn('← Prev', page - 1, page <= 1, 'Previous page')
    + `<span class="feed-pager-where">Page <strong>${esc(fmt.int(page))}</strong> of ${esc(fmt.int(pages))}`
    + (per_page ? ` · lots ${esc(fmt.int(first))}–${esc(fmt.int(last))} of ${esc(fmt.int(total))}` : '') + '</span>'
    + btn('Next →', page + 1, page >= pages, 'Next page')
    + (capped && page >= pages
      ? ' <a class="btn btn-primary btn-small" href="/platform/signup">Sign in to see more</a>' : '');
}

// The shared MapLibre map (static/ui/glmap.js). Rejects with err.mapFailed when the library or style cannot load.
export function mountMap(el, opts) { return mount(el, opts); }
