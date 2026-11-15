// static/deals/save_search.js — "Save this search" on /deals. Mounts into #feed-actions (the slot feed.js leaves).
// feed.js announces the current filters on document 'feed:params'; this file never reads the controls itself.
// Signed in: the search goes to the account (POST /platform/api/finds). No account, or the account call is
// refused: it is kept in this browser and shows on /platform/finds; merge.js moves it over after sign-in.
import {api, toast} from '../ui/state.js';
import {addPending, fromParams, hasSession, describe} from '../site/platform/finds_store.js';

const slot = document.getElementById('feed-actions');
let current = null;

function paint() {
  if (!slot) return;
  if (!slot.firstChild) {
    slot.innerHTML = '<button type="button" class="btn btn-ghost btn-small" id="feed-save-search">Save this search</button>'
      + ' <a class="feed-finds-link" href="/platform/finds">Saved searches</a>';
    slot.querySelector('#feed-save-search').addEventListener('click', save);
  }
  const btn = slot.querySelector('#feed-save-search');
  btn.disabled = !current;
  btn.title = current ? 'Save: ' + describe(current) : 'Set a search word or a filter first';
  btn.textContent = 'Save this search';
}

async function save() {
  if (!current) return;
  const btn = slot.querySelector('#feed-save-search');
  btn.disabled = true;
  let where = '';
  if (hasSession()) {
    try {
      await api('/platform/api/finds', {method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(current)});
      where = 'account';
    } catch (err) {
      if (err.status === 409) { toast('You have the most saved searches an account can hold. Delete one first.', 'err'); btn.disabled = false; return; }
      // 401/403 (signed out, not onboarded) or a server fault: keep it in the browser instead of losing it
    }
  }
  if (!where) {
    const r = addPending(current);
    if (!r.ok) {
      toast(r.reason === 'full' ? 'This browser already holds 10 saved searches. Delete one on the Saved searches page.'
        : 'This browser would not store the search.', 'err');
      btn.disabled = false; return;
    }
    where = r.reason === 'duplicate' ? 'duplicate' : 'browser';
  }
  btn.textContent = where === 'duplicate' ? 'Already saved' : 'Saved';
  toast(where === 'account' ? 'Search saved to your account.'
    : where === 'duplicate' ? 'This search is already saved.' : 'Search saved in this browser.', 'ok');
}

document.addEventListener('feed:params', (e) => {
  current = fromParams(e.detail.params);
  paint();
});
paint();
