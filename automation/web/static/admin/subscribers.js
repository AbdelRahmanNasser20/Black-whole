// static/admin/subscribers.js — Subscribers tab (plan §10 E-subscribers).
// The read (GET /api/subscribers) goes through UI.load (skeleton → ready | empty | error, keepOld on refresh),
// the two mutations (PATCH status, DELETE) through UI.pending. The status filter lives in the URL (?status=).
import {$, $$, toast, escapeHtml, escapeAttr, getParams, setParams} from './shared.js';
import {load as uiLoad, pending, api} from '../ui/state.js';

const STATUSES = ['new', 'contacted', 'matched', 'unsubscribed'];

const SUB_LABELS = {
  use_case: {church: 'church', event_venue: 'event venue', wedding_rental: 'wedding rental',
             restaurant: 'restaurant', school: 'school', reseller: 'reseller', other: 'other'},
  timeline: {asap: 'ASAP', month: 'within a month', flexible: 'flexible'},
  budget_per_chair: {under_5: '<$5/chair', '5_10': '$5–10/chair', '10_20': '$10–20/chair', '20_plus': '$20+/chair'},
  delivery: {pickup: 'pickup', delivery: 'ship', either: 'either'},
};

const filter = {status: ''};            // mirrors ?status=
const list = () => $('#sub-list');

// ───────── URL ↔ controls ─────────

function statusValues() { return $$('#sub-status-filter .seg-btn').map(b => b.dataset.value); }

function readParams() {
  const p = getParams();
  filter.status = statusValues().includes(p.status || '') ? (p.status || '') : '';
  syncControls();
}

function syncControls() {
  $$('#sub-status-filter .seg-btn').forEach(b => {
    const on = b.dataset.value === filter.status;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
  });
}

function setStatus(value) {
  filter.status = statusValues().includes(value) ? value : '';
  setParams({status: filter.status});
  syncControls();
  loadSubscribers({keepOld: true});
}

// ───────── data ─────────

function renderCount(n) {
  const el = $('#sub-count');
  if (!el) return;
  if (n == null) el.textContent = '';
  else el.textContent = filter.status ? `${n} ${filter.status}` : `${n} signup${n === 1 ? '' : 's'}`;
}

function emptyArgs() {
  if (filter.status) {
    return {title: `No ${filter.status} signups`, body: 'Nobody carries this status right now.',
            cta: {label: 'Show all', onClick: () => setStatus('')}};
  }
  return {glyph: '◌', title: 'No alert signups yet', body: 'The public form on /listings#alerts feeds this list.',
          cta: {label: 'Open the signup form', href: '/listings#alerts'}};
}

async function loadSubscribers({keepOld = false} = {}) {
  const el = list();
  if (!el) return undefined;
  const qs = new URLSearchParams();
  if (filter.status) qs.set('status', filter.status);
  const url = '/api/subscribers' + (qs.toString() ? '?' + qs.toString() : '');
  return uiLoad(el, ({signal}) => api(url, {signal}), {
    skeleton: 'row', count: 5, keepOld,
    isEmpty(data) {
      const items = data.items || [];
      renderCount(items.length);
      return !items.length;
    },
    empty: emptyArgs(),
    render: (data) => cardsFragment(data.items || []),
    errorMessage: (err) => err.status ? `Couldn't load subscribers. The server said ${err.status}.`
                                      : "Couldn't load subscribers. The database didn't answer in 15 s.",
  });
}

// ───────── render ─────────

function fmtWhen(iso) { return iso ? String(iso).replace('T', ' ').slice(0, 16) : ''; }

function prefsLine(q) {
  const geo = [q.city, q.state, q.zip_code].filter(Boolean).join(' ');
  return [
    q.quantity_wanted ? `qty ${q.quantity_wanted}` : '',
    geo,
    SUB_LABELS.use_case[q.use_case] || q.use_case || '',
    q.chair_type || '',
    SUB_LABELS.timeline[q.timeline] || q.timeline || '',
    SUB_LABELS.budget_per_chair[q.budget_per_chair] || q.budget_per_chair || '',
    SUB_LABELS.delivery[q.delivery] || q.delivery || '',
  ].filter(Boolean).join(' · ');
}

function cardHtml(q) {
  const prefs = prefsLine(q);
  return `
    <header class="sub-head">
      <span class="sub-kind">ALERT</span>
      <span class="sub-src">${escapeHtml(q.source || '')}</span>
      <span class="sub-when">${escapeHtml(fmtWhen(q.created_at))}</span>
      <span class="badge sub-status sub-status-${escapeAttr(q.status)}">${escapeHtml(q.status)}</span>
    </header>
    <div class="sub-body">
      <div class="sub-name">${escapeHtml(q.name || '—')}</div>
      <div class="sub-contact">
        ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
        ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
      </div>
      ${prefs ? `<div class="sub-contact">${escapeHtml(prefs)}</div>` : ''}
      ${q.notes ? `<blockquote class="sub-notes">${escapeHtml(q.notes)}</blockquote>` : ''}
    </div>
    <footer class="sub-foot">
      ${STATUSES.filter(s => s !== q.status)
        .map(s => `<button type="button" class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
      <button type="button" class="btn btn-small btn-ghost sub-danger" data-delete>✕ delete</button>
    </footer>`;
}

function cardsFragment(items) {
  const frag = document.createDocumentFragment();
  for (const q of items) {
    const card = document.createElement('article');
    card.className = `card card-sub sub-${q.status}`;
    card.dataset.id = q.id;
    card.innerHTML = cardHtml(q);
    frag.appendChild(card);
  }
  return frag;
}

// ───────── mutations (delegated — cards are re-rendered on every load) ─────────

async function onListClick(e) {
  const btn = e.target.closest('button[data-set-status], button[data-delete]');
  if (!btn) return;
  const card = btn.closest('.card-sub');
  const id = card?.dataset.id;
  if (!id) return;

  if (btn.dataset.setStatus) {
    const status = btn.dataset.setStatus;
    await pending(btn, '…', async () => {
      try {
        await api(`/api/subscribers/${id}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status}),
        });
        toast(`Subscriber #${id} → ${status}`, 'ok');
        loadSubscribers({keepOld: true});
      } catch (err) {
        toast('Status change failed: ' + (err.message || err), 'err');
      }
    });
    return;
  }

  if (!confirm(`Delete subscriber #${id}?`)) return;
  await pending(btn, '…', async () => {
    try {
      await api(`/api/subscribers/${id}`, {method: 'DELETE'});
      toast(`Subscriber #${id} deleted.`, 'ok');
      loadSubscribers({keepOld: true});
    } catch (err) {
      toast('Delete failed: ' + (err.message || err), 'err');
    }
  });
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;

  // The pane ships its skeleton twin inside data-state="loading". Until this tab is activated (shell.js →
  // load()), nothing is actually loading — drop the state so a smoke on another tab is not blocked on a
  // hidden pane. load() re-sets it when the tab opens.
  const el = list();
  const pane = el?.closest('[data-pane]');
  if (el && pane?.hidden) { delete el.dataset.state; el.removeAttribute('aria-busy'); }

  el?.addEventListener('click', onListClick);

  $('#sub-refresh')?.addEventListener('click', (e) => {
    pending(e.currentTarget, '↻ loading…', () => loadSubscribers({keepOld: true}));
  });

  $$('#sub-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => setStatus(b.dataset.value)));
}

/** Tab activation (and popstate via the shell): resync from the URL, then fetch — dimming old cards if we have any. */
export async function load() {
  readParams();
  const el = list();
  return loadSubscribers({keepOld: !!el && el.dataset.state === 'ready'});
}
