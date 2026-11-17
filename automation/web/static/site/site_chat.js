// static/site/site_chat.js — the buyer chat (templates/_site_chat.html).
// The server owns the script (automation/site_chat.py): this file draws what POST /chat/start and
// POST /chat/answer return, and sends back one answer at a time. Loaded on pages with and without site.js,
// so it stands alone. Every stored value is drawn with textContent, never as HTML.
import {api} from '../ui/state.js';

// ───────── what the browser remembers ─────────
// bw_chat   the open chat id, so a reload or the next page continues the same chat
// bw_vid    a random id for this browser + first visit + days seen (no name, no IP)
// bw_trail  the last 30 storefront pages, so the operator sees what a buyer looked at
// bw_attr   first-touch attribution, written by site.js on pages that load it
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value));
  } catch (e) { /* private mode: the chat still works, it just does not resume */ }
  return value === undefined ? null : value;
}

function visitor() {
  const today = new Date().toISOString().slice(0, 10);
  let v = store('bw_vid');
  if (!v || !v.id) {
    const id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + '-' + Math.random().toString(36).slice(2, 12));
    v = {id, first: Date.now(), days: 0, last: ''};
  }
  if (v.last !== today) { v.days = (v.days || 0) + 1; v.last = today; }
  store('bw_vid', v);
  return {id: v.id, first: v.first, days: v.days};
}

function trail() {
  const list = (store('bw_trail') || []).filter(e => e && typeof e.p === 'string');
  return list.slice(-30);
}

function recordPage() {
  const list = trail();
  const path = location.pathname.slice(0, 200);
  const last = list[list.length - 1];
  if (!last || last.p !== path) list.push({p: path, t: Math.floor(Date.now() / 1000)});
  store('bw_trail', list.slice(-30));
}

function attribution() {
  const a = store('bw_attr');
  if (a && a.landing) return {source: a.source || '', medium: a.medium || '', campaign: a.campaign || '', referrer: a.referrer || '', landing: a.landing || ''};
  const q = new URLSearchParams(location.search);
  let ref = '';
  try { ref = document.referrer ? new URL(document.referrer).host : ''; } catch (e) { ref = ''; }
  if (ref === location.host) ref = '';
  return {source: q.get('utm_source') || '', medium: q.get('utm_medium') || '', campaign: q.get('utm_campaign') || '', referrer: ref, landing: (trail()[0] || {}).p || location.pathname};
}

const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const wait = (ms) => new Promise(r => setTimeout(r, reducedMotion() ? 0 : ms));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function boot(root) {
  const launch = root.querySelector('.bwchat-launch');
  const panel = root.querySelector('.bwchat-panel');
  const log = root.querySelector('.bwchat-log');
  const options = root.querySelector('.bwchat-options');
  const form = root.querySelector('.bwchat-form');
  const input = root.querySelector('.bwchat-input');
  const send = root.querySelector('.bwchat-send');
  let sid = (store('bw_chat') || {}).sid || null;
  let started = false;
  let busy = false;
  let current = null;          // the prompt on screen
  let picked = new Set();      // lot ids ticked on the lots step

  recordPage();
  visitor();

  const scrollDown = () => { log.scrollTop = log.scrollHeight; };

  function say(text, who, fresh = true) {
    const node = el('div', `bwchat-msg is-${who}${fresh ? ' is-new' : ''}`, text);
    log.appendChild(node);
    scrollDown();
    return node;
  }

  async function botLines(lines) {
    for (const line of lines) {
      const dots = el('div', 'bwchat-msg is-bot');
      const t = el('span', 'bwchat-typing');
      t.setAttribute('aria-hidden', 'true');
      t.append(el('i'), el('i'), el('i'));
      dots.appendChild(t);
      log.appendChild(dots);
      scrollDown();
      await wait(Math.min(900, 320 + line.length * 6));
      dots.remove();
      say(line, 'bot');
    }
  }

  function card(c) {
    const node = el('article', 'bwchat-card');
    if (c.hero) {
      const img = el('img', 'bwchat-card-photo');
      img.src = c.hero; img.alt = c.title; img.loading = 'lazy';
      node.appendChild(img);
    }
    const body = el('div', 'bwchat-card-body');
    body.appendChild(el('h3', 'bwchat-card-title', c.title));
    if (c.price) {
      const price = el('div', 'bwchat-card-price', c.price + ' ');
      price.appendChild(el('small', '', `each`));
      body.appendChild(price);
    }
    const unit = c.quantity === 1 ? c.unit : c.unit + 's';
    body.appendChild(el('div', 'bwchat-card-meta', `${c.quantity.toLocaleString('en-US')} ${unit} in stock`));
    const where = c.distance_mi == null ? c.place
      : c.distance_mi < 10 ? `${c.place}, in your area`
      : `${c.place}, about ${c.distance_mi.toLocaleString('en-US')} miles from you`;
    if (where) body.appendChild(el('div', 'bwchat-card-meta', where));
    const link = el('a', 'bwchat-card-link', 'See the photos');
    link.href = c.url; link.target = '_blank'; link.rel = 'noopener';
    body.appendChild(link);
    const pick = el('button', 'bwchat-card-pick', 'Pick these chairs');
    pick.type = 'button';
    pick.setAttribute('aria-pressed', 'false');
    pick.addEventListener('click', () => {
      const on = !picked.has(c.lot_id);
      if (on && picked.size >= (current.max_picks || 3)) return;
      if (on) picked.add(c.lot_id); else picked.delete(c.lot_id);
      node.classList.toggle('is-picked', on);
      pick.setAttribute('aria-pressed', String(on));
      pick.textContent = on ? 'Picked ✓' : 'Pick these chairs';
      drawOptions();
    });
    body.appendChild(pick);
    node.appendChild(body);
    return node;
  }

  function option(label, value, primary = false) {
    const b = el('button', 'bwchat-opt' + (primary ? ' is-primary' : ''), label);
    b.type = 'button';
    b.addEventListener('click', () => answer(value, label));
    return b;
  }

  function drawOptions() {
    options.replaceChildren();
    if (!current || current.done) { options.hidden = true; return; }
    if (current.input === 'lots') {
      const n = picked.size;
      const go = option(n ? `Continue with ${n} picked` : 'Pick the chairs you like', [...picked], true);
      go.disabled = !n;
      options.append(go, option('None of these', 'none'));
    } else {
      current.options.forEach(o => options.appendChild(option(o, o)));
    }
    options.hidden = !options.children.length;
  }

  async function show(prompt, {instant = false} = {}) {
    current = prompt;
    picked = new Set();
    options.hidden = true;
    if (prompt.error) say(prompt.error, 'bot is-error');
    else if (instant) prompt.messages.forEach(m => say(m, 'bot', false));
    else await botLines(prompt.messages);
    if (prompt.cards && prompt.cards.length) {
      const strip = el('div', 'bwchat-cards');
      prompt.cards.forEach(c => strip.appendChild(card(c)));
      log.appendChild(strip);
      scrollDown();
    }
    drawOptions();
    form.hidden = !!prompt.done;
    scrollDown();              // the options bar just took room from the log
    input.placeholder = prompt.placeholder || 'Type your answer';
    input.inputMode = prompt.input === 'zip' ? 'numeric' : (prompt.input === 'phone' ? 'tel' : 'text');
    input.autocomplete = prompt.input === 'zip' ? 'postal-code' : (prompt.input === 'name' ? 'name' : (prompt.input === 'phone' ? 'tel' : 'off'));
    if (prompt.done) {
      store('bw_chat', null);
      try { if (window.zaraz && window.zaraz.track) window.zaraz.track('lead', {kind: 'site_chat'}); } catch (e) { /* analytics never breaks the chat */ }
    } else if (panel.contains(document.activeElement) || !instant) {
      input.focus({preventScroll: true});
    }
  }

  function fail(err) {
    const text = err && err.status === 429 ? 'Too many messages from this address. Please try again in an hour.'
      : err && err.status === 503 ? 'The chat is not available right now. Please use the contact form on this page.'
      : 'That did not send. Check your connection and try again.';
    say(text, 'bot is-error');
  }

  async function start() {
    if (started) return;
    started = true;
    try {
      const res = await post('/chat/start', {sid, page: location.pathname, trail: trail(), visitor: visitor(), attribution: attribution()});
      sid = res.sid;
      store('bw_chat', {sid});
      const resumed = res.history && res.history.length;
      (res.history || []).forEach(h => { say(h.q, 'bot', false); say(h.a, 'user', false); });
      await show(res, {instant: !!resumed});
    } catch (err) { started = false; fail(err); }
  }

  async function answer(value, label) {
    if (busy || !current || current.done) return;
    busy = true; send.disabled = true;
    options.querySelectorAll('button').forEach(b => { b.disabled = true; });
    say(label, 'user');
    try {
      const res = await post('/chat/answer', {sid, value, trail: trail(), attribution: attribution()});
      await show(res);
    } catch (err) {
      if (err && err.status === 404) { store('bw_chat', null); sid = null; started = false; log.replaceChildren(); await start(); }
      else { fail(err); drawOptions(); }
    } finally { busy = false; send.disabled = false; }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    input.value = '';
    answer(text, text);
  });

  function open() {
    root.dataset.open = 'true';
    panel.hidden = false;
    launch.setAttribute('aria-expanded', 'true');
    store('bw_chat_open', true);
    start().then(() => input.focus({preventScroll: true}));
  }
  function close() {
    root.dataset.open = 'false';
    panel.hidden = true;
    launch.setAttribute('aria-expanded', 'false');
    store('bw_chat_open', null);
    launch.focus();
  }
  launch.addEventListener('click', open);
  root.querySelector('.bwchat-close').addEventListener('click', close);
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });

  // A buyer who was mid-chat and moved to another page (for example "See the photos") finds it open.
  if (sid && store('bw_chat_open') && window.matchMedia('(min-width: 521px)').matches) open();
}

// Last on purpose: boot() can open the chat at once, and it needs every const above it.
const root = document.getElementById('bwchat');
if (root) boot(root);
