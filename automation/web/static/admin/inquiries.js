// static/admin/inquiries.js — Inquiries tab (plan §10 E-inquiries).
// One read site (GET /api/inquiries → UI.load: skeleton twin shipped in index.html → ready | empty | error, keepOld on
// refresh/refilter/after a mutation) and two mutations (PATCH status, DELETE → UI.pending on the clicked button).
// The status filter lives in the URL (`?status=new|contacted|closed`, absent = all) so a filtered inbox is refresh-safe
// and shareable; popstate reaches us through the shell's activateTab → load().
// Cards are the shared `.card` (components.css) with the `card--inquiry` modifier — see static/admin/inquiries.css.
import {$, $$, toast, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const STATUSES = ['new', 'contacted', 'closed'];
const filter = {status: ''};          // mirrors ?status= ('' = all)

const list = () => $('#inq-list');

// ───────── URL ↔ controls ─────────

function statusValues() { return $$('#inq-status-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const p = getParams();
  filter.status = statusValues().includes(p.status || '') ? (p.status || '') : '';
  syncControls();
}

function syncControls() {
  $$('#inq-status-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.status;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setStatus(value) {
  filter.status = value || '';
  syncControls();
  setParams({status: filter.status || null});
  return loadInquiries({keepOld: true});
}

// ───────── render ─────────

function inquiryCard(q) {
  const card = document.createElement('article');
  card.className = `card card--inquiry is-${escapeAttr(q.status)}`;
  card.dataset.id = q.id;
  const when = q.created_at ? q.created_at.replace('T', ' ').slice(0, 16) : '';
  const lotLink = q.lot_id
    ? `<a href="/listings/${encodeURIComponent(q.lot_id)}" target="_blank" rel="noopener">LOT #${escapeHtml(q.lot_id)}</a>`
    : '<span class="inquiry-unlinked">unlinked</span>';
  card.innerHTML = `
    <div class="card-band">
      <span class="inquiry-kind inquiry-kind--${escapeAttr(q.kind)}">${q.kind === 'buy' ? 'BUY' : 'SELL'}</span>
      <span class="card-cat inquiry-lot">${lotLink}</span>
      <span class="card-timer inquiry-when">${escapeHtml(when)}</span>
      <span class="badge inquiry-status">${escapeHtml(q.status)}</span>
    </div>
    <div class="card-title inquiry-who">${escapeHtml(q.name)}</div>
    <div class="card-meta inquiry-contact">
      ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
      ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
      ${q.quantity_interested ? `<span>· qty ${escapeHtml(q.quantity_interested)}</span>` : ''}
    </div>
    ${q.message ? `<blockquote class="inquiry-msg">${escapeHtml(q.message)}</blockquote>` : ''}
    <div class="card-chips inquiry-actions">
      ${STATUSES.filter(s => s !== q.status)
        .map(s => `<button type="button" class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
      <button type="button" class="btn btn-small btn-ghost inquiry-danger" data-delete>✕ delete</button>
    </div>
  `;
  card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', () => {
    const status = b.dataset.setStatus;
    return pending(b, '…', async () => {
      try {
        await api(`/api/inquiries/${q.id}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status}),
        });
        toast(`Inquiry #${q.id} → ${status}`, 'ok');
        await loadInquiries({keepOld: true});
      } catch (err) {
        toast('Status change failed: ' + (err.message || err), 'err');
      }
    });
  }));
  card.querySelector('[data-delete]').addEventListener('click', (e) => {
    if (!confirm(`Delete inquiry #${q.id}?`)) return;
    return pending(e.currentTarget, '…', async () => {
      try {
        await api(`/api/inquiries/${q.id}`, {method: 'DELETE'});
        toast(`Inquiry #${q.id} deleted.`, 'ok');
        await loadInquiries({keepOld: true});
      } catch (err) {
        toast('Delete failed: ' + (err.message || err), 'err');
      }
    });
  });
  return card;
}

function renderInquiries(items) {
  const frag = document.createDocumentFragment();
  for (const q of items) frag.appendChild(inquiryCard(q));
  return frag;
}

function emptyState() {
  if (filter.status) {
    return {
      glyph: '◌',
      title: `No ${filter.status} inquiries`,
      body: 'Nothing carries this status right now.',
      cta: {label: 'Show all', onClick: () => setStatus('')},
    };
  }
  return {
    glyph: '◌',
    title: 'No inquiries yet',
    body: 'Share /listings with customers to collect leads.',
    cta: {label: 'Open /listings', href: '/listings'},
  };
}

// ───────── load ─────────

// Skeleton kind is `card` (band + 4 lines): the twin of an inquiry card (band · name · contact · message · actions),
// and the same markup the pane ships server-side via skeleton_cards(5) — so first paint and refetch do not reflow.
async function loadInquiries({keepOld = false} = {}) {
  const el = list();
  if (!el) return;
  const url = '/api/inquiries' + (filter.status ? `?status=${encodeURIComponent(filter.status)}` : '');
  return uiLoad(el, async ({signal}) => (await api(url, {signal})).items || [], {
    skeleton: 'card',
    count: 5,
    keepOld,
    render: renderInquiries,
    empty: emptyState(),
    errorMessage: (err) => "Couldn't load inquiries. " + (err.status
      ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.`
      : "The database didn't answer in 15 s."),
  });
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js → load()),
  // nothing is loading — drop the state so a smoke on another tab is not blocked on a hidden pane.
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  readParams();
  $('#inq-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ refreshing…', () => loadInquiries({keepOld: true}));
  });
  $$('#inq-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => setStatus(b.dataset.value)));
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch — dimming old cards if we have any. */
export async function load() {
  readParams();
  const el = list();
  return loadInquiries({keepOld: !!el && el.dataset.state === 'ready'});
}
