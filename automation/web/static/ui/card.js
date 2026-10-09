// static/ui/card.js — the §1.4 feed card. One renderer for /deals, the map side list, "Similar lots", and the preview.
// Input: one `/deals/api/lots` row. No photo, ever (public rows never carry one) — the price block leads.
import {esc, fmt, skeleton} from './state.js';

const HOUR_MS = 3600 * 1000;

function bandLabel(row) {
  const cat = (row.canonical_category || 'lot').replace(/_/g, ' ').toUpperCase();
  return row.state ? `${cat} · ${esc(row.state)}` : cat;
}

function timerHtml(row, nowMs) {
  if (row.outcome_complete) {
    const why = row.outcome ? ' · ' + String(row.outcome).replace(/_/g, ' ') : '';
    return `<span class="card-timer is-closed">closed${esc(why)}</span>`;
  }
  const iso = row.end_utc || '';
  const left = iso ? new Date(iso).getTime() - nowMs : NaN;
  const urgent = Number.isFinite(left) && left > 0 && left < HOUR_MS;
  return `<span class="card-timer${urgent ? ' is-urgent' : ''}" data-ends="${esc(iso)}">⏱ ${esc(fmt.endsIn(iso, nowMs))}</span>`;
}

export function card(row, {compact = false} = {}) {
  const nowMs = Date.now();
  const closed = !!row.outcome_complete;
  const price = closed && row.final_bid != null ? row.final_bid : row.current_bid;
  const bids = closed && row.final_bid_count != null ? row.final_bid_count : (row.bid_count ?? 0);
  const qty = Number(row.quantity) || 0;
  const showUnit = !compact && qty > 1 && row.quantity_source !== 'default' && row.unit_bid != null;
  const place = [row.city, row.state].filter(Boolean).map(esc).join(', ');
  const chips = [];
  if (!compact && row.landed_cost != null) chips.push(`<span class="chip chip-mono">landed ${esc(fmt.money(row.landed_cost))}</span>`);
  if (!compact && !closed && !bids) chips.push('<span class="chip chip-mono">no bids yet</span>');
  // Source badge = the external link's word. Rows from deal_lots carry govdeals_url; rows from the other
  // recorder sites carry url + source_name (server-normalised, see deals_sources.py).
  const extUrl = row.url || row.govdeals_url;
  const source = row.source || 'govdeals';
  const sourceName = row.source_name || 'GovDeals';
  const ext = extUrl
    ? `<a class="card-ext" href="${esc(extUrl)}" target="_blank" rel="noopener" aria-label="Open on ${esc(sourceName)}"><span class="card-ext-word card-source" data-source="${esc(source)}">${esc(sourceName)}</span> ↗</a>` : '';
  // No viewer page for a non-GovDeals lot: the title goes straight to the source site.
  const title = esc(row.title || 'Untitled lot');
  const titleHtml = row.viewer_url
    ? `<a class="card-title card-link" href="${esc(row.viewer_url)}">${title}</a>`
    : extUrl ? `<a class="card-title card-link" href="${esc(extUrl)}" target="_blank" rel="noopener">${title}</a>`
    : `<span class="card-title">${title}</span>`;
  return `<article class="card${compact ? ' card-compact' : ''}${closed ? ' is-closed' : ''}" data-source="${esc(source)}">
  <div class="card-band"><span class="card-cat">${bandLabel(row)}</span>${ext}${timerHtml(row, nowMs)}</div>
  <div class="card-price-row"><span class="card-price">${esc(fmt.money(price))}</span>${showUnit
    ? `<span class="card-unit">${esc(fmt.int(qty))} × ${esc(fmt.money(row.unit_bid))} /unit</span>` : ''}</div>
  ${titleHtml}
  <div class="card-meta">${place || '—'} · <span class="card-bids${bids > 0 ? ' has-bids' : ''}">${esc(fmt.int(bids))} bid${bids === 1 ? '' : 's'}</span></div>
  ${chips.length ? `<div class="card-chips">${chips.join('')}</div>` : ''}
</article>`;
}

export function cardSkeleton(n = 8) { return skeleton('card', n); }

export function tickTimers(root = document) {
  const nowMs = Date.now();
  root.querySelectorAll('[data-ends]').forEach(el => {
    const iso = el.dataset.ends;
    if (!iso) return;
    const left = new Date(iso).getTime() - nowMs;
    el.textContent = '⏱ ' + fmt.endsIn(iso, nowMs);
    el.classList.toggle('is-urgent', left > 0 && left < HOUR_MS);
  });
}
