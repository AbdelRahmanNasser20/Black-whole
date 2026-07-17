/* DealCard — plug-in lot viewer with an image carousel.
 *
 * Usage (from any page that can reach /api/deals/...):
 *   <script src="/static/deal_card.js"></script>
 *   DealCard.open(assetId, accountId, auctionId)            // open on hero
 *   DealCard.open(assetId, accountId, auctionId, {index:3}) // open on image 3
 *
 * Self-contained: injects its own styles, no dependencies. Esc / outside-click
 * closes; arrows + keyboard navigate the carousel.
 */
(function () {
  "use strict";

  const CSS = `
  .dcard-overlay { position:fixed; inset:0; z-index:9999; background:rgba(1,4,9,.82);
    display:flex; align-items:center; justify-content:center; padding:20px; }
  .dcard { width:min(880px, 96vw); max-height:92vh; overflow:auto; border-radius:12px;
    background:#0d1117; color:#e6edf3; border:1px solid #30363d;
    font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .dcard-carousel { position:relative; background:#010409; user-select:none; }
  .dcard-carousel img.dcard-main { width:100%; height:min(52vh, 460px);
    object-fit:contain; display:block; }
  .dcard-nav { position:absolute; top:50%; transform:translateY(-50%);
    width:44px; height:64px; border:0; border-radius:8px; cursor:pointer;
    background:rgba(13,17,23,.65); color:#e6edf3; font-size:24px; }
  .dcard-nav:hover { background:rgba(48,54,61,.9); }
  .dcard-nav.prev { left:10px; } .dcard-nav.next { right:10px; }
  .dcard-count { position:absolute; right:12px; bottom:10px; padding:2px 10px;
    border-radius:999px; background:rgba(13,17,23,.75); font-size:.8rem; color:#9da7b1; }
  .dcard-thumbs { display:flex; gap:6px; overflow-x:auto; padding:8px; background:#0d1117;
    border-bottom:1px solid #21262d; }
  .dcard-thumbs img { width:64px; height:48px; object-fit:cover; border-radius:6px;
    cursor:pointer; opacity:.45; border:1px solid #30363d; flex:0 0 auto; }
  .dcard-thumbs img.on { opacity:1; border-color:#58a6ff; }
  .dcard-body { padding:16px 18px 20px; }
  .dcard-title { font-size:1.15rem; font-weight:700; margin:0 0 8px; padding-right:32px; }
  .dcard-chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
  .dcard-chip { padding:1px 9px; border-radius:999px; font-size:.75rem;
    border:1px solid #30363d; background:#161b22; color:#9da7b1; }
  .dcard-chip.green { border-color:#2ea04366; color:#3fb950; }
  .dcard-chip.red { border-color:#f8514966; color:#f85149; }
  .dcard-chip.amber { border-color:#d2992266; color:#d29922; }
  .dcard-price { font-size:1.3rem; font-weight:700; margin:2px 0; }
  .dcard-price small { font-weight:400; font-size:.8rem; color:#9da7b1; }
  .dcard-meta { color:#9da7b1; font-size:.85rem; margin-bottom:10px; }
  .dcard-desc { white-space:pre-wrap; color:#c9d1d9; max-height:180px; overflow:auto;
    border-top:1px solid #21262d; padding-top:10px; margin-top:4px; }
  .dcard-links { margin-top:12px; display:flex; gap:14px; font-size:.85rem; }
  .dcard-links a { color:#58a6ff; text-decoration:none; }
  .dcard-close { position:absolute; top:8px; right:10px; border:0; background:transparent;
    color:#9da7b1; font-size:22px; cursor:pointer; }
  .dcard-close:hover { color:#e6edf3; }
  .dcard-state { padding:40px; text-align:center; color:#9da7b1; }
  `;

  let overlay = null, state = null;

  function ensureStyles() {
    if (!document.getElementById("dcard-styles")) {
      const s = document.createElement("style");
      s.id = "dcard-styles";
      s.textContent = CSS;
      document.head.appendChild(s);
    }
  }

  function close() {
    if (overlay) { overlay.remove(); overlay = null; state = null; }
    document.removeEventListener("keydown", onKey);
  }

  function onKey(e) {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(state.index - 1);
    else if (e.key === "ArrowRight") show(state.index + 1);
  }

  function show(i) {
    if (!state || !state.images.length) return;
    state.index = (i + state.images.length) % state.images.length;
    state.main.src = state.images[state.index];
    state.count.textContent = (state.index + 1) + " / " + state.images.length;
    state.thumbs.forEach((t, j) => t.classList.toggle("on", j === state.index));
    const on = state.thumbs[state.index];
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function money(lot) {
    const v = lot.final_bid != null ? lot.final_bid : (lot.current_bid || 0);
    const label = lot.final_bid != null ? "final" : "current bid";
    return `${esc(lot.currency_code || "USD")} ${Number(v).toFixed(2)} <small>${label}</small>`;
  }

  function render(lot, startIndex) {
    const images = lot.images || [];
    const card = overlay.querySelector(".dcard");
    const outcomeChip = lot.outcome
      ? `<span class="dcard-chip ${lot.outcome === "no_bid" ? "green" : "amber"}">closed · ${esc(lot.outcome)}</span>`
      : `<span class="dcard-chip green">active</span>`;
    const srcChip = lot.image_source === "archived"
      ? `<span class="dcard-chip green">images archived</span>`
      : `<span class="dcard-chip red">images from CDN</span>`;
    const ends = lot.end_utc
      ? ` · ${lot.outcome ? "ended" : "ends"} ${new Date(lot.end_utc).toLocaleString()}`
      : "";
    card.innerHTML = `
      ${images.length ? `
      <div class="dcard-carousel">
        <img class="dcard-main" alt="${esc(lot.title)}">
        ${images.length > 1 ? `
          <button class="dcard-nav prev" aria-label="previous">‹</button>
          <button class="dcard-nav next" aria-label="next">›</button>` : ""}
        <span class="dcard-count"></span>
      </div>
      ${images.length > 1 ? `<div class="dcard-thumbs">` +
        images.map(u => `<img src="${u}" loading="lazy" alt="">`).join("") + `</div>` : ""}
      ` : ""}
      <div class="dcard-body">
        <h3 class="dcard-title">${esc(lot.title)}</h3>
        <div class="dcard-chips">${outcomeChip}
          <span class="dcard-chip">${lot.bid_count == null ? 0 : lot.bid_count} bids</span>
          ${lot.canonical_category ? `<span class="dcard-chip">${esc(lot.canonical_category)}</span>` : ""}
          ${srcChip}</div>
        <div class="dcard-price">${money(lot)}</div>
        <div class="dcard-meta">${esc(lot.native_category_name)} · ${esc(lot.city)}, ${esc(lot.state)}${esc(ends)}</div>
        <div class="dcard-desc">${esc(lot.description)}</div>
        <div class="dcard-links">
          <a href="/deals/${lot.asset_id}/${lot.account_id}/${lot.auction_id}" target="_blank" rel="noopener">Full page ↗</a>
          <a href="https://www.govdeals.com/en/asset/${lot.asset_id}/${lot.account_id}" target="_blank" rel="noopener">GovDeals ↗</a>
        </div>
      </div>
      <button class="dcard-close" aria-label="close">✕</button>`;
    state = {
      images,
      index: 0,
      main: card.querySelector(".dcard-main"),
      count: card.querySelector(".dcard-count"),
      thumbs: Array.from(card.querySelectorAll(".dcard-thumbs img")),
    };
    card.querySelector(".dcard-close").onclick = close;
    const prev = card.querySelector(".dcard-nav.prev"), next = card.querySelector(".dcard-nav.next");
    if (prev) prev.onclick = () => show(state.index - 1);
    if (next) next.onclick = () => show(state.index + 1);
    state.thumbs.forEach((t, j) => { t.onclick = () => show(j); });
    if (state.main) state.main.onclick = () => show(state.index + 1);
    if (images.length) show(startIndex || 0);
  }

  async function open(assetId, accountId, auctionId, opts) {
    ensureStyles();
    close();
    overlay = document.createElement("div");
    overlay.className = "dcard-overlay";
    overlay.innerHTML = `<div class="dcard" style="position:relative">
      <div class="dcard-state">loading lot ${esc(assetId)}/${esc(accountId)}…</div></div>`;
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    try {
      const r = await fetch(`/api/deals/${assetId}/${accountId}/${auctionId}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      render(await r.json(), (opts && opts.index) || 0);
    } catch (e) {
      if (overlay) overlay.querySelector(".dcard").innerHTML =
        `<div class="dcard-state">couldn't load lot: ${esc(e.message)}</div>
         <button class="dcard-close" aria-label="close">✕</button>`;
      const btn = overlay && overlay.querySelector(".dcard-close");
      if (btn) btn.onclick = close;
    }
  }

  window.DealCard = { open, close };
})();
