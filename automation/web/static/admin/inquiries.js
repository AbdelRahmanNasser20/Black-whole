// static/admin/inquiries.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, escapeHtml, escapeAttr} from './shared.js';

// ─────────────────────────── Inquiries tab ───────────────────────────

var _inqStatusFilter = '';

async function loadInquiries() {
  const el = $('#inq-list');
  el.innerHTML = '<div class="drafts-empty">Loading…</div>';
  try {
    const r = await fetch('/api/inquiries' + (_inqStatusFilter ? `?status=${_inqStatusFilter}` : ''));
    const data = await r.json();
    renderInquiries(data.items || []);
  } catch (e) {
    el.innerHTML = `<div class="drafts-empty">Load failed: ${e}</div>`;
  }
}

function renderInquiries(items) {
  const el = $('#inq-list');
  if (!items.length) {
    el.innerHTML = '<div class="drafts-empty">No inquiries yet. Share /listings with customers to collect leads.</div>';
    return;
  }
  el.innerHTML = '';
  for (const q of items) {
    const card = document.createElement('article');
    card.className = `inq-card inq-${q.status}`;
    card.dataset.id = q.id;
    const when = q.created_at ? q.created_at.replace('T', ' ').slice(0, 16) : '';
    const lotLink = q.lot_id
      ? `<a href="/listings/${encodeURIComponent(q.lot_id)}" target="_blank">LOT #${escapeHtml(q.lot_id)}</a>`
      : '<span class="inq-unlinked mono tiny">UNLINKED</span>';
    card.innerHTML = `
      <header class="inq-head">
        <span class="inq-kind inq-kind--${q.kind}">${q.kind === 'buy' ? 'BUY' : 'SELL'}</span>
        <span class="inq-lot">${lotLink}</span>
        <span class="inq-when mono tiny">${when}</span>
        <span class="inq-status-pill inq-status-${q.status}">${q.status}</span>
      </header>
      <div class="inq-body">
        <div class="inq-name">${escapeHtml(q.name)}</div>
        <div class="inq-contact mono tiny">
          ${q.email ? `<a href="mailto:${escapeAttr(q.email)}">${escapeHtml(q.email)}</a>` : ''}
          ${q.phone ? `<a href="tel:${escapeAttr(q.phone)}">${escapeHtml(q.phone)}</a>` : ''}
          ${q.quantity_interested ? `· qty ${q.quantity_interested}` : ''}
        </div>
        ${q.message ? `<blockquote class="inq-msg">${escapeHtml(q.message)}</blockquote>` : ''}
      </div>
      <footer class="inq-foot">
        ${['new','contacted','closed'].filter(s => s !== q.status)
          .map(s => `<button class="btn btn-small" data-set-status="${s}">→ ${s}</button>`).join('')}
        <button class="btn btn-small btn-ghost inv-danger" data-delete>✕ delete</button>
      </footer>
    `;
    card.querySelectorAll('[data-set-status]').forEach(b => b.addEventListener('click', async () => {
      const status = b.dataset.setStatus;
      await withButtonLoading(b, '…', async () => {
        try {
          await apiFetch(`/api/inquiries/${q.id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status}),
          });
          toast(`Inquiry #${q.id} → ${status}`, 'ok');
          loadInquiries();
        } catch (err) {
          toast('Status change failed: ' + (err.message || err), 'err');
        }
      });
    }));
    card.querySelector('[data-delete]').addEventListener('click', async (e) => {
      if (!confirm(`Delete inquiry #${q.id}?`)) return;
      await withButtonLoading(e.currentTarget, '…', async () => {
        try {
          await apiFetch(`/api/inquiries/${q.id}`, {method: 'DELETE'});
          toast(`Inquiry #${q.id} deleted.`, 'ok');
          loadInquiries();
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
  $('#inq-refresh')?.addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '↻ loading…', loadInquiries);
  });

  $$('#inq-status-filter .seg-btn').forEach(b => b.addEventListener('click', () => {
    $$('#inq-status-filter .seg-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    _inqStatusFilter = b.dataset.value;
    loadInquiries();
  }));
}

export async function load() { return loadInquiries(); }
