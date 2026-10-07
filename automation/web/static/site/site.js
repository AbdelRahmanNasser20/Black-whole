/* Black Whole Liquidation — public site (static/site/site.js, was static/public.js).
   ES module: the one network write (POST /subscribe | /contact) goes through UI.pending + UI.api (plan §2 #68). */
import {api, pending} from '../ui/state.js';

// ─── first-touch attribution (lead analytics) ────────────────────────────
// Where did this visitor come from the FIRST time? Stored once in localStorage
// (first visit wins, 90-day life) and sent with every lead POST so the server
// can store it next to the lead (automation/attribution.py, migration 022).
// Values are clipped to 200 chars; the server clips again.
const ATTR_KEY = 'bw_attr';
const ATTR_TTL_MS = 90 * 864e5;
const OWN_HOST_RE = /(^|\.)(black-whole\.com|bwliquidation\.com|blackwholeliquidation\.com|blackwholesupply\.com|blackwholeservices\.com)$/;
const clip200 = (v) => (v == null ? '' : String(v)).slice(0, 200);

function readAttr() {
  try {
    const a = JSON.parse(localStorage.getItem(ATTR_KEY) || 'null');
    if (a && a.ts && Date.now() - a.ts < ATTR_TTL_MS) return a;
  } catch (e) { /* private mode / blocked storage: no memory, still fine */ }
  return null;
}

function captureAttr() {
  let a = readAttr();
  if (a) return a;
  const q = new URLSearchParams(location.search);
  let ref = '';
  try { ref = document.referrer ? new URL(document.referrer).host : ''; } catch (e) { ref = ''; }
  if (ref && (ref === location.host || OWN_HOST_RE.test(ref))) ref = '';
  a = {
    source: clip200(q.get('utm_source')), medium: clip200(q.get('utm_medium')),
    campaign: clip200(q.get('utm_campaign')), referrer: clip200(ref),
    landing: clip200(location.pathname), ts: Date.now(),
  };
  try { localStorage.setItem(ATTR_KEY, JSON.stringify(a)); } catch (e) { /* ignore */ }
  return a;
}

const ATTR = captureAttr();

function attribution() {
  const a = ATTR || {};
  return {
    source: a.source || '', medium: a.medium || '', campaign: a.campaign || '',
    referrer: a.referrer || '', landing: a.landing || '',
  };
}

function attrSource() {
  const a = attribution();
  return a.source || a.referrer || 'direct';
}

// Cloudflare Zaraz custom event. A no-op until the operator turns Zaraz on in
// the Cloudflare dashboard (docs/analytics_leads_plan.md) — then every lead
// shows up under Zaraz → Monitoring → Events as `lead`.
function zarazTrack(name, props) {
  try {
    if (window.zaraz && typeof window.zaraz.track === 'function') window.zaraz.track(name, props || {});
  } catch (e) { /* analytics must never break a form */ }
}

// tel: / mailto: clicks never reach the server on their own — beacon them to
// POST /event so they count as leads in the DB too. Never preventDefault.
document.addEventListener('click', (e) => {
  const a = e.target && e.target.closest && e.target.closest('a[href^="tel:"], a[href^="mailto:"]');
  if (!a) return;
  const kind = (a.getAttribute('href') || '').startsWith('tel:') ? 'tel_click' : 'mailto_click';
  const widget = document.getElementById('freight-widget');
  const lot = (widget && widget.dataset && widget.dataset.lotId) || '';
  zarazTrack('lead', {kind: kind, lot_id: lot, source: attrSource()});
  const body = JSON.stringify({kind: kind, lot_id: lot, attribution: attribution()});
  try {
    if (navigator.sendBeacon) {
      // Survives the page navigating away to the dialer / mail app.
      navigator.sendBeacon('/event', new Blob([body], {type: 'application/json'}));
    } else {
      api('/event', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: body, keepalive: true})
        .catch(() => {});
    }
  } catch (err) { /* ignore */ }
});

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
    payload.attribution = attribution();

    result.hidden = true;
    result.classList.remove('mf-result--ok', 'mf-result--err');
    if (endpoint === '/subscribe' && !payload.email && !payload.phone) {
      result.textContent = '✗ We need an email or a phone number to reach you.';
      result.classList.add('mf-result--err');
      result.hidden = false;
      return;
    }

    const btn = form.querySelector('button[type="submit"]');
    try {
      // UI.pending: disables + relabels the button for the life of the request, restores after.
      const data = await pending(btn, 'FILING…', () => api(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      })) || {};
      result.textContent = form.dataset.success ||
        ('◉ INQUIRY #' + data.id + ' FILED. WE\u2019LL BE IN TOUCH WITHIN 1 BUSINESS DAY.');
      result.classList.add('mf-result--ok');
      result.hidden = false;
      form.reset();
      zarazTrack('lead', {
        kind: endpoint === '/subscribe' ? 'subscribe' : 'contact',
        lot_id: payload.lot_id || '', source: attrSource(),
      });
    } catch (err) {
      result.textContent = '✗ ' + (err.message || 'Something broke. Please try again or email us.');
      result.classList.add('mf-result--err');
      result.hidden = false;
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
// One step: destination ZIP + email + phone, then the range. The server stores the request BEFORE it
// answers, so a number on screen always has a lead on file behind it. An unquotable lane is a normal
// answer (we already have the contact details and follow up by hand) — never a guess.
(function initFreightWidget() {
  const widget = document.getElementById('freight-widget');
  if (!widget) return;
  const form = widget.querySelector('.fw-form');
  const result = widget.querySelector('.fw-result');
  if (!form || !result) return;
  const zipEl = widget.querySelector('.fw-zip');
  const qtyEl = widget.querySelector('.fw-qty');
  const emailEl = widget.querySelector('.fw-email-input');
  const phoneEl = widget.querySelector('.fw-phone-input');

  const money = (n) => '$' + Math.round(Number(n)).toLocaleString('en-US');
  const num = (n) => Number(n).toLocaleString('en-US');

  function show(html, tone) {
    result.innerHTML = html;
    result.classList.remove('mf-result--ok', 'mf-result--err');
    if (tone) result.classList.add(tone);
    result.hidden = false;
  }

  function flag(el, bad) { if (el) el.classList.toggle('is-bad', !!bad); }

  // "30033-1234" / "30033 1234" → "30033". Anything else goes to the server untouched: it refuses
  // the lane, stores the request, and we follow up by hand.
  function cleanZip(raw) {
    const m = /^(\d{5})(?:[-\s]?\d{4})$/.exec(raw);
    return m ? m[1] : raw;
  }

  // Mirror of the server's rule (app.py `_clean_phone`): 10 US digits, optional leading 1.
  function phoneOk(raw) {
    let d = raw.replace(/\D/g, '');
    if (d.length === 11 && d[0] === '1') d = d.slice(1);
    return d.length === 10 && !'01'.includes(d[0]) && !'01'.includes(d[3]);
  }

  function rangeFor(est, mode) {
    return est[mode] || null;
  }

  function renderEstimate(data) {
    const est = data.estimate || {};
    const mode = est.recommended_mode || est.mode || 'ltl';
    const primary = rangeFor(est, mode) || rangeFor(est, 'ltl') || rangeFor(est, 'partial');
    if (!primary) {
      show('WE’LL QUOTE THIS LANE BY HAND AND GET BACK TO YOU.', 'mf-result--ok');
      return;
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
    if (data.available) {
      html += '<span class="fw-alt">NOTE: ' + num(data.available) + ' AVAILABLE ON THIS LOT</span>';
    }
    html += '<span class="fw-alt">REQUEST SAVED — WE’LL FOLLOW UP.</span>';
    show(html, 'mf-result--ok');
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const dest = cleanZip((zipEl.value || '').trim());
    const email = (emailEl.value || '').trim();
    const phone = (phoneEl.value || '').trim();

    // All digits but not 5 (or ZIP+4, already trimmed) is a typo — catch it here. The server would
    // refuse to price it anyway (it never pads "3003" into 03003). Anything non-numeric (a Canadian
    // postal code) is sent as typed: no price, but the request is stored and answered by hand.
    const zipBad = !dest || (/^\d+$/.test(dest) && dest.length !== 5);
    const emailBad = !/^\S+@\S+\.\S+$/.test(email);
    const phoneBad = !phoneOk(phone);
    flag(zipEl, zipBad); flag(emailEl, emailBad); flag(phoneEl, phoneBad);
    if (zipBad) { show('✗ ENTER A 5-DIGIT DELIVERY ZIP CODE.', 'mf-result--err'); return; }
    if (emailBad) { show('✗ ENTER AN EMAIL WE CAN SEND THE QUOTE TO.', 'mf-result--err'); return; }
    if (phoneBad) { show('✗ ENTER A 10-DIGIT US PHONE NUMBER.', 'mf-result--err'); return; }

    const payload = {lot_id: widget.dataset.lotId, dest_zip: dest, email: email, phone: phone};
    const qty = parseInt(qtyEl && qtyEl.value, 10);
    if (qty > 0) payload.quantity = qty;
    payload.attribution = attribution();
    const leadEvent = () => zarazTrack('lead', {kind: 'freight_quote', lot_id: payload.lot_id, source: attrSource()});

    // UI.api throws on non-2xx with .status + the server's `detail` as .message; an unquotable
    // lane and a failed save are 200s carrying {ok: false, reason}, so they are normal answers.
    await pending(form.querySelector('button[type="submit"]'), 'PRICING…', async () => {
      try {
        const data = await api('/freight-estimate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        if (data.ok === false) {
          if (data.reason === 'not_saved') {
            show('✗ WE COULDN’T SAVE YOUR REQUEST — TRY AGAIN IN A MINUTE, OR <a href="#contact-form">USE THE FORM BELOW</a>.', 'mf-result--err');
          } else if (data.saved === false) {
            // Unquotable AND not on file: the contact form below is the durable path.
            show('WE CAN’T PRICE THIS LANE AUTOMATICALLY — <a href="#contact-form">SEND THE REQUEST BELOW</a> AND WE’LL QUOTE IT BY HAND.', 'mf-result--err');
          } else {
            show('GOT IT — WE’LL QUOTE THIS LANE BY HAND AND GET BACK TO YOU.', 'mf-result--ok');
            leadEvent();   // unquotable but on file — still a lead
          }
          return;
        }
        renderEstimate(data);
        leadEvent();
      } catch (err) {
        if (err.status === 429) {
          show('TOO MANY ESTIMATES — GIVE IT A MINUTE.', 'mf-result--err');
        } else if (err.status === 400 && /phone/i.test(err.message || '')) {
          flag(phoneEl, true);
          show('✗ ENTER A 10-DIGIT US PHONE NUMBER.', 'mf-result--err');
        } else if (err.status === 400 && /email/i.test(err.message || '')) {
          flag(emailEl, true);
          show('✗ ENTER AN EMAIL WE CAN SEND THE QUOTE TO.', 'mf-result--err');
        } else {
          show('✗ COULDN’T REACH THE PRICER — TRY THAT AGAIN IN A MOMENT.', 'mf-result--err');
        }
      }
    });
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
      attribution: attribution(),
    };
    if (!payload.email && !payload.phone) {
      show('✗ We need an email or a phone number to reach you.', 'mf-result--err');
      return;
    }

    const btn = form.querySelector('button[type="submit"]');
    const btnText = btn.textContent;
    btn.disabled = true; btn.textContent = 'OPENING CHECKOUT…';
    try {
      // UI.api, not UI.pending: on success we hand off to Stripe, and the button has to stay
      // disabled across that navigation — pending() would re-enable it in its finally.
      const data = await api(form.dataset.endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!data.ok) throw new Error(data.detail || 'Could not start checkout');
      // data-endpoint is "/reserve/{lot_id}/checkout" — the lot id is the 2nd segment.
      zarazTrack('checkout_start', {lot_id: (form.dataset.endpoint || '').split('/')[2] || '', kind: payload.kind, source: attrSource()});
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
