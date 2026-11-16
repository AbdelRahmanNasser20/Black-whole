// static/site/platform/save_feed.js — "Save this search" on /platform/deals.
// That page has its own filter names (deals.js). This maps them to the saved-search ones (the /deals filters):
//   q → q · site → site · state → state · "No bids" → max_bids 0 · Ending 24 h / 7 d → ending_within 24 / 168 ·
//   Max bid → max_price. The category is NOT carried: this feed shows each auction site's own category text,
//   and a saved search filters on the shared category list. Closed lots cannot be saved (a search is open lots).
// Reads the controls at click time, so it needs nothing from deals.js.
import {addPending, clean, hasSession} from './finds_store.js';

const btn = document.getElementById('pd-save');
const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
const ENDING = {'24h': 24, '7d': 168};

function current() {
  const nobids = document.getElementById('pd-nobids');
  return clean({
    q: val('pd-q'), site: val('pd-site'), state: val('pd-state'),
    max_bids: nobids && nobids.checked ? 0 : null,
    ending_within: ENDING[val('pd-ending')] || null,
    max_price: val('pd-maxbid') === '' ? null : val('pd-maxbid'),
  });
}

function done(text) {
  btn.textContent = text;
  setTimeout(() => { btn.textContent = 'Save this search'; }, 2500);
}

async function save() {
  const f = current();
  if (!f) { done('Set a filter first'); return; }
  if (f.q && f.q.length < 2) { done('Search word too short'); return; }
  if (hasSession()) {
    try {
      const res = await fetch('/platform/api/finds', {method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(f)});
      if (res.ok) { done('Saved to your account'); return; }
      if (res.status === 409) { done('Limit reached. Delete one first'); return; }
    } catch { /* offline: keep it in the browser below */ }
  }
  const r = addPending(f);
  done(r.ok ? (r.reason === 'duplicate' ? 'Already saved' : 'Saved in this browser')
    : r.reason === 'full' ? 'Limit reached. Delete one first' : 'Could not save');
}

if (btn) btn.addEventListener('click', save);
