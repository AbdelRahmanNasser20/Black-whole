// static/site/platform/merge.js — after sign-in, move what the browser kept into the account.
//   saved searches (finds_store SEARCH_KEY) → POST /platform/api/finds, one each. A search leaves the browser
//     only when the account took it (200). The account cap (409) stops the run and keeps the rest here.
//   stars (STAR_KEY) → POST /platform/api/saved/import, once per set. The stars stay in the browser because
//     the /platform page shows them there. A marker stops the same set from being sent again.
// Safe to call on every signed-in page: with nothing to move it makes no request. Never throws.
import {readPending, writePending, toQuery, starRefs} from './finds_store.js';

const STAR_MARK = 'pf.stars.merged.v1';

async function post(url, body) {
  const res = await fetch(url, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return res.status;
}

export async function mergeAll() {
  const out = {searches: 0, stars: 0, kept: 0};
  try {
    let pending = readPending();
    for (const it of [...pending]) {
      const body = Object.fromEntries(toQuery(it));
      for (const k of ['max_bids', 'ending_within', 'min_price', 'max_price']) if (k in body) body[k] = Number(body[k]);
      if (it.name) body.name = it.name;
      body.alert = it.want_alert === true;
      const status = await post('/platform/api/finds', body);
      if (status === 200) {
        pending = pending.filter((p) => p.id !== it.id);
        writePending(pending);
        out.searches += 1;
      } else if (status === 422) {          // the server will never take this one; drop it so it cannot block the rest
        pending = pending.filter((p) => p.id !== it.id);
        writePending(pending);
      } else break;                          // 401/403/409/5xx: stop, keep what is left for the next visit
    }
    out.kept = pending.length;
    const stars = starRefs();
    const mark = stars.map((s) => `${s.source}:${s.lot_key}`).sort().join('|');
    let done = '';
    try { done = window.localStorage.getItem(STAR_MARK) || ''; } catch { /* blocked storage */ }
    if (stars.length && mark !== done) {
      if (await post('/platform/api/saved/import', {lots: stars}) === 200) {
        out.stars = stars.length;
        try { window.localStorage.setItem(STAR_MARK, mark); } catch { /* blocked storage */ }
      }
    }
  } catch { /* offline or blocked: nothing moved, everything is still in the browser */ }
  return out;
}
