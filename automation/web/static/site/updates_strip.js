// static/site/updates_strip.js — the "early access" email strip (templates/_updates_strip.html).
// One write: POST /subscribe {email, source: 'site_updates'}. The address is stored; nothing is sent from
// here, so no message promises a schedule. Loaded on pages with and without site.js, so it stands alone.
import {api, pending} from '../ui/state.js';

const form = document.querySelector('#updates-strip .updates-strip-form');
if (form) {
  const out = form.querySelector('.updates-strip-result');
  const say = (text, ok) => { out.textContent = text; out.classList.toggle('is-err', !ok); out.hidden = false; };
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = form.elements.email;
    const email = input.value.trim();
    if (!/^\S+@\S+\.\S+$/.test(email)) { say('Enter an email address.', false); input.focus(); return; }
    pending(form.querySelector('button[type="submit"]'), 'Saving…', () => api('/subscribe', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, source: form.elements.source.value}),
    })).then(() => { say('You are on the list.', true); form.reset(); })
      .catch((err) => say(err.status === 429 ? 'Too many tries from this address. Try again later.'
        : 'That did not save. Try again in a moment.', false));
  });
}
