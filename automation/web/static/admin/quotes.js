// static/admin/quotes.js — Sales › Quotes: every freight request, and whether it has been answered.
// One read site (GET /api/freight-quotes → UI.load: skeleton twin shipped in index.html → ready | empty | error,
// keepOld on refresh/refilter/after a mutation) and three mutations (PATCH status, PATCH note, POST carrier-check →
// UI.pending on the clicked button). The status filter lives in the URL (`?qstatus=`, absent = open = new + answered,
// `all` = everything) so a filtered inbox is refresh-safe; the key is `qstatus` because Inquiries and Subscribers
// already own `status` and Deposits owns `dstatus`.
//
// What a card shows, top to bottom: who asked and for what lane · how to reach them · the range the SITE showed the
// buyer next to the cheapest REAL carrier price (Warp's multi-carrier feed, fetched in the background after the
// request) with a flag when the two disagree · the operator's note · the next statuses.
// Before migration 021 the list still loads (old columns); a banner says which command adds status + carrier prices.
import {$, $$, toast, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const STATUSES = ['new', 'answered', 'won', 'lost', 'junk'];
const DEFAULT_FILTER = 'open';                // new + answered: what still needs the operator
const filter = {status: DEFAULT_FILTER};      // mirrors ?qstatus= (absent = open, 'all' = no filter)
let meta = {schema_ready: true, migration_hint: ''};

const list = () => $('#quo-list');

const MONEY = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
const money = (n) => (n == null || isNaN(n)) ? '—' : MONEY.format(n);
const num = (n) => Number(n).toLocaleString('en-US');
const when = (ts) => ts ? String(ts).replace('T', ' ').slice(0, 16) : '';

function prettyPhone(p) {
  const d = String(p || '');
  return /^\d{10}$/.test(d) ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : d;
}

// ───────── URL ↔ controls ─────────

function filterValues() { return $$('#quo-status-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const raw = getParams().qstatus;
  filter.status = filterValues().includes(raw || '') && raw ? raw : DEFAULT_FILTER;
  syncControls();
}

function syncControls() {
  $$('#quo-status-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.status;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setFilter(value) {
  filter.status = filterValues().includes(value) ? value : DEFAULT_FILTER;
  syncControls();
  setParams({qstatus: filter.status === DEFAULT_FILTER ? null : filter.status});
  return loadQuotes({keepOld: true});
}

// ───────── render ─────────

const CHECK_LABEL = {site_low: 'SITE LOW', site_high: 'SITE HIGH'};
const CHECK_TITLE = {
  site_low: 'Real carriers start well above the range the site showed. Tell the buyer before they plan around it.',
  site_high: 'Real carriers start well below the range the site showed.',
};

function shownLine(q) {
  if (q.unquotable_reason) {
    return `<div class="quote-line quote-line--warn"><span class="quote-k">SITE SHOWED</span>
      <span>no price — quote by hand</span><span class="quote-why">${escapeHtml(q.unquotable_reason)}</span></div>`;
  }
  if (q.shown_low == null) return '';
  return `<div class="quote-line"><span class="quote-k">SITE SHOWED</span>
    <span class="quote-v">${money(q.shown_low)}–${money(q.shown_high)}</span>
    <span class="quote-dim">${escapeHtml(q.mode || 'ltl')} · ${escapeHtml(q.provider || 'estimator')}</span></div>`;
}

function carrierLine(q) {
  if (q.unquotable_reason) return '';
  const st = q.carrier_status;
  let body;
  if (st === 'ok' && q.carrier_low != null) {
    const flag = CHECK_LABEL[q.price_check]
      ? `<span class="quote-flag quote-flag--${escapeAttr(q.price_check)}" title="${escapeAttr(CHECK_TITLE[q.price_check])}">${CHECK_LABEL[q.price_check]}</span>`
      : '';
    body = `<span class="quote-v">${money(q.carrier_low)}</span>
      <span class="quote-dim">${escapeHtml(q.carrier_name || 'carrier')} · ${num(q.carrier_count || 0)} carriers</span>${flag}`;
  } else if (st === 'too_big') {
    body = '<span class="quote-dim">too big for LTL — quote by hand</span>';
  } else if (st === 'none') {
    body = '<span class="quote-dim">no carrier answered for this lane</span>';
  } else if (st === 'error') {
    body = '<span class="quote-dim">carrier check failed — try again</span>';
  } else {
    body = '<span class="quote-dim">not checked yet</span>';
  }
  const opts = (st === 'ok' && Array.isArray(q.carrier_options) && q.carrier_options.length > 1)
    ? `<details class="quote-options"><summary>${num(q.carrier_options.length)} cheapest</summary><ul>${
        q.carrier_options.map(o => `<li><span>${escapeHtml(o.carrier)}</span><span>${money(o.price_usd)}</span>` +
          `<span class="quote-dim">${o.transit_days != null ? escapeHtml(o.transit_days) + ' d' : ''}</span></li>`).join('')
      }</ul></details>`
    : '';
  return `<div class="quote-line"><span class="quote-k">CHEAPEST CARRIER</span>${body}</div>${opts}`;
}

function quoteCard(q) {
  const card = document.createElement('article');
  card.className = `card card--quote is-${escapeAttr(q.status)}`;
  card.dataset.id = q.id;
  const lot = q.lot_id
    ? `<a href="/listings/${encodeURIComponent(q.lot_id)}" target="_blank" rel="noopener">${escapeHtml(q.lot_title || ('LOT #' + q.lot_id))}</a>`
    : '<span class="quote-dim">no lot</span>';
  const lane = [
    `${num(q.quantity)} chairs`,
    `${escapeHtml(q.origin_zip || '?')} → ${escapeHtml(q.dest_zip || '?')}`,
    q.miles != null ? `${num(q.miles)} mi` : '',
  ].filter(Boolean).join(' · ');
  // `stock_is_current`: an older row has no stock snapshot, so the comparison is against the lot as it stands today.
  const stock = q.over_stock
    ? `<span class="quote-flag quote-flag--stock" title="The buyer asked for more than the lot ${q.stock_is_current ? 'holds now' : 'held at the time'}">ASKED ${num(q.quantity)} · LOT ${q.stock_is_current ? 'HAS' : 'HAD'} ${num(q.stock_compared)}</span>`
    : '';
  // Stored strings go into URL attributes here, so each is made safe for THAT context, not just HTML-escaped:
  // the address is percent-encoded (a "?bcc=" in it cannot become a mailto parameter), the phone is digits only,
  // and a thread link is rendered only when it is plainly https.
  const mailto = q.buyer_email ? 'mailto:' + encodeURIComponent(q.buyer_email).replace(/%40/g, '@') : '';
  const tel = String(q.buyer_phone || '').replace(/[^0-9+]/g, '');
  const thread = /^https:\/\//i.test(q.thread_url || '') ? q.thread_url : '';
  const contact = [
    q.buyer_email ? `<a href="${escapeAttr(mailto)}">${escapeHtml(q.buyer_email)}</a>` : '',
    q.buyer_phone ? `<a href="tel:${escapeAttr(tel)}">${escapeHtml(prettyPhone(q.buyer_phone))}</a>` : '',
    thread ? `<a href="${escapeAttr(thread)}" target="_blank" rel="noopener noreferrer">open thread ↗</a>` : '',
  ].filter(Boolean).join('');
  const canCheck = !q.unquotable_reason && q.origin_zip;
  card.innerHTML = `
    <div class="card-band">
      <span class="quote-source quote-source--${escapeAttr(q.source)}">${q.source === 'crm' ? 'CRM' : 'SITE'}</span>
      <span class="card-cat quote-lot">${lot}</span>
      <span class="card-timer quote-when">${escapeHtml(when(q.quoted_at))}</span>
      <span class="badge quote-status">${escapeHtml(q.status)}</span>
    </div>
    <div class="card-title quote-lane">#${escapeHtml(q.id)} · ${lane} ${stock}</div>
    <div class="card-meta quote-contact">${contact || '<span class="quote-nocontact">no contact left — nothing to answer</span>'}</div>
    ${shownLine(q)}
    ${carrierLine(q)}
    <label class="quote-note">
      <span class="quote-k">NOTE</span>
      <input type="text" maxlength="500" value="${escapeAttr(q.note || '')}" placeholder="what happened — called, emailed, price given…" data-note>
      <button type="button" class="btn btn-small btn-ghost" data-save-note>save</button>
    </label>
    <div class="card-chips quote-actions">
      ${STATUSES.filter(s => s !== q.status)
        .map(s => `<button type="button" class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
      ${canCheck ? '<button type="button" class="btn btn-small btn-ghost" data-carrier-check title="Asks ~17 carriers; takes 20–45 s">↻ carrier prices</button>' : ''}
    </div>
  `;

  const patch = (body, okMsg) => api(`/api/freight-quotes/${q.id}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(() => { toast(okMsg, 'ok'); return loadQuotes({keepOld: true}); });

  card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', () => {
    const status = b.dataset.setStatus;
    return pending(b, '…', () => patch({status}, `Quote #${q.id} → ${status}`)
      .catch(err => toast('Status change failed: ' + (err.message || err), 'err')));
  }));

  const noteEl = card.querySelector('[data-note]');
  const saveNote = (btn) => pending(btn, '…', () => patch({note: noteEl.value}, `Note saved on #${q.id}`)
    .catch(err => toast('Note not saved: ' + (err.message || err), 'err')));
  card.querySelector('[data-save-note]').addEventListener('click', (e) => saveNote(e.currentTarget));
  noteEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveNote(card.querySelector('[data-save-note]')); }
  });

  card.querySelector('[data-carrier-check]')?.addEventListener('click', (e) => {
    return pending(e.currentTarget, 'asking carriers… (20–45 s)', async () => {
      try {
        await api(`/api/freight-quotes/${q.id}/carrier-check`, {method: 'POST'});
        toast(`Carrier prices updated on #${q.id}`, 'ok');
        await loadQuotes({keepOld: true});
      } catch (err) {
        toast('Carrier check failed: ' + (err.message || err), 'err');
      }
    });
  });
  return card;
}

function renderQuotes(items) {
  const frag = document.createDocumentFragment();
  for (const q of items) frag.appendChild(quoteCard(q));
  return frag;
}

function renderBanner() {
  const el = $('#quo-banner');
  if (!el) return;
  if (meta.schema_ready) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.innerHTML = '<strong>Status, notes and carrier prices are off.</strong> ' +
    escapeHtml(meta.migration_hint || 'Migration 021 is not applied.');
}

function emptyState() {
  if (filter.status === 'open') {
    return {glyph: '◌', title: 'Nothing waiting on you', body: 'Every freight request has been closed out.',
            cta: {label: 'Show all', onClick: () => setFilter('all')}};
  }
  if (filter.status !== 'all') {
    return {glyph: '◌', title: `No ${filter.status} quotes`, body: 'Nothing carries this status right now.',
            cta: {label: 'Show open', onClick: () => setFilter(DEFAULT_FILTER)}};
  }
  return {glyph: '◌', title: 'No freight requests yet',
          body: 'A buyer who asks for a freight estimate on a lot page lands here.',
          cta: {label: 'Open /listings', href: '/listings'}};
}

// ───────── load ─────────

async function loadQuotes({keepOld = false} = {}) {
  const el = list();
  if (!el) return;
  const url = '/api/freight-quotes' + (filter.status === 'all' ? '' : `?status=${encodeURIComponent(filter.status)}`);
  return uiLoad(el, async ({signal}) => {
    const data = await api(url, {signal});
    meta = {schema_ready: data.schema_ready !== false, migration_hint: data.migration_hint || ''};
    renderBanner();
    return data.items || [];
  }, {
    skeleton: 'card',
    count: 5,
    keepOld,
    render: renderQuotes,
    empty: emptyState(),
    errorMessage: (err) => "Couldn't load freight quotes. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this view is activated (shell.js → load()),
  // nothing is loading — drop the state so a smoke on another tab is not blocked on a hidden pane.
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  readParams();
  $('#quo-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ refreshing…', () => loadQuotes({keepOld: true}));
  });
  $$('#quo-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => setFilter(b.dataset.value)));
}

/** View activation (and popstate via the shell): resync from the URL, then fetch — dimming old cards if we have any. */
export async function load() {
  readParams();
  const el = list();
  return loadQuotes({keepOld: !!el && el.dataset.state === 'ready'});
}
