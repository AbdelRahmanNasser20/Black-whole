// static/admin/lane.js — Sales › Lane quote: what real carriers charge for ANY lane, unpadded.
// One form (POST /api/freight/lane-quote → a table of every carrier, cheapest first, with the in-house estimate
// and the padded "site would show" number beside it), a lot picker that prefills origin + quantity + chair data
// from GET /api/inventory, and the live buyer padding (GET/PATCH /api/settings → freight_padding_pct), which is
// what the lot page adds to the cheapest carrier. Warp takes 20–45 s on a cold lane; the same lane again is instant
// (server-side memo). No URL keys of its own.
import {$, $$, toast, escapeHtml} from './shared.js';
import {pending, api} from '../ui/state.js';

const MONEY = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 2});
const money = (n) => (n == null || isNaN(n)) ? '—' : MONEY.format(n);
const whole = (n) => (n == null || isNaN(n)) ? '—' : '$' + Math.round(Number(n)).toLocaleString('en-US');
const num = (n) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');

const form = () => $('#lane-form');
const result = () => $('#lane-result');

// ───────── lots ─────────

let lots = [];

async function loadLots() {
  const sel = $('#lane-lot');
  if (!sel) return;
  try {
    const data = await api('/api/inventory');
    lots = (data.items || []).filter(l => l.zip_code || l.state);
    sel.innerHTML = '<option value="">— type a lane —</option>' + lots.map(l =>
      `<option value="${escapeHtml(l.lot_id)}">${escapeHtml(l.lot_id)} · ${escapeHtml(l.title || '')} · ${escapeHtml(l.zip_code || l.state || '')}` +
      `${l.quantity_remaining != null ? ' · ' + num(l.quantity_remaining) : ''}</option>`).join('');
  } catch (err) {
    toast('Could not load lots for the picker: ' + (err.message || err), 'err');
  }
}

function prefillFromLot() {
  const lot = lots.find(l => l.lot_id === $('#lane-lot')?.value);
  if (!lot) return;
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== '') el.value = v; };
  set('#lane-origin', lot.zip_code);
  set('#lane-qty', lot.quantity_remaining || lot.quantity_original);
  set('#lane-lb', lot.chair_weight_lb);
  set('#lane-per-pallet', lot.chairs_per_pallet);
  set('#lane-height', lot.pallet_height_in);
}

// ───────── render ─────────

function dropLabel(out) {
  if (!out.residential && !out.liftgate) return 'dock / forklift';
  if (out.residential && out.liftgate) return 'residential + liftgate';
  if (out.residential) return 'residential, no liftgate (buyer hand-unloads)';
  return 'liftgate, limited access';
}

function renderResult(out) {
  const el = result();
  if (!el) return;
  const s = out.shipment || {};
  const est = out.estimator;
  let estLine = '<span class="lane-dim">estimator refused this lane (offshore or unresolvable)</span>';
  if (est) {
    const mode = est.recommended_mode || est.mode || 'ltl';
    const low = est[`${mode}_low`], high = est[`${mode}_high`];
    estLine = `<span class="lane-v">${whole(low)}–${whole(high)}</span> <span class="lane-dim">${escapeHtml(mode)} · ~${num(est.miles)} mi · ~${num(est.transit_days)} days</span>`;
  }
  let carriers;
  if (out.carriers && out.carriers.length) {
    // Every option carries its own expiry (1–3 days) and whether Warp can book it — only Warp's own entries ever
    // can; a third-party carrier is "quote by hand with that carrier".
    carriers = `<table class="table lane-table"><thead><tr><th>#</th><th>carrier</th><th class="num">price</th><th class="num">transit</th><th>expires</th><th>book</th></tr></thead><tbody>${
      out.carriers.map((c, i) => `<tr${i === 0 ? ' class="is-cheapest"' : ''}><td>${i + 1}</td><td>${escapeHtml(c.carrier)}${c.service_level ? ` <span class="lane-dim">${escapeHtml(c.service_level)}</span>` : ''}</td>` +
        `<td class="num">${money(c.price_usd)}</td><td class="num">${c.transit_days != null ? escapeHtml(c.transit_days) + ' d' : '—'}</td>` +
        `<td>${c.expires_at ? escapeHtml(String(c.expires_at).replace('T', ' ').slice(0, 16)) : '—'}</td>` +
        `<td>${c.bookable ? '<span class="lane-flag">BOOKABLE</span>' : '<span class="lane-dim">quote only</span>'}</td></tr>`).join('')
    }</tbody></table>`;
  } else {
    const why = out.error || ({none: 'no carrier answered for this lane', too_big: 'too big for LTL — quote a partial/full truck by hand'})[out.carrier_status] || out.carrier_status || 'not asked';
    carriers = `<div class="lane-line lane-line--warn"><span class="lane-k">CARRIERS</span><span>none — ${escapeHtml(why)}</span></div>`;
  }
  el.innerHTML = `
    <div class="lane-line"><span class="lane-k">LANE</span>
      <span class="lane-v">${escapeHtml(out.origin_zip)} → ${escapeHtml(out.dest_zip)}</span>
      <span class="lane-dim">${num(out.quantity)} chairs · ${num(s.pallets)} pallet${s.pallets === 1 ? '' : 's'} × ${num(s.weight_lbs_per_pallet)} lb · ${num(s.height_in)} in · ${escapeHtml(dropLabel(out))}</span></div>
    <div class="lane-line"><span class="lane-k">CHAIR DATA</span>
      <span class="lane-dim">${num(s.lbs_per_chair)} lb/chair${s.lbs_per_chair_estimated ? ' (standard chair — unmeasured)' : ''} · ${num(s.chairs_per_pallet)} per pallet${out.lot_id ? ' · lot ' + escapeHtml(out.lot_id) : ''}</span></div>
    ${out.cheapest ? `<div class="lane-line"><span class="lane-k">CHEAPEST</span>
      <span class="lane-v">${money(out.cheapest.price_usd)}</span><span class="lane-dim">${escapeHtml(out.cheapest.carrier)} · ${num(out.carrier_count)} carriers</span>
      <span class="lane-flag" title="cheapest carrier + the padding below, rounded up to the dollar">SITE WOULD SHOW ${whole(out.padded_price)} (+${num(out.padding_pct)}%)</span></div>` : ''}
    <div class="lane-line"><span class="lane-k">ESTIMATOR</span>${estLine}</div>
    ${carriers}`;
  el.dataset.state = 'ready';
}

// ───────── submit ─────────

async function quoteLane() {
  const f = form();
  if (!f) return;
  const val = (id) => ($(id)?.value || '').trim();
  const body = {
    lot_id: val('#lane-lot') || null,
    origin_zip: val('#lane-origin') || null,
    dest_zip: val('#lane-dest'),
    quantity: val('#lane-qty') || null,
    lbs_per_chair: val('#lane-lb') || null,
    chairs_per_pallet: val('#lane-per-pallet') || null,
    pallet_height_in: val('#lane-height') || null,
    residential: !!$('#lane-residential')?.checked,
    liftgate: !!$('#lane-liftgate')?.checked,
  };
  if (!/^\d{5}$/.test(body.dest_zip)) { toast('Destination must be a 5-digit ZIP.', 'err'); return; }
  const el = result();
  if (el) { el.dataset.state = 'loading'; el.innerHTML = '<div class="lane-dim">asking carriers… (20–45 s on a new lane)</div>'; }
  try {
    renderResult(await api('/api/freight/lane-quote', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    }));
  } catch (err) {
    if (el) { el.dataset.state = 'error'; el.innerHTML = `<div class="lane-line lane-line--warn">${escapeHtml(err.message || String(err))}</div>`; }
    toast('Lane quote failed: ' + (err.message || err), 'err');
  }
}

// ───────── padding ─────────

function settingsMsg(text, kind) {
  const el = $('#lane-settings-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'lane-settings-status' + (kind ? ` lane-msg-${kind}` : '');
}

async function loadPadding() {
  try {
    const s = await api('/api/settings');
    const el = $('#lane-padding');
    if (el && s.freight_padding_pct != null) el.value = s.freight_padding_pct;
    settingsMsg('');
  } catch (err) {
    settingsMsg('could not load padding: ' + (err.message || err), 'err');
  }
}

async function savePadding() {
  const pct = Number($('#lane-padding')?.value);
  if (!isFinite(pct) || pct < 0 || pct > 100) { settingsMsg('padding must be between 0 and 100', 'err'); return; }
  try {
    const s = await api('/api/settings', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({freight_padding_pct: pct}),
    });
    settingsMsg(`saved · buyers see cheapest carrier +${s.freight_padding_pct}%`, 'ok');
    toast('Freight padding saved.', 'ok');
  } catch (err) {
    settingsMsg('save failed: ' + (err.message || err), 'err');
    toast('Padding save failed: ' + (err.message || err), 'err');
  }
}

// ───────── mount / load ─────────

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  form()?.addEventListener('submit', (e) => {
    e.preventDefault();
    pending(form().querySelector('button[type="submit"]'), 'asking carriers…', quoteLane);
  });
  $('#lane-lot')?.addEventListener('change', prefillFromLot);
  $('#lane-padding-save')?.addEventListener('click', (e) => pending(e.currentTarget, 'saving…', savePadding));
}

/** View activation: the lot picker and the padding, nothing else fetches until the operator asks. */
export async function load() {
  loadPadding();
  if (!lots.length) await loadLots();
}
