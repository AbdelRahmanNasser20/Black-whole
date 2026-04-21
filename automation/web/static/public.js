/* Black Whole Liquidation — public site */

// ─── contact form submission ────────────────────────────────────────────
(function initContactForm() {
  const form = document.getElementById('contact-form');
  if (!form) return;
  const result = form.querySelector('.mf-result');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {};
    for (const [k, v] of fd.entries()) {
      if (typeof v === 'string' && v.trim() === '') continue;
      payload[k] = v;
    }
    if (payload.quantity_interested) {
      payload.quantity_interested = parseInt(payload.quantity_interested, 10);
    }

    const btn = form.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'FILING…';
    result.hidden = true;
    result.classList.remove('mf-result--ok', 'mf-result--err');

    try {
      const r = await fetch('/contact', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Request failed');
      result.textContent = '◉ INQUIRY #' + data.id + ' FILED. WE\u2019LL BE IN TOUCH WITHIN 1 BUSINESS DAY.';
      result.classList.add('mf-result--ok');
      result.hidden = false;
      form.reset();
    } catch (err) {
      result.textContent = '✗ ' + (err.message || 'Something broke. Please try again or email us.');
      result.classList.add('mf-result--err');
      result.hidden = false;
    } finally {
      btn.disabled = false; btn.textContent = btnText;
    }
  });
})();

// ─── listings filter ────────────────────────────────────────────────────
(function initListingsFilter() {
  const grid = document.getElementById('lot-grid');
  if (!grid) return;
  const fType = document.getElementById('f-type');
  const fCity = document.getElementById('f-city');
  const fQty = document.getElementById('f-qty');
  const fSearch = document.getElementById('f-search');
  const reset = document.getElementById('f-reset');
  const empty = document.getElementById('empty-filter');
  const cards = Array.from(grid.querySelectorAll('.lot-card'));

  function applyFilters() {
    const t = (fType.value || '').toLowerCase();
    const c = (fCity.value || '').toLowerCase();
    const minQ = parseInt(fQty.value || '0', 10) || 0;
    const search = (fSearch.value || '').toLowerCase().trim();
    let visible = 0;
    for (const card of cards) {
      const cardType = (card.dataset.type || '').toLowerCase();
      const cardCity = (card.dataset.city || '').toLowerCase();
      const cardQty = parseInt(card.dataset.qty || '0', 10) || 0;
      const searchBag = (card.dataset.search || '').toLowerCase();
      const typeOk = !t || cardType === t;
      const cityOk = !c || cardCity === c;
      const qtyOk = cardQty >= minQ;
      const searchOk = !search || searchBag.includes(search);
      const show = typeOk && cityOk && qtyOk && searchOk;
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    }
    if (empty) empty.hidden = visible > 0;
  }

  [fType, fCity, fQty, fSearch].forEach(el => {
    if (!el) return;
    const ev = (el.tagName === 'SELECT') ? 'change' : 'input';
    el.addEventListener(ev, applyFilters);
  });
  reset?.addEventListener('click', () => {
    fType.value = ''; fCity.value = ''; fQty.value = ''; fSearch.value = '';
    applyFilters();
  });
})();

// ─── detail page gallery ────────────────────────────────────────────────
(function initGallery() {
  const main = document.getElementById('gal-main-img');
  if (!main) return;
  const thumbs = document.querySelectorAll('.gal-thumb');
  if (!thumbs.length) return;
  // Mark the one matching the main src as active
  thumbs.forEach(t => {
    if (t.dataset.src === main.getAttribute('src')) t.classList.add('is-active');
    t.addEventListener('click', () => {
      main.src = t.dataset.src;
      thumbs.forEach(x => x.classList.remove('is-active'));
      t.classList.add('is-active');
    });
  });
})();
