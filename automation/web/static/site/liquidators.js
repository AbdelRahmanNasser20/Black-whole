// static/site/liquidators.js — GET /liquidators. ES module.
// The one write on the page is the request-access form: POST /contact (existing endpoint, unchanged).
// `inquiries` has no source column, so the lead is tagged at the start of `message` (no schema change).
import {api, pending} from '../ui/state.js';

export const LEAD_TAG = '[LIQUIDATORS PAGE]';
const INTEREST = {find: 'find distressed businesses', sell: 'sell to the buyer network', both: 'find and sell', other: 'something else'};

export function accessPayload(fields) {
  const clean = k => String(fields[k] || '').trim();
  const parts = [`Wants to: ${INTEREST[clean('interest')] || clean('interest') || 'not given'}`];
  if (clean('company')) parts.push(`Company: ${clean('company')}`);
  if (clean('note')) parts.push(clean('note'));
  // The tag leads the message so it survives the 240-character Telegram preview and any truncation.
  return {kind: 'buy', name: clean('name'), email: clean('email'), message: `${LEAD_TAG} ${parts.join(' | ')}`};
}

function initAccess() {
  const form = document.getElementById('lq-access-form');
  if (!form) return;
  const result = form.querySelector('.mf-result');
  const show = (text, ok) => {
    result.textContent = text;
    result.classList.toggle('mf-result--ok', ok); result.classList.toggle('mf-result--err', !ok);
    result.hidden = false;
  };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const payload = accessPayload(Object.fromEntries(new FormData(form).entries()));
    if (!payload.name) { show('Add your name so I know who is asking.', false); form.elements.name.focus(); return; }
    if (!/^\S+@\S+\.\S+$/.test(payload.email)) { show('Add an email address I can reply to.', false); form.elements.email.focus(); return; }
    try {
      const data = await pending(form.querySelector('button[type="submit"]'), 'REQUESTING…', () => api('/contact', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      })) || {};
      show(`Access requested. Request #${data.id} is in the inbox, and the reply will come by email.`, true);
      form.reset();
    } catch (err) {
      show(`The request did not send${err.message ? ': ' + err.message : ''}. Try again in a moment.`, false);
    }
  });
}

initAccess();
