/* DealCard — plug-in lot viewer with an image carousel. Operator-only (the page loads it under show_images).
 *
 * Usage (ES module; also exposed as window.DealCard for the legacy admin page):
 *   import { open } from '/static/deal_card.js';
 *   open(assetId, accountId, auctionId)            // open on hero
 *   open(assetId, accountId, auctionId, {index:3}) // open on image 3
 *
 * Injects its own styles (tokens only). Esc / outside-click closes; arrows + keyboard navigate the carousel.
 * The lot fetch goes through UI.load so the overlay shows the shared skeleton / error states.
 */
import { load, api, esc, fmt } from './ui/state.js';

const CSS = `
  .dcard-overlay { position:fixed; inset:0; z-index:9999; background:rgba(0,0,0,.82);
    display:flex; align-items:center; justify-content:center; padding:20px; }
  .dcard { position:relative; width:min(880px, 96vw); max-height:92vh; overflow:auto; border-radius:var(--radius);
    background:var(--surface); color:var(--text); border:1px solid var(--border-strong); font:var(--fs-base)/1.5 var(--sans); }
  .dcard[data-state="loading"] { padding: var(--pad); }
  .dcard-carousel { position:relative; background:var(--bg); user-select:none; }
  .dcard-carousel img.dcard-main { width:100%; height:min(52vh, 460px); object-fit:contain; display:block; }
  .dcard-nav { position:absolute; top:50%; transform:translateY(-50%); width:44px; height:64px; border:0;
    border-radius:var(--radius); cursor:pointer; background:rgba(0,0,0,.6); color:var(--text); font-size:24px; }
  .dcard-nav:hover { background:var(--surface-2); }
  .dcard-nav.prev { left:10px; } .dcard-nav.next { right:10px; }
  .dcard-count { position:absolute; right:12px; bottom:10px; padding:2px 10px; background:rgba(0,0,0,.7);
    font:500 var(--fs-xs)/1.6 var(--mono); color:var(--muted); }
  .dcard-thumbs { display:flex; gap:6px; overflow-x:auto; padding:8px; background:var(--surface); border-bottom:1px solid var(--border); }
  .dcard-thumbs img { width:64px; height:48px; object-fit:cover; cursor:pointer; opacity:.45; border:1px solid var(--border); flex:0 0 auto; }
  .dcard-thumbs img.on { opacity:1; border-color:var(--accent); }
  .dcard-body { padding:16px 18px 20px; }
  .dcard-title { font:400 var(--fs-xl)/1.2 var(--display); margin:0 0 8px; padding-right:32px; }
  .dcard-chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
  .dcard-price { font:700 var(--fs-xl)/1.1 var(--mono); margin:2px 0; font-variant-numeric:tabular-nums; }
  .dcard-price small { font:400 var(--fs-xs)/1 var(--mono); color:var(--muted); margin-left:6px; }
  .dcard-meta { color:var(--muted); font-size:var(--fs-sm); margin-bottom:10px; }
  .dcard-desc { white-space:pre-wrap; color:var(--text); max-height:180px; overflow:auto; border-top:1px solid var(--border); padding-top:10px; margin-top:4px; }
  .dcard-links { margin-top:12px; display:flex; gap:14px; font:500 var(--fs-sm)/1 var(--mono); }
  .dcard-links a { color:var(--muted); text-decoration:none; }
  .dcard-links a:hover { color:var(--text); }
  .dcard-close { position:absolute; top:8px; right:10px; border:0; background:transparent; color:var(--muted); font-size:22px; cursor:pointer; z-index:2; }
  .dcard-close:hover { color:var(--text); }
`;

let overlay = null, state = null;

function ensureStyles() {
  if (!document.getElementById('dcard-styles')) {
    const s = document.createElement('style');
    s.id = 'dcard-styles';
    s.textContent = CSS;
    document.head.appendChild(s);
  }
}

export function close() {
  if (overlay) { overlay.remove(); overlay = null; state = null; }
  document.removeEventListener('keydown', onKey);
}

function onKey(e) {
  if (e.key === 'Escape') close();
  else if (e.key === 'ArrowLeft') show(state.index - 1);
  else if (e.key === 'ArrowRight') show(state.index + 1);
}

function show(i) {
  if (!state || !state.images.length) return;
  state.index = (i + state.images.length) % state.images.length;
  state.main.src = state.images[state.index];
  state.count.textContent = (state.index + 1) + ' / ' + state.images.length;
  state.thumbs.forEach((t, j) => t.classList.toggle('on', j === state.index));
  const on = state.thumbs[state.index];
  if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function money(lot) {
  const v = lot.final_bid != null ? lot.final_bid : (lot.current_bid || 0);
  const label = lot.final_bid != null ? 'final' : 'current bid';
  return `${esc(fmt.money(v))} <small>${label}</small>`;
}

function markup(lot) {
  const images = lot.images || [];
  const outcomeChip = lot.outcome
    ? `<span class="badge badge-closed">closed · ${esc(lot.outcome)}</span>`
    : '<span class="badge badge-live">live</span>';
  const srcChip = lot.image_source === 'archived'
    ? '<span class="chip chip-mono">images archived</span>'
    : '<span class="chip chip-mono">images from CDN</span>';
  const ends = lot.end_utc ? ` · ${lot.outcome ? 'ended' : 'ends'} ${fmt.date(lot.end_utc)}` : '';
  return `
    ${images.length ? `
    <div class="dcard-carousel">
      <img class="dcard-main" alt="${esc(lot.title)}">
      ${images.length > 1 ? `
        <button class="dcard-nav prev" type="button" aria-label="previous">‹</button>
        <button class="dcard-nav next" type="button" aria-label="next">›</button>` : ''}
      <span class="dcard-count"></span>
    </div>
    ${images.length > 1 ? '<div class="dcard-thumbs">' + images.map(u => `<img src="${esc(u)}" loading="lazy" alt="">`).join('') + '</div>' : ''}
    ` : ''}
    <div class="dcard-body">
      <h3 class="dcard-title">${esc(lot.title)}</h3>
      <div class="dcard-chips">${outcomeChip}
        <span class="chip chip-mono">${lot.bid_count == null ? 0 : esc(lot.bid_count)} bids</span>
        ${lot.canonical_category ? `<span class="chip chip-mono">${esc(lot.canonical_category)}</span>` : ''}
        ${srcChip}</div>
      <div class="dcard-price">${money(lot)}</div>
      <div class="dcard-meta">${esc(lot.native_category_name)} · ${esc(lot.city)}, ${esc(lot.state)}${esc(ends)}</div>
      <div class="dcard-desc">${esc(lot.description)}</div>
      <div class="dcard-links">
        <a href="/deals/${esc(lot.asset_id)}/${esc(lot.account_id)}/${esc(lot.auction_id)}" target="_blank" rel="noopener">Full page ↗</a>
        <a href="https://www.govdeals.com/en/asset/${esc(lot.asset_id)}/${esc(lot.account_id)}" target="_blank" rel="noopener">GovDeals ↗</a>
      </div>
    </div>
    <button class="dcard-close" type="button" aria-label="close">✕</button>`;
}

function wire(card, lot, startIndex) {
  const images = lot.images || [];
  state = {
    images,
    index: 0,
    main: card.querySelector('.dcard-main'),
    count: card.querySelector('.dcard-count'),
    thumbs: Array.from(card.querySelectorAll('.dcard-thumbs img')),
  };
  card.querySelector('.dcard-close').onclick = close;
  const prev = card.querySelector('.dcard-nav.prev'), next = card.querySelector('.dcard-nav.next');
  if (prev) prev.onclick = () => show(state.index - 1);
  if (next) next.onclick = () => show(state.index + 1);
  state.thumbs.forEach((t, j) => { t.onclick = () => show(j); });
  if (state.main) state.main.onclick = () => show(state.index + 1);
  if (images.length) show(startIndex || 0);
}

export async function open(assetId, accountId, auctionId, opts) {
  ensureStyles();
  close();
  overlay = document.createElement('div');
  overlay.className = 'dcard-overlay';
  overlay.innerHTML = '<div class="dcard"></div>';
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.body.appendChild(overlay);
  document.addEventListener('keydown', onKey);
  const card = overlay.querySelector('.dcard');
  const url = `/api/deals/${assetId}/${accountId}/${auctionId}`;
  const lot = await load(card, ({ signal }) => api(url, { signal }), {
    skeleton: 'line', count: 6,
    render: (d) => markup(d),
  });
  if (lot && overlay && card.isConnected) wire(card, lot, (opts && opts.index) || 0);
  else if (overlay && card.isConnected && !card.querySelector('.dcard-close')) {
    // error state from UI.load: keep a way out
    const btn = document.createElement('button');
    btn.className = 'dcard-close'; btn.type = 'button'; btn.setAttribute('aria-label', 'close'); btn.textContent = '✕';
    btn.onclick = close; card.appendChild(btn);
  }
}

window.DealCard = { open, close };
