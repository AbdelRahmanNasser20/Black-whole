// static/admin/subscribers.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, escapeHtml, escapeAttr} from './shared.js';

// ─────────────────────────── Subscribers tab ───────────────────────────

var _subStatusFilter = '';

const SUB_LABELS = {
  use_case: {church: 'church', event_venue: 'event venue', wedding_rental: 'wedding rental',
             restaurant: 'restaurant', school: 'school', reseller: 'reseller', other: 'other'},
  timeline: {asap: 'ASAP', month: 'within a month', flexible: 'flexible'},
  budget_per_chair: {under_5: '<$5/chair', '5_10': '$5–10/chair', '10_20': '$10–20/chair', '20_plus': '$20+/chair'},
  delivery: {pickup: 'pickup', delivery: 'ship', either: 'either'},
};

async function loadSubscribers() {
  const el = $('#sub-list');
  el.innerHTML = '<div class="drafts-empty">Loading…</div>';
  try {
    const r = await fetch('/api/subscribers' + (_subStatusFilter ? `?status=${_subStatusFilter}` : ''));
    const data = await r.json();
    renderSubscribers(data.items || []);
  } catch (e) {
    el.innerHTML = `<div class="drafts-empty">Load failed: ${e}</div>`;
  }
}

function renderSubscribers(items) {
  const el = $('#sub-list');
  if (!items.length) {
    el.innerHTML = '<div class="drafts-empty">No alert signups yet. The form lives at /listings#alerts.</div>';
    return;
  }
  el.innerHTML = '';
  for (const q of items) {
    const card = document.createElement('article');
    card.className = `inq-card inq-${q.status}`;
    card.dataset.id = q.id;
    const when = q.created_at ? String(q.created_at).replace('T', ' ').slice(0, 16) : '';
    const geo = [q.city, q.state, q.zip_code].filter(Boolean).join(' ');
    const prefs = [
      q.quantity_wanted ? `qty ${q.quantity_wanted}` : '',
      geo,
      SUB_LABELS.use_case[q.use_case] || q.use_case || '',
      q.chair_type || '',
      SUB_LABELS.timeline[q.timeline] || q.timeline || '',
      SUB_LABELS.budget_per_chair[q.budget_per_chair] || q.budget_per_chair || '',
      SUB_LABELS.delivery[q.delivery] || q.delivery || '',
    ].filter(Boolean).join(' · ');
    card.innerHTML = `
      <header class="inq-head">
        <span class="inq-kind inq-kind--buy">ALERT</span>
        <span class="inq-lot mono tiny">${escapeHtml(q.source || '')}</span>
        <span class="inq-when mono tiny">${when}</span>
        <span class="inq-status-pill inq-status-${q.status}">${q.status}</span>
      </header>
      <div class="inq-body">
        <div class="inq-name">${escapeHtml(q.name || '—')}</div>
        <div class="inq-contact mono tiny">
          ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
          ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
        </div>
        ${prefs ? `<div class="inq-contact mono tiny">${escapeHtml(prefs)}</div>` : ''}
        ${q.notes ? `<blockquote class="inq-msg">${escapeHtml(q.notes)}</blockquote>` : ''}
      </div>
      <footer class="inq-foot">
        ${['new','contacted','matched','unsubscribed'].filter(s => s !== q.status)
          .map(s => `<button class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
        <button class="btn btn-small btn-ghost inv-danger" data-delete>✕ delete</button>
      </footer>
    `;
    card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', async () => {
      const status = b.dataset.setStatus;
      await withButtonLoading(b, '…', async () => {
        try {
          await apiFetch(`/api/subscribers/${q.id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status}),
          });
          toast(`Subscriber #${q.id} → ${status}`, 'ok');
          loadSubscribers();
        } catch (err) {
          toast('Status change failed: ' + (err.message || err), 'err');
        }
      });
    }));
    card.querySelector('[data-delete]').addEventListener('click', async (e) => {
      if (!confirm(`Delete subscriber #${q.id}?`)) return;
      await withButtonLoading(e.currentTarget, '…', async () => {
        try {
          await apiFetch(`/api/subscribers/${q.id}`, {method: 'DELETE'});
          toast(`Subscriber #${q.id} deleted.`, 'ok');
          loadSubscribers();
        } catch (err) {
          toast('Delete failed: ' + (err.message || err), 'err');
        }
      });
    });
    el.appendChild(card);
  }
}

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  $('#sub-refresh')?.addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '↻ loading…', loadSubscribers);
  });

  $$('#sub-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
    $$('#sub-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    _subStatusFilter = b.dataset.value;
    loadSubscribers();
  }));
}

export async function load() { return loadSubscribers(); }
