// static/deals/platform.js — Save/Unsave on /deals for signed-in platform users. ES module.
// Loaded only when the server rendered <meta name="platform-authed">. Buttons come from card.js;
// this file only handles clicks, and touches the DOM through textContent / dataset (no innerHTML).
import {api} from '../ui/state.js';

document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest && ev.target.closest('.card-save');
  if (!btn || btn.disabled) return;
  const source = btn.dataset.saveSource;
  const key = btn.dataset.saveKey;
  const wasSaved = btn.dataset.saved === '1';
  btn.disabled = true;
  try {
    if (wasSaved) {
      const path = key.split('/').map(encodeURIComponent).join('/');
      await api(`/platform/api/saved/${encodeURIComponent(source)}/${path}`, {method: 'DELETE', credentials: 'include'});
    } else {
      await api('/platform/api/saved', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source, lot_key: key}),
      });
    }
    btn.dataset.saved = wasSaved ? '0' : '1';
    btn.textContent = wasSaved ? 'Save' : 'Saved';
  } catch (e) {
    if (e.status === 401) { location.href = '/platform/signup'; return; }
    if (e.status === 403) { location.href = '/platform/onboarding'; return; }
    btn.textContent = 'Retry';
  } finally {
    btn.disabled = false;
  }
});
