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
      // data-city is a `|`-joined list — a lot can sit in several places.
      const cityOk = !c || cardCity.split('|').includes(c);
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

// ─── freight estimate widget (detail page) ──────────────────────────────
// Two steps on purpose: ZIP first (zero friction, always answers), and only
// once a number is on screen do we ask for an email. An unquotable lane is a
// normal answer — it hands the buyer to the contact form, never a guess.
(function initFreightWidget() {
  const widget = document.getElementById('freight-widget');
  if (!widget) return;
  const form = widget.querySelector('.fw-form');
  const result = widget.querySelector('.fw-result');
  if (!form || !result) return;
  const zipEl = widget.querySelector('.fw-zip');
  const qtyEl = widget.querySelector('.fw-qty');
  const emailRow = widget.querySelector('.fw-email');
  const emailEl = widget.querySelector('.fw-email-input');
  let quoteId = null;

  const money = (n) => '$' + Math.round(Number(n)).toLocaleString('en-US');
  const num = (n) => Number(n).toLocaleString('en-US');

  function show(html, tone) {
    result.innerHTML = html;
    result.classList.remove('mf-result--ok', 'mf-result--err');
    if (tone) result.classList.add(tone);
    result.hidden = false;
  }

  function rangeFor(est, mode) {
    return est[mode] || null;
  }

  function renderEstimate(data) {
    const est = data.estimate || {};
    const mode = est.recommended_mode || est.mode || 'ltl';
    const primary = rangeFor(est, mode) || rangeFor(est, 'ltl') || rangeFor(est, 'partial');
    if (!primary) {
      show('WE’LL QUOTE THIS LANE BY HAND — <a href="#contact-form">SEND THE REQUEST BELOW</a>', 'mf-result--err');
      return false;
    }
    const bits = ['◉ EST. ' + money(primary.low) + '–' + money(primary.high)];
    if (est.miles) bits.push('~' + num(est.miles) + ' MI');
    if (est.transit_days) bits.push('~' + num(est.transit_days) + ' DAYS');
    let html = bits.join(' · ');
    if (est.mode === 'both') {
      const altKey = (mode === 'ltl') ? 'partial' : 'ltl';
      const alt = rangeFor(est, altKey);
      if (alt) {
        html += '<span class="fw-alt">' + altKey.toUpperCase() + ' ALT. ' +
          money(alt.low) + '–' + money(alt.high) + '</span>';
      }
    }
    show(html, 'mf-result--ok');
    return true;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const dest = (zipEl.value || '').trim();
    if (!/^\d{5}$/.test(dest)) {
      show('✗ ENTER A 5-DIGIT US ZIP CODE.', 'mf-result--err');
      return;
    }
    const payload = {lot_id: widget.dataset.lotId, dest_zip: dest};
    const qty = parseInt(qtyEl && qtyEl.value, 10);
    if (qty > 0) payload.quantity = qty;

    const btn = form.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'PRICING…';
    if (emailRow) emailRow.hidden = true;
    quoteId = null;

    try {
      const r = await fetch('/freight-estimate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (r.status === 429) {
        show('TOO MANY ESTIMATES — GIVE IT A MINUTE.', 'mf-result--err');
        return;
      }
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Estimate failed');
      if (data.ok === false) {
        show('WE’LL QUOTE THIS LANE BY HAND — <a href="#contact-form">SEND THE REQUEST BELOW</a>', 'mf-result--err');
        return;
      }
      if (renderEstimate(data) && emailRow && data.quote_id) {
        quoteId = data.quote_id;
        emailRow.hidden = false;
      }
    } catch (err) {
      show('✗ COULDN’T REACH THE PRICER — TRY THAT AGAIN IN A MOMENT.', 'mf-result--err');
    } finally {
      btn.disabled = false; btn.textContent = btnText;
    }
  });

  emailRow?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = (emailEl.value || '').trim();
    if (!quoteId || !email) return;
    const btn = emailRow.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'SENDING…';
    try {
      const r = await fetch('/freight-estimate/email', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({quote_id: quoteId, email: email}),
      });
      if (!r.ok) throw new Error('send failed');
      emailRow.hidden = true;
      show('SENT — WE’LL FOLLOW UP.', 'mf-result--ok');
    } catch (err) {
      show('✗ COULDN’T SAVE THAT EMAIL — TRY AGAIN OR USE THE FORM BELOW.', 'mf-result--err');
    } finally {
      btn.disabled = false; btn.textContent = btnText;
    }
  });
})();

// ─── reserve form (deposit checkout) ────────────────────────────────────
// The math here is DISPLAY ONLY. The server re-derives every cent from the
// lot's own price before it talks to Stripe, so a tampered field changes what
// the buyer reads and nothing else.
(function initReserveForm() {
  const form = document.getElementById('reserve-form');
  if (!form) return;
  const result = form.querySelector('.mf-result');
  const qtyEl = form.querySelector('[name="quantity"]');
  const out = {
    subtotal: document.getElementById('quote-subtotal'),
    dueNow: document.getElementById('quote-due-now'),
    balance: document.getElementById('quote-balance'),
  };
  const price = parseFloat(form.dataset.price || '0') || 0;
  const pct = parseFloat(form.dataset.pct || '0') || 0;
  const minCents = parseInt(form.dataset.minCents || '0', 10) || 0;
  const maxQty = parseInt(form.dataset.maxQty || '0', 10) || 0;

  const dollars = (cents) => '$' + (cents / 100).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const kindNow = () => {
    const picked = form.querySelector('[name="kind"]:checked');
    return picked ? picked.value : 'deposit';
  };

  function show(text, tone) {
    if (!result) return;
    result.textContent = text;
    result.classList.remove('mf-result--ok', 'mf-result--err');
    if (tone) result.classList.add(tone);
    result.hidden = false;
  }

  function recompute() {
    let qty = parseInt(qtyEl && qtyEl.value, 10);
    if (!(qty > 0)) qty = 0;
    if (maxQty && qty > maxQty) qty = maxQty;
    const subtotal = Math.round(qty * price * 100);
    const dueNow = (kindNow() === 'full')
      ? subtotal
      : Math.min(subtotal, Math.max(Math.ceil(subtotal * pct), minCents));
    const balance = subtotal - dueNow;
    if (out.subtotal) out.subtotal.textContent = dollars(subtotal);
    if (out.dueNow) out.dueNow.textContent = dollars(dueNow);
    if (out.balance) out.balance.textContent = dollars(balance);
  }

  qtyEl?.addEventListener('input', recompute);
  form.querySelectorAll('[name="kind"]').forEach(el => {
    el.addEventListener('change', recompute);
  });
  recompute();

  if (location.search.indexOf('canceled=1') !== -1 || form.dataset.canceled === '1') {
    show('CHECKOUT CANCELED — YOUR LOT IS STILL HERE.', null);
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {
      quantity: parseInt(fd.get('quantity'), 10),
      kind: kindNow(),
      name: (fd.get('name') || '').trim(),
      email: (fd.get('email') || '').trim(),
      phone: (fd.get('phone') || '').trim(),
    };
    if (!payload.email && !payload.phone) {
      show('✗ We need an email or a phone number to reach you.', 'mf-result--err');
      return;
    }

    const btn = form.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'OPENING CHECKOUT…';
    try {
      const r = await fetch(form.dataset.endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.detail || 'Could not start checkout');
      window.location = data.url;
    } catch (err) {
      show('✗ ' + (err.message || 'Something broke. Please try again or contact us.'),
           'mf-result--err');
      btn.disabled = false; btn.textContent = btnText;
    }
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
