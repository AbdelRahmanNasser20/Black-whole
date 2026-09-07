// static/admin/launcher.js — split verbatim from app.js (Workstream F). Bodies unchanged; only import/export/mount added.
import {$, $$, toast, withButtonLoading, apiFetch, hooks} from './shared.js';

// ───────── launcher ─────────

const consoleEl = $('#console');
const showEvents = $('#show-events');
const autoscroll = $('#autoscroll');

function _bindToggleIndicator(checkbox, indicatorId) {
  const ind = document.getElementById(indicatorId);
  if (!checkbox || !ind) return;
  const sync = () => {
    ind.dataset.on = checkbox.checked ? '1' : '0';
    ind.textContent = checkbox.checked ? 'ON' : 'OFF';
  };
  checkbox.addEventListener('change', sync);
  sync();
}

function appendLine(stream, data) {
  // Hide raw event lines if user toggled off
  if (stream === 'event' && !showEvents.checked) return;

  const line = document.createElement('span');
  line.className = `console-line ${stream}`;
  const ts = new Date().toTimeString().slice(0, 8);

  let body;
  if (stream === 'event') {
    const ev = typeof data === 'string' ? JSON.parse(data) : data;
    body = `${ev.kind}${ev.phase ? ':' + ev.phase : ''} → ${
      Object.entries(ev)
        .filter(([k]) => !['ts','kind','phase'].includes(k))
        .map(([k,v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join(' ')
    }`;
  } else {
    body = data;
  }

  line.innerHTML = `<span class="ts">${ts}</span><span class="tag">${stream}</span><span class="body"></span>`;
  line.querySelector('.body').textContent = body;
  consoleEl.appendChild(line);

  if (autoscroll.checked) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function setPhase(name, status, extras = {}) {
  const card = document.querySelector(`.phase[data-phase="${name}"]`);
  if (!card) return;
  card.dataset.state = status;
  const pill = $('.phase-status', card);
  pill.textContent = status;
  pill.dataset.state = status;

  const body = $('[data-body]', card);
  body.innerHTML = '';
  const dl = document.createElement('dl');
  dl.className = 'kv';
  for (const [k, v] of Object.entries(extras)) {
    if (k === 'status' || v == null || v === '') continue;
    const dt = document.createElement('dt');  dt.textContent = k;
    const dd = document.createElement('dd');
    if ((k === 'url' || k === 'fb_url' || k === 'ebay_url') && typeof v === 'string') {
      const a = document.createElement('a');
      a.href = v; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = v.length > 60 ? v.slice(0, 57) + '…' : v;
      dd.appendChild(a);
    } else if (typeof v === 'object') {
      dd.textContent = JSON.stringify(v).slice(0, 90);
    } else {
      dd.textContent = String(v);
    }
    dl.appendChild(dt); dl.appendChild(dd);
  }
  body.appendChild(dl);
}

function applyState(s) {
  if (s.phases) {
    for (const [name, info] of Object.entries(s.phases)) {
      setPhase(name, info.status || 'pending', info);
    }
  }
  $('#run-btn').disabled = (s.status === 'running');
  $('#cancel-btn').disabled = (s.status !== 'running');
  if (s.suggested_price && s.confirmed_price == null) {
    $('#price-prompt').hidden = false;
    $('#pp-suggested').textContent = `$${s.suggested_price}`;
    $('#pp-input').value = s.suggested_price;
  } else if (s.confirmed_price != null) {
    $('#price-prompt').hidden = true;
  }
  if (s.queue !== undefined) renderQueueStrip(s.queue);
}

function renderQueueStrip(queue) {
  const strip = $('#queue-strip');
  if (!strip) return;
  if (!queue || !queue.length) {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;
  $('#queue-count').textContent = String(queue.length);
  const urlsEl = $('#queue-urls');
  urlsEl.innerHTML = '';
  queue.slice(0, 3).forEach(item => {
    const chip = document.createElement('span');
    chip.className = 'queue-chip';
    chip.title = item.url;
    chip.textContent = shortUrl(item.url);
    urlsEl.appendChild(chip);
  });
  if (queue.length > 3) {
    const more = document.createElement('span');
    more.className = 'queue-chip queue-chip-more';
    more.textContent = `+${queue.length - 3}`;
    urlsEl.appendChild(more);
  }
}

function shortUrl(u) {
  try {
    const url = new URL(u);
    const parts = url.pathname.split('/').filter(Boolean);
    return parts.slice(-2).join('/') || url.host;
  } catch { return u.slice(-40); }
}

// ▶ is "list everywhere" by default; the legacy scrape/eBay pipeline is a checkbox.
const _modeBox = document.querySelector('#launch-form [name="mode_pipeline"]');

// ── SSE ──
let es;
function connectStream() {
  if (es) es.close();
  es = new EventSource('/api/runs/stream');
  const dot = $('#conn-dot');

  es.addEventListener('open', () => dot.classList.add('live'));
  es.addEventListener('error', () => dot.classList.remove('live'));

  es.addEventListener('queue', (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.data && msg.data.queue !== undefined) renderQueueStrip(msg.data.queue);
    } catch (err) { console.warn(err); }
  });

  for (const stream of ['stdout', 'stderr', 'event', 'system']) {
    es.addEventListener(stream, (e) => {
      try {
        const msg = JSON.parse(e.data);
        appendLine(msg.stream, msg.data);
        if (msg.stream === 'event') {
          const ev = msg.data;
          if (ev.kind === 'phase') {
            setPhase(ev.phase, ev.status || 'pending', ev);
          } else if (ev.kind === 'price' && ev.suggested && ev.confirmed == null) {
            $('#price-prompt').hidden = false;
            $('#pp-suggested').textContent = `$${ev.suggested}`;
            $('#pp-input').value = ev.suggested;
          } else if (ev.kind === 'price' && ev.confirmed != null) {
            // Auto-accept fired or another client confirmed — drop the stale UI.
            $('#price-prompt').hidden = true;
          } else if (ev.kind === 'run' && ev.status === 'finished') {
            $('#run-btn').disabled = false;
            $('#cancel-btn').disabled = true;
          }
        }
      } catch (err) { console.warn(err); }
    });
  }
}

// ── boot ──

hooks.applyState = applyState;
export {applyState, connectStream};

let mounted = false;
export function mount() {
  if (mounted) return;
  mounted = true;
  _bindToggleIndicator(showEvents, 'show-events-state');
  _bindToggleIndicator(autoscroll, 'autoscroll-state');

  // ── form ──
  $('#launch-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const pipeline = fd.get('mode_pipeline') === 'on';
    const channels = ['site', 'fb', 'business'].filter(c => fd.get('ch_' + c) === 'on');
    const payload = pipeline ? {
      mode: 'pipeline',
      url: fd.get('url'),
      skip_dewatermark: fd.get('skip_dewatermark') === 'on',
      skip_fb: fd.get('skip_fb') === 'on',
      skip_ebay: fd.get('skip_ebay') === 'on',
      price: fd.get('price') ? parseInt(fd.get('price'), 10) : null,
    } : {
      mode: 'channels',
      url: fd.get('url'),
      price: fd.get('price') ? parseInt(fd.get('price'), 10) : null,
      title: (fd.get('title') || '').trim(),
      blurb: (fd.get('blurb') || '').trim(),
      split: (fd.get('split') || '').trim(),
      channels,
    };
    consoleEl.innerHTML = '';
    $$('.phase').forEach(p => setPhase(p.dataset.phase, 'pending', {}));

    const res = await fetch('/api/runs/start', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({detail: res.statusText}));
      appendLine('stderr', `[start failed] ${err.detail || res.statusText}`);
      return;
    }
    $('#run-btn').disabled = true;
    $('#cancel-btn').disabled = false;
  });

  if (_modeBox) {
    const sync = () => {
      const on = _modeBox.checked;
      document.querySelectorAll('#launch-form .pipeline-only').forEach(el => el.hidden = !on);
      document.querySelectorAll('#launch-form .opts-copy').forEach(el => el.hidden = on);
      const lbl = $('#run-btn-label');
      if (lbl) lbl.textContent = on ? 'Run pipeline' : 'List everywhere';
    };
    _modeBox.addEventListener('change', sync);
    sync();
  }

  $('#cancel-btn').addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '…cancelling', async () => {
      try {
        await apiFetch('/api/runs/cancel', {method: 'POST'});
        toast('Cancel requested.', 'info');
      } catch (err) {
        toast('Cancel failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#queue-clear').addEventListener('click', (e) => {
    withButtonLoading(e.currentTarget, '…clearing', async () => {
      try {
        await apiFetch('/api/runs/queue/clear', {method: 'POST'});
        toast('Run queue cleared.', 'ok');
      } catch (err) {
        toast('Clear failed: ' + (err.message || err), 'err');
      }
    });
  });

  $('#pp-confirm').addEventListener('click', (e) => {
    const raw = $('#pp-input').value.trim();
    const v = parseInt(raw, 10);
    if (!raw || !Number.isFinite(v) || v <= 0) {
      toast('Enter a positive number first.', 'err');
      return;
    }
    withButtonLoading(e.currentTarget, '…sending', async () => {
      try {
        await apiFetch('/api/runs/stdin', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({line: String(v)}),
        });
        $('#price-prompt').hidden = true;
      } catch (err) {
        toast('Failed to send: ' + (err.message || err), 'err');
      }
    });
  });
}

export function load() {}
