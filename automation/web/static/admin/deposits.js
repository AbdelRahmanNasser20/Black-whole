// static/admin/deposits.js — Deposits tab (11), the money ledger.
// Ported out of app.js during the PR #64 rebase onto the E1/F admin rebuild: one read site
// (GET /api/deposits → UI.load: skeleton twin shipped in index.html → ready | empty | error, keepOld on
// refresh/refilter/after a mutation), two mutations (PATCH status, DELETE → UI.pending on the clicked
// button), plus the live deposit rule (GET/PATCH /api/settings). The status filter lives in the URL
// (`?dstatus=`, absent = all) so a filtered ledger is refresh-safe; the key is `dstatus` and not `status`
// because Inquiries and Subscribers already own `status`.
//
// Two things this UI deliberately does NOT do:
//   · no refund button — refunds happen in the Stripe dashboard and come back as a webhook, so this stays
//     a mirror of Stripe rather than a rival source of truth;
//   · no inventory writes — a paid deposit doesn't decrement anything (v1).
// `→ CANCELED` is offered only on pending/processing rows because those are the only ones where "this
// reservation is dead" is an operator judgement call rather than something Stripe already told us.
import {$, $$, toast, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const filter = {status: ''};            // mirrors ?dstatus= ('' = all)

const list = () => $('#dep-list');

const MONEY = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});

function money(cents) {
  if (cents == null || isNaN(cents)) return '—';
  return MONEY.format(cents / 100);
}

function when(ts) { return ts ? String(ts).replace('T', ' ').slice(0, 16) : ''; }

// ───────── URL ↔ controls ─────────

function statusValues() { return $$('#dep-status-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const p = getParams();
  filter.status = statusValues().includes(p.dstatus || '') ? (p.dstatus || '') : '';
  syncControls();
}

function syncControls() {
  $$('#dep-status-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.status;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setStatus(value) {
  filter.status = statusValues().includes(value) ? value : '';
  syncControls();
  setParams({dstatus: filter.status || null});
  return loadDeposits({keepOld: true});
}

// ───────── render ─────────

function depositCard(d) {
  const card = document.createElement('article');
  card.className = `card card--deposit is-${escapeAttr(d.status)}`;
  card.dataset.id = d.id;
  const lotLink = d.lot_id
    ? `<a href="/listings/${encodeURIComponent(d.lot_id)}" target="_blank" rel="noopener">LOT #${escapeHtml(d.lot_id)}</a>`
    : '<span class="dep-unlinked">no lot</span>';
  const stamps = [
    d.created_at ? `created ${when(d.created_at)}` : '',
    d.paid_at ? `paid ${when(d.paid_at)}` : '',
    d.refunded_at ? `refunded ${when(d.refunded_at)}` : '',
  ].filter(Boolean).join(' · ');
  const ids = [d.stripe_session_id, d.stripe_payment_intent].filter(Boolean)
    .map(x => `<span class="dep-sid">${escapeHtml(x)}</span>`).join('');
  const canCancel = d.status === 'pending' || d.status === 'processing';
  card.innerHTML = `
    <div class="card-band">
      <span class="dep-kind dep-kind--${escapeAttr(d.kind)}">${d.kind === 'full' ? 'FULL' : 'DEPOSIT'}</span>
      <span class="dep-amount">${money(d.amount_cents)}</span>
      <span class="dep-qty">× ${d.quantity != null ? escapeHtml(d.quantity) : '?'}</span>
      <span class="card-cat dep-lot">${lotLink}</span>
      <span class="card-timer dep-when">${escapeHtml(stamps)}</span>
      <span class="badge dep-status">${escapeHtml(d.status)}</span>
    </div>
    <div class="card-title dep-who">${escapeHtml(d.buyer_name || '—')}</div>
    <div class="card-meta dep-contact">
      ${d.buyer_email ? `<a href="mailto:${escapeAttr(d.buyer_email)}">${escapeHtml(d.buyer_email)}</a>` : ''}
      ${d.buyer_phone ? `<a href="tel:${escapeAttr(d.buyer_phone)}">${escapeHtml(d.buyer_phone)}</a>` : ''}
      ${d.payment_method ? `<span>· ${escapeHtml(d.payment_method)}</span>` : ''}
    </div>
    <div class="dep-order">
      order ${money(d.subtotal_cents)}
      · balance at delivery ${money((d.subtotal_cents || 0) - (d.amount_cents || 0))}
    </div>
    ${ids ? `<div class="dep-ids">${ids}</div>` : ''}
    ${d.failure_reason ? `<div class="dep-fail">${escapeHtml(d.failure_reason)}</div>` : ''}
    ${d.admin_note ? `<blockquote class="dep-note">${escapeHtml(d.admin_note)}</blockquote>` : ''}
    <div class="card-chips dep-actions">
      ${canCancel ? '<button type="button" class="btn btn-small" data-cancel>→ CANCELED</button>' : ''}
      ${d.status === 'paid' ? '<span class="dep-hint">Refund in the Stripe dashboard — status syncs here automatically.</span>' : ''}
      <button type="button" class="btn btn-small btn-ghost dep-danger" data-delete>✕ delete</button>
    </div>
  `;
  card.querySelector('[data-cancel]')?.addEventListener('click', (e) => {
    if (!confirm(`Mark deposit #${d.id} canceled? This does not refund anything in Stripe.`)) return;
    return pending(e.currentTarget, '…', async () => {
      try {
        await api(`/api/deposits/${d.id}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: 'canceled'}),
        });
        toast(`Deposit #${d.id} → canceled`, 'ok');
        await loadDeposits({keepOld: true});
      } catch (err) {
        toast('Status change failed: ' + (err.message || err), 'err');
      }
    });
  });
  card.querySelector('[data-delete]').addEventListener('click', (e) => {
    if (!confirm(`Delete deposit #${d.id}? The Stripe record stays; only our row goes.`)) return;
    return pending(e.currentTarget, '…', async () => {
      try {
        await api(`/api/deposits/${d.id}`, {method: 'DELETE'});
        toast(`Deposit #${d.id} deleted.`, 'ok');
        await loadDeposits({keepOld: true});
      } catch (err) {
        toast('Delete failed: ' + (err.message || err), 'err');
      }
    });
  });
  return card;
}

function renderDeposits(items) {
  const frag = document.createDocumentFragment();
  for (const d of items) frag.appendChild(depositCard(d));
  return frag;
}

function emptyState() {
  if (filter.status) {
    return {
      glyph: '◌',
      title: `No ${filter.status} deposits`,
      body: 'Nothing carries this status right now.',
      cta: {label: 'Show all', onClick: () => setStatus('')},
    };
  }
  return {
    glyph: '◌',
    title: 'No deposits yet',
    body: 'Reservations land here the moment a buyer opens Stripe Checkout.',
    cta: {label: 'Open /listings', href: '/listings'},
  };
}

// ───────── load ─────────

async function loadDeposits({keepOld = false} = {}) {
  const el = list();
  if (!el) return;
  const url = '/api/deposits' + (filter.status ? `?status=${encodeURIComponent(filter.status)}` : '');
  return uiLoad(el, async ({signal}) => (await api(url, {signal})).items || [], {
    skeleton: 'card',
    count: 5,
    keepOld,
    render: renderDeposits,
    empty: emptyState(),
    errorMessage: (err) => "Couldn't load deposits. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
}

// ───────── the rule ─────────
// Stored as a fraction (0.15); shown as a percent (15) because that's how the operator thinks and how the
// storefront copy reads.

function settingsMsg(text, kind) {
  const el = $('#dep-settings-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'dep-settings-status' + (kind ? ` dep-msg-${kind}` : '');
}

async function loadDepositSettings() {
  try {
    const s = await api('/api/settings');
    const pct = $('#dep-pct'), min = $('#dep-min');
    if (pct && s.deposit_pct != null) pct.value = Math.round(Number(s.deposit_pct) * 1000) / 10;
    if (min && s.deposit_min_usd != null) min.value = s.deposit_min_usd;
    settingsMsg('');
  } catch (err) {
    settingsMsg('could not load rule: ' + (err.message || err), 'err');
  }
}

async function saveDepositSettings() {
  const pct = Number($('#dep-pct')?.value);
  const min = Number($('#dep-min')?.value);
  if (!isFinite(pct) || pct <= 0 || pct > 100) {
    settingsMsg('deposit % must be between 0 and 100', 'err');
    return;
  }
  if (!isFinite(min) || min < 0) {
    settingsMsg('minimum must be a positive dollar amount', 'err');
    return;
  }
  try {
    const s = await api('/api/settings', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        deposit_pct: Math.round((pct / 100) * 10000) / 10000,
        deposit_min_usd: Math.round(min),
      }),
    });
    settingsMsg(`saved · ${Math.round(Number(s.deposit_pct) * 1000) / 10}% · floor $${s.deposit_min_usd}`, 'ok');
    toast('Deposit rule saved.', 'ok');
  } catch (err) {
    settingsMsg('save failed: ' + (err.message || err), 'err');
    toast('Deposit rule save failed: ' + (err.message || err), 'err');
  }
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is loading — drop the state so a smoke on another tab is not blocked on a hidden pane.
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  readParams();
  $('#dep-settings-save')?.addEventListener('click', (e) => {
    pending(e.currentTarget, 'saving…', saveDepositSettings);
  });
  $('#dep-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ refreshing…', () => loadDeposits({keepOld: true}));
  });
  $$('#dep-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => setStatus(b.dataset.value)));
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch the ledger and the rule. */
export async function load() {
  readParams();
  const el = list();
  loadDepositSettings();
  return loadDeposits({keepOld: !!el && el.dataset.state === 'ready'});
}
