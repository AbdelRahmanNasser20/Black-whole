// static/site/platform/merge.js — after sign-in, move what the browser kept into the account.
//   saved searches (finds_store SEARCH_KEY) → POST /platform/api/finds, one each. A search leaves the browser
//     only when the account took it (200). The answer's own code (detail.error) decides the rest, never the
//     status alone:
//       alert_limit        the account is full: stop, keep the rest here, report `capped`.
//       alert_needs_filter the search is fine but too wide for an alert: store it with the alert off.
//       keyword_required   the account cannot hold a search with no keyword yet: skip it, keep it here.
//       any other 422      the server will never take it: skip it, keep it here.
//     Every skipped search is returned in `rejected` so the page can name it. Nothing is dropped silently.
//   stars (STAR_KEY) → POST /platform/api/saved/import, once per set. The stars stay in the browser because
//     the /platform page shows them there. A marker stops the same set from being sent again.
// Safe to call on every signed-in page: with nothing to move it makes no request. Never throws.
import {readPending, writePending, toQuery, starRefs} from './finds_store.js';
import {setPlan} from '/static/ui/plan.js';

const STAR_MARK = 'pf.stars.merged.v1';

async function post(url, body) {
  const res = await fetch(url, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  const detail = data && data.detail;
  return {status: res.status, data, error: detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.error : null};
}

export async function mergeAll() {
  const out = {searches: 0, stars: 0, kept: 0, capped: false, held: 0, rejected: []};
  try {
    // The plan decides the browser's star cap (free = 3). The server enforces the same cap.
    try {
      const me = await fetch('/platform/api/saved', {credentials: 'include'});
      if (me.ok) setPlan((await me.json()).plan);
    } catch { /* offline: keep the last known plan */ }
    let pending = readPending();
    for (const it of [...pending]) {
      const body = Object.fromEntries(toQuery(it));
      for (const k of ['max_bids', 'ending_within', 'min_price', 'max_price']) if (k in body) body[k] = Number(body[k]);
      if (it.name) body.name = it.name;
      body.alert = it.want_alert === true;
      let res = await post('/platform/api/finds', body);
      if (res.status === 422 && res.error === 'alert_needs_filter') {   // keep the search, leave the alert off
        res = await post('/platform/api/finds', {...body, alert: false});
        if (res.status === 200) out.held += 1;
      }
      if (res.status === 200) {
        if (res.data && res.data.alert_held) out.held += 1;
        pending = pending.filter((p) => p.id !== it.id);
        writePending(pending);
        out.searches += 1;
      } else if (res.status === 409 && res.error === 'alert_limit') {
        out.capped = true;                   // the account is full: the rest stays in this browser
        break;
      } else if (res.status === 422) {       // this one search was refused: keep it here, say so, go on
        out.rejected.push({search: it, error: res.error || 'invalid'});
      } else break;                          // 401/403/5xx: stop, keep what is left for the next visit
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
