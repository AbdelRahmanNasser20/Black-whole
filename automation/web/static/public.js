/* Black Whole Liquidation — public site */

// ─── capture form submission (contact + alerts signup) ─────────────────
function bindCaptureForm(form) {
  const endpoint = form.dataset.endpoint || '/contact';
  const result = form.querySelector('.mf-result');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {};
    for (const [k, v] of fd.entries()) {
      if (typeof v === 'string' && v.trim() === '') continue;
      payload[k] = v;
    }
    for (const qty of ['quantity_interested', 'quantity_wanted']) {
      if (payload[qty]) payload[qty] = parseInt(payload[qty], 10);
    }

    result.hidden = true;
    result.classList.remove('mf-result--ok', 'mf-result--err');
    if (endpoint === '/subscribe' && !payload.email && !payload.phone) {
      result.textContent = '✗ We need an email or a phone number to reach you.';
      result.classList.add('mf-result--err');
      result.hidden = false;
      return;
    }

    const btn = form.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'FILING…';

    try {
      const r = await fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Request failed');
      result.textContent = form.dataset.success ||
        ('◉ INQUIRY #' + data.id + ' FILED. WE\u2019LL BE IN TOUCH WITHIN 1 BUSINESS DAY.');
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
}

(function initCaptureForms() {
  const contact = document.getElementById('contact-form');
  if (contact) bindCaptureForm(contact);
  document.querySelectorAll('.js-capture-form').forEach(bindCaptureForm);
})();

// ─── featured carousel (landing) ────────────────────────────────────────
(function initFeaturedCarousel() {
  const track = document.getElementById('featured-track');
  if (!track) return;
  const step = () => {
    const card = track.querySelector('.lot-card');
    return card ? card.getBoundingClientRect().width + 24 : 324;
  };
  document.getElementById('feat-prev')?.addEventListener('click', () => {
    track.scrollBy({left: -step(), behavior: 'smooth'});
  });
  document.getElementById('feat-next')?.addEventListener('click', () => {
    track.scrollBy({left: step(), behavior: 'smooth'});
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
