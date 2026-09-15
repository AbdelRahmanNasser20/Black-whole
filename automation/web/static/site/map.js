// static/site/map.js — the three public map surfaces (home band, /map, listing mini-map).
// One feed (/map/api/points), one mounter. Pins are city-level; sold pins render muted.
import { load, api, esc, fmt } from '/static/ui/state.js';

const BUCKET_LABEL = { available: 'Available', incoming: 'Incoming', sold: 'Sold' };

function ensureAdminMap() {
  if (window.AdminMap) return Promise.resolve(window.AdminMap);
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/static/admin_map.js';
    s.onload = () => resolve(window.AdminMap);
    s.onerror = () => reject(new Error('map library failed to load'));
    document.head.appendChild(s);
  });
}

function popupHtml(p) {
  const qty = p.quantity != null ? `${fmt.int(p.quantity)} ${esc(p.unit || 'CHAIR')}${p.quantity === 1 ? '' : 'S'}` : '';
  const price = p.price_per_chair != null ? ` · $${fmt.int(p.price_per_chair)}/ea` : '';
  const where = [p.city, p.state].filter(Boolean).join(', ');
  const cta = p.kind === 'favorite' ? 'Message us to reserve →' : 'View lot →';
  return `<div class="map-pop b-${esc(p.bucket)}">
    ${p.hero ? `<img src="${esc(p.hero)}" alt="" loading="lazy">` : ''}
    <div class="map-pop-title">${esc(p.title)}</div>
    <div class="mono tiny">${esc(BUCKET_LABEL[p.bucket] || p.bucket)}${p.precision !== 'city' ? ' · approx' : ''} · ${esc(where)}</div>
    <div class="mono">${qty}${price}${p.distance_mi != null ? ` · ${p.distance_mi} mi` : ''}</div>
    <a href="${esc(p.url)}">${cta}</a>
  </div>`;
}

function listItemHtml(p) {
  const where = [p.city, p.state].filter(Boolean).join(', ');
  return `<a class="map-item b-${esc(p.bucket)}" href="${esc(p.url)}" data-id="${esc(p.id)}">
    ${p.hero ? `<img src="${esc(p.hero)}" alt="" loading="lazy">` : '<span class="map-item-noimg"></span>'}
    <span class="map-item-body">
      <span class="map-item-title">${esc(p.title)}</span>
      <span class="mono tiny">${esc(BUCKET_LABEL[p.bucket] || p.bucket)} · ${esc(where)}${p.distance_mi != null ? ` · ${p.distance_mi} mi` : ''}</span>
      <span class="mono">${p.quantity != null ? fmt.int(p.quantity) + ' ' + esc(p.unit || 'CHAIR') + 'S' : ''}${p.price_per_chair != null ? ' · $' + fmt.int(p.price_per_chair) + '/ea' : ''}</span>
    </span></a>`;
}

function readStatus(el, fallback = 'available,incoming') {
  return (el.dataset.status || fallback).split(',').map(s => s.trim()).filter(Boolean);
}

export async function mountSiteMap(el, opts = {}) {
  const listEl = opts.listEl || (el.dataset.list ? document.querySelector(el.dataset.list) : null);
  const countEl = opts.countEl || (el.dataset.count ? document.querySelector(el.dataset.count) : null);
  const state = {
    status: new Set(opts.status || readStatus(el)),
    near: opts.near ?? el.dataset.near ?? '',
    radius: opts.radius ?? el.dataset.radius ?? '',
    points: [], all: [],
  };
  // Only needed when there is no side list — it is load()'s target then. Never create
  // one beside a list: on /map the map and the list are siblings in a 2-column grid and
  // a third child would push the list onto its own row.
  let statusEl = listEl ? null : el.parentElement.querySelector(':scope > .map-status');
  if (!listEl && !statusEl) { statusEl = document.createElement('div'); statusEl.className = 'map-status mono tiny'; el.insertAdjacentElement('afterend', statusEl); }
  const AdminMap = await ensureAdminMap();
  const map = await AdminMap.mount(el, { tiles: el.dataset.tiles || 'light' });
  el.classList.remove('map-loading');

  const render = () => {
    const pts = state.all.filter(p => state.status.has(p.bucket));
    state.points = pts;
    map.setPoints(pts.map(p => ({ lat: p.lat, lng: p.lng, title: p.title, popup: popupHtml(p),
                                  approx: p.precision !== 'city', cls: 'b-' + p.bucket })));
    if (countEl) countEl.textContent = `${pts.length} lot${pts.length === 1 ? '' : 's'}`;
    if (listEl) {
      listEl.dataset.state = pts.length ? 'ready' : 'empty';
      listEl.innerHTML = pts.length ? pts.map(listItemHtml).join('')
        : '<div class="map-empty"><div class="display">Nothing here yet</div><p>Widen the radius or turn on Sold.</p></div>';
    }
  };

  const fetchAll = () => {
    const q = new URLSearchParams({ status: 'available,incoming,sold' });
    if (state.near) q.set('near', state.near);
    if (state.radius) q.set('radius', state.radius);
    const url = `${el.dataset.pointsUrl || '/map/api/points'}?${q}`;
    // NEVER hand the Leaflet container to load(): it replaces innerHTML with a
    // skeleton and would wipe the map. The side list (or a status sibling) owns
    // the loading state.
    const target = listEl || statusEl;
    return load(target, ({ signal }) => api(url, { signal }), {
      skeleton: listEl ? 'card' : 'line', count: listEl ? 3 : 1, keepOld: !!listEl,
      render: (d) => {
        state.all = d.points || []; render();
        if (d.near) map.leaflet.setView([d.near.lat, d.near.lng], state.radius ? 6 : 5);
        else if (!el.dataset.focusLat) map.fit();
        if (opts.onOrigin) opts.onOrigin(d.near);
        return listEl ? undefined : '';          // clear the status skeleton; the list rendered itself
      },
      isEmpty: () => false,
    });
  };

  if (el.dataset.focusLat && el.dataset.focusLng) {
    map.leaflet.setView([+el.dataset.focusLat, +el.dataset.focusLng], 6);
  }
  await fetchAll();

  return {
    map,
    setStatus(next) { state.status = new Set(next); render(); },
    setNear(near, radius) { state.near = near || ''; state.radius = radius || ''; return fetchAll(); },
    points: () => state.points,
  };
}

function wireFilters(form, ctl) {
  const seg = form.querySelector('#map-status');
  const hidden = seg && seg.querySelector('input[name="status"]');
  if (!seg || !hidden) return;
  const active = new Set((seg.dataset.value || 'available,incoming').split(','));
  const paint = () => seg.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('on', active.has(b.dataset.bucket)));
  paint();
  seg.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn'); if (!b) return;
    if (active.has(b.dataset.bucket)) active.delete(b.dataset.bucket); else active.add(b.dataset.bucket);
    paint(); hidden.value = [...active].join(','); ctl.setStatus(active);
    const u = new URL(location.href); u.searchParams.set('status', hidden.value); history.replaceState(null, '', u);
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const near = form.querySelector('#map-near').value.trim();
    const radius = form.querySelector('#map-radius').value;
    ctl.setNear(near, radius);
    const u = new URL(location.href);
    near ? u.searchParams.set('near', near) : u.searchParams.delete('near');
    radius ? u.searchParams.set('radius', radius) : u.searchParams.delete('radius');
    history.replaceState(null, '', u);
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  for (const el of document.querySelectorAll('[data-points-url]')) {
    try {
      const ctl = await mountSiteMap(el);
      const form = document.getElementById('map-filters');
      if (form && el.id === 'site-map') wireFilters(form, ctl);
      const homeToggle = document.getElementById('home-map-sold');
      if (homeToggle && el.id === 'home-map') {
        homeToggle.addEventListener('change', () => ctl.setStatus(homeToggle.checked ? ['available', 'incoming', 'sold'] : ['available', 'incoming']));
      }
    } catch (err) {
      el.classList.remove('map-loading');
      el.innerHTML = `<div class="map-empty"><p>${esc(err.message || 'Map unavailable')}</p></div>`;
    }
  }
});
